"""Phase 4.5.4.4 automated tests for SQLite corruption recovery and storage resilience."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import ShiftStatus, SyncStatus, TrackerState
from vaani_tracker.core.aggregator import ActivityAggregator
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.models import ActivityBucket, ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue


class TestDatabaseResilience(unittest.TestCase):
    """Verifies corrupt database quarantine, WAL/SHM handling, and disk-full resilience."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "queue.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_zero_byte_database_quarantined_and_reinitialized(self) -> None:
        """A pre-existing 0-byte queue.db is quarantined to .corrupt.<ts> and replaced with a clean v2 DB."""
        self.db_path.write_bytes(b"")
        self.assertEqual(self.db_path.stat().st_size, 0)

        queue = ActivityQueue(self.db_path)

        self.assertIsNotNone(queue.last_quarantined_db_path)
        self.assertTrue(queue.last_quarantined_db_path.exists())
        self.assertEqual(queue.last_quarantined_db_path.stat().st_size, 0)
        self.assertIn("zero-byte", queue.last_quarantine_reason or "")

        # New database is valid and usable
        bucket = ActivityBucket(
            device_uuid="dev-01",
            employee_id=101,
            session_id=500,
            bucket_start="2026-11-12T10:00:00+00:00",
            bucket_end="2026-11-12T10:01:00+00:00",
            active_seconds=45,
            idle_seconds=15,
            state=TrackerState.ACTIVE,
        )
        queue.save_bucket(bucket, employee_id=101)
        self.assertEqual(queue.get_pending_count(employee_id=101), 1)

    def test_02_random_non_sqlite_bytes_quarantined_with_wal_and_shm(self) -> None:
        """Non-SQLite garbage bytes and associated -wal/-shm files are all quarantined safely."""
        garbage_payload = b"NOT_A_VALID_SQLITE_HEADER_RANDOM_CORRUPTION_1234567890" * 8
        wal_payload = b"STALE_WAL_BYTES_987654321"
        shm_payload = b"STALE_SHM_BYTES_123456789"

        self.db_path.write_bytes(garbage_payload)
        wal_path = Path(f"{self.db_path}-wal")
        shm_path = Path(f"{self.db_path}-shm")
        wal_path.write_bytes(wal_payload)
        shm_path.write_bytes(shm_payload)

        queue = ActivityQueue(self.db_path)

        quarantined_db = queue.last_quarantined_db_path
        self.assertIsNotNone(quarantined_db)
        self.assertTrue(quarantined_db.exists())
        self.assertEqual(quarantined_db.read_bytes(), garbage_payload)

        # Verify matching -wal and -shm quarantine files were preserved
        suffix = quarantined_db.name.split(".corrupt.", 1)[1]
        quarantined_wal = self.db_path.with_name(f"{self.db_path.name}-wal.corrupt.{suffix}")
        quarantined_shm = self.db_path.with_name(f"{self.db_path.name}-shm.corrupt.{suffix}")
        self.assertTrue(quarantined_wal.exists())
        self.assertTrue(quarantined_shm.exists())
        self.assertEqual(quarantined_wal.read_bytes(), wal_payload)
        self.assertEqual(quarantined_shm.read_bytes(), shm_payload)

        # Verify new queue works cleanly
        queue.set_meta("health_check", "ok")
        self.assertEqual(queue.get_meta("health_check"), "ok")

    def test_03_malformed_schema_missing_table_or_columns_quarantined(self) -> None:
        """A SQLite file with user_version=2 but missing activity_buckets or required columns is quarantined."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA user_version = 2;")
        conn.execute("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY);")
        conn.commit()
        conn.close()

        queue = ActivityQueue(self.db_path)
        self.assertIsNotNone(queue.last_quarantined_db_path)
        self.assertTrue(queue.last_quarantined_db_path.exists())
        self.assertIn("missing activity_buckets", queue.last_quarantine_reason or "")

        # Second corruption in the same second never overwrites the first quarantine file
        first_quarantine = queue.last_quarantined_db_path
        conn2 = sqlite3.connect(str(self.db_path))
        conn2.execute("DROP TABLE activity_buckets;")
        conn2.execute("CREATE TABLE activity_buckets (bucket_id TEXT PRIMARY KEY);")
        conn2.commit()
        conn2.close()

        queue2 = ActivityQueue(self.db_path)
        second_quarantine = queue2.last_quarantined_db_path
        self.assertIsNotNone(second_quarantine)
        self.assertNotEqual(first_quarantine, second_quarantine)
        self.assertTrue(first_quarantine.exists())
        self.assertTrue(second_quarantine.exists())
        self.assertIn("missing required columns", queue2.last_quarantine_reason or "")

    def test_04_valid_database_preserves_offline_buckets_across_restarts(self) -> None:
        """A healthy database with pending offline buckets is never quarantined on restart."""
        queue1 = ActivityQueue(self.db_path)
        b1 = ActivityBucket(
            device_uuid="dev-01",
            employee_id=77,
            session_id=900,
            bucket_start="2026-11-12T11:00:00+00:00",
            bucket_end="2026-11-12T11:01:00+00:00",
            active_seconds=50,
            idle_seconds=10,
            state=TrackerState.ACTIVE,
        )
        queue1.save_bucket(b1, employee_id=77)
        self.assertIsNone(queue1.last_quarantined_db_path)

        # Re-open queue on same path
        queue2 = ActivityQueue(self.db_path)
        self.assertIsNone(queue2.last_quarantined_db_path)
        pending = queue2.get_pending_buckets(employee_id=77)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].bucket_id, b1.bucket_id)
        self.assertEqual(pending[0].active_seconds, 50)

    def test_05_disk_full_during_aggregator_tick_does_not_crash_and_recovers(self) -> None:
        """Simulated disk-full OperationalError during tick() logs cleanly, preserves accumulators, and recovers."""
        queue = ActivityQueue(self.db_path)

        idle_det = MagicMock()
        idle_det.get_idle_seconds.return_value = 0.0
        sess_not = MagicMock()
        sess_not.is_logged_off = False
        sess_not.is_locked = False
        pwr_not = MagicMock()
        pwr_not.is_sleeping = False

        sm = TrackingStateMachine(
            idle_detector=idle_det,
            session_notifier=sess_not,
            power_notifier=pwr_not,
            idle_threshold_seconds=300,
        )
        sm.update_shift_status(
            ShiftInfo(
                session_id=888,
                status=ShiftStatus.ACTIVE,
            )
        )

        proc_id = MagicMock()
        proc_id.get_foreground_process_name.return_value = "code.exe"

        mono_now = [1000.0]
        aggregator = ActivityAggregator(
            state_machine=sm,
            process_identifier=proc_id,
            queue=queue,
            device_uuid="dev-disk-test",
            bucket_duration_seconds=60,
            clock=lambda: mono_now[0],
            employee_id=42,
        )

        # Accumulate 59 seconds normally
        mono_now[0] += 59.0
        res1 = aggregator.tick(elapsed_seconds=59.0)
        self.assertIsNone(res1)
        self.assertEqual(aggregator._active_seconds, 59)

        # Simulate disk full on the 60th second when tick tries to close and save the bucket
        with patch.object(
            queue,
            "save_bucket",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            mono_now[0] += 1.0
            res_fail = aggregator.tick(elapsed_seconds=1.0)
            # Tick must not raise or crash; bucket was not closed due to disk full
            self.assertIsNone(res_fail)
            # In-memory accumulators are preserved at 60s
            self.assertEqual(aggregator._active_seconds, 60)
            self.assertEqual(queue.get_pending_count(employee_id=42), 0)

        # Next tick after disk space recovers: closes the preserved 60s bucket and records the new 1s slice
        mono_now[0] += 1.0
        res_recovered = aggregator.tick(elapsed_seconds=1.0)
        self.assertIsNotNone(res_recovered)
        self.assertEqual(res_recovered.active_seconds, 60)
        self.assertEqual(queue.get_pending_count(employee_id=42), 1)
        self.assertEqual(aggregator._active_seconds, 1)


if __name__ == "__main__":
    unittest.main()
