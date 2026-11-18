"""Tests for SQLite employee queue isolation, migrations, and reliability.

Verifies:
- Scenario 1: Employee 1 queues records, unpairs, and Employee 2 logs in.
- Scenario 2: Employee 2 cannot retrieve or upload Employee 1's records.
- Scenario 3: Employee 1 can recover their records after re-authentication.
- Scenario 4: Legacy database migration preserves records without guessing ownership.
- Scenario 5: SQLite migration is safe when run repeatedly or on existing databases.
- Scenario 10: Dead-letter records do not block new pending records (anti-HOL blocking).
- Scenario 15: Synced-record pruning preserves every unsynced record.
- Scenario 16: Multiple concurrent queue operations do not corrupt SQLite.
"""

from datetime import datetime, timezone, timedelta
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, SyncStatus, MAX_RETRY_COUNT
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.syncer import BackgroundSyncer
from vaani_tracker.models import ActivityBucket, AppUsage, SyncResult
from vaani_tracker.storage.queue import ActivityQueue, CURRENT_SCHEMA_VERSION
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier
from vaani_tracker.ui.summary import generate_summary_html


class TestEmployeeQueueIsolation(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "events.db"
        self.queue = ActivityQueue(self.db_path)
        self.config = TrackerConfig(data_dir=self.data_dir, device_uuid="dev-isolation-test")
        self.auth = DeviceAuthManager(self.config, self.queue)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_account_switching_and_queue_isolation(self):
        """Scenarios 1, 2, 3: Employee 1 unpairs, Employee 2 logs in, Employee 1 recovers."""
        # --- Step 1: Employee 1 logs in and generates 3 pending buckets ---
        self.auth.bind_employee(101, "Agent Rahul")
        self.auth.set_token("token_emp_101")

        for i in range(3):
            b = ActivityBucket(
                bucket_id=f"b-rahul-{i}",
                device_uuid=self.config.device_uuid,
                employee_id=101,
                session_id=501,  # Rahul's WorkSession
                active_seconds=50,
                idle_seconds=10,
                state=TrackerState.ACTIVE,
            )
            self.queue.save_bucket(b, employee_id=101)

        # Verify Employee 1 sees 3 pending buckets
        rahul_pending = self.queue.get_pending_buckets(employee_id=101)
        self.assertEqual(len(rahul_pending), 3)

        # --- Step 2: Employee 1 unpairs (e.g. shift ends or shift handover) ---
        self.auth.clear_credentials()

        # Unpairing must NOT delete pending records from SQLite
        self.assertEqual(self.queue.get_total_bucket_count(allow_unfiltered=True), 3)
        self.assertEqual(self.queue.get_pending_count(employee_id=101), 3)

        # --- Step 3: Employee 2 (Priya) logs in on the same workstation ---
        self.auth.bind_employee(102, "Agent Priya")
        self.auth.set_token("token_emp_102")

        # Scenario 2: Employee 2 MUST NOT be able to retrieve or upload Rahul's records
        priya_pending = self.queue.get_pending_buckets(employee_id=102)
        self.assertEqual(len(priya_pending), 0, "Employee 2 must not see Employee 1's pending records")

        # Priya works and creates 2 buckets
        for i in range(2):
            b = ActivityBucket(
                bucket_id=f"b-priya-{i}",
                device_uuid=self.config.device_uuid,
                employee_id=102,
                session_id=602,  # Priya's WorkSession
                active_seconds=60,
                idle_seconds=0,
                state=TrackerState.ACTIVE,
            )
            self.queue.save_bucket(b, employee_id=102)

        # Priya only sees her own 2 pending buckets
        priya_pending = self.queue.get_pending_buckets(employee_id=102)
        self.assertEqual(len(priya_pending), 2)
        self.assertEqual({b.bucket_id for b in priya_pending}, {"b-priya-0", "b-priya-1"})
        for b in priya_pending:
            self.assertEqual(b.employee_id, 102)
            self.assertEqual(b.session_id, 602)

        # --- Step 4: Scenario 3: Employee 1 logs back in later ---
        self.auth.clear_credentials()
        self.auth.bind_employee(101, "Agent Rahul")
        self.auth.set_token("token_emp_101_reauth")

        # Rahul can retrieve his original 3 records with original WorkSession ID 501 preserved
        recovered_pending = self.queue.get_pending_buckets(employee_id=101)
        self.assertEqual(len(recovered_pending), 3)
        for b in recovered_pending:
            self.assertEqual(b.employee_id, 101)
            self.assertEqual(b.session_id, 501)

    def test_legacy_database_migration_quarantines_unassigned_records(self):
        """Scenario 4: Legacy database without employee_id preserves records into ORPHANED state."""
        legacy_db_path = self.data_dir / "legacy_v1.db"

        # Create a legacy (version 0 / v1) schema database manually
        conn = sqlite3.connect(str(legacy_db_path))
        conn.execute("""
            CREATE TABLE activity_buckets (
                bucket_id TEXT PRIMARY KEY,
                device_uuid TEXT NOT NULL,
                session_id INTEGER,
                bucket_start TEXT NOT NULL,
                bucket_end TEXT NOT NULL,
                active_seconds INTEGER NOT NULL,
                idle_seconds INTEGER NOT NULL,
                state TEXT NOT NULL,
                is_locked INTEGER NOT NULL,
                is_sleeping INTEGER NOT NULL,
                is_paused INTEGER NOT NULL,
                apps_json TEXT NOT NULL,
                sync_status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL
            );
        """)
        # Insert 1 legacy pending bucket and 1 legacy synced bucket
        conn.execute("""
            INSERT INTO activity_buckets VALUES (
                'legacy-pending-1', 'dev-leg-1', 400, '2026-11-01T10:00:00Z', '2026-11-01T10:01:00Z',
                40, 20, 'ACTIVE', 0, 0, 0, '[]', 'PENDING', 0, NULL, '2026-11-01T10:01:00Z'
            );
        """)
        conn.execute("""
            INSERT INTO activity_buckets VALUES (
                'legacy-synced-1', 'dev-leg-1', 400, '2026-11-01T09:00:00Z', '2026-11-01T09:01:00Z',
                60, 0, 'ACTIVE', 0, 0, 0, '[]', 'SYNCED', 0, NULL, '2026-11-01T09:01:00Z'
            );
        """)
        conn.commit()
        conn.close()

        # Initialize ActivityQueue on the legacy database to trigger automatic migration
        migrated_queue = ActivityQueue(legacy_db_path)

        # 1. Verify PRAGMA user_version is updated to current
        conn = sqlite3.connect(str(legacy_db_path))
        cursor = conn.execute("PRAGMA user_version;")
        self.assertEqual(cursor.fetchone()[0], CURRENT_SCHEMA_VERSION)

        # 2. Verify employee_id column was added
        cursor = conn.execute("PRAGMA table_info(activity_buckets);")
        col_names = [r[1] for r in cursor.fetchall()]
        self.assertIn("employee_id", col_names)
        conn.close()

        # 3. Verify legacy pending bucket was quarantined (ORPHANED), not assigned to any user
        orphaned = migrated_queue.get_orphaned_buckets()
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(orphaned[0].bucket_id, "legacy-pending-1")
        self.assertIsNone(orphaned[0].employee_id)
        self.assertEqual(orphaned[0].session_id, 400)
        self.assertEqual(orphaned[0].sync_status, SyncStatus.ORPHANED)

        # 4. Verify a newly bound employee cannot accidentally upload the legacy record
        self.assertEqual(len(migrated_queue.get_pending_buckets(employee_id=999)), 0)

    def test_migration_is_idempotent_and_safe_on_repeated_runs(self):
        """Scenario 5: Repeated _init_db() calls do not fail or duplicate objects."""
        # Run init 3 times consecutively
        self.queue._init_db()
        self.queue._init_db()

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("PRAGMA user_version;")
        self.assertEqual(cursor.fetchone()[0], CURRENT_SCHEMA_VERSION)

        # Check indexes exist without conflict
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity_buckets';")
        indexes = {r[0] for r in cursor.fetchall()}
        self.assertIn("idx_activity_buckets_emp_sync", indexes)
        conn.close()

    def test_dead_letter_records_do_not_block_new_pending_records(self):
        """Scenario 10: Head-of-line blocking prevention with bounded retry and DEAD_LETTER."""
        emp_id = 205

        # 1. Create 5 permanently failed buckets (simulating repeated failure or fatal error)
        dead_ids = [f"b-dead-{i}" for i in range(5)]
        for b_id in dead_ids:
            b = ActivityBucket(
                bucket_id=b_id,
                device_uuid=self.config.device_uuid,
                employee_id=emp_id,
                bucket_start="2026-11-02T08:00:00Z",
                active_seconds=60,
                sync_status=SyncStatus.PENDING,
            )
            self.queue.save_bucket(b, employee_id=emp_id)

        # Mark them as DEAD_LETTER (or max retries exceeded)
        self.queue.mark_buckets_failed(dead_ids, "INVALID_SHIFT: Unrecoverable mismatch", is_fatal=True)

        # 2. Add 1 fresh pending bucket
        fresh_bucket = ActivityBucket(
            bucket_id="b-fresh-1",
            device_uuid=self.config.device_uuid,
            employee_id=emp_id,
            bucket_start="2026-11-02T10:00:00Z",
            active_seconds=60,
            sync_status=SyncStatus.PENDING,
        )
        self.queue.save_bucket(fresh_bucket, employee_id=emp_id)

        # 3. Query pending buckets
        pending = self.queue.get_pending_buckets(employee_id=emp_id, limit=50)

        # MUST only return the 1 fresh bucket; the 5 dead-letter records must not block the queue!
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].bucket_id, "b-fresh-1")

        # Dead-letter records are preserved for administrative inspection
        dead_records = self.queue.get_dead_letter_buckets(employee_id=emp_id)
        self.assertEqual(len(dead_records), 5)

    def test_synced_record_pruning_preserves_every_unsynced_record(self):
        """Scenario 15: Pruning strictly removes old SYNCED records, never unsynced telemetry."""
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=45)).isoformat()
        recent_time = (now - timedelta(days=5)).isoformat()

        # Insert 3 old SYNCED buckets (> 30 days)
        for i in range(3):
            b = ActivityBucket(
                bucket_id=f"b-old-synced-{i}",
                device_uuid=self.config.device_uuid,
                employee_id=300,
                bucket_start=old_time,
                sync_status=SyncStatus.SYNCED,
            )
            self.queue.save_bucket(b, employee_id=300)
            self.queue.mark_buckets_synced([b.bucket_id])

        # Insert 2 recent SYNCED buckets (< 30 days)
        for i in range(2):
            b = ActivityBucket(
                bucket_id=f"b-recent-synced-{i}",
                device_uuid=self.config.device_uuid,
                employee_id=300,
                bucket_start=recent_time,
                sync_status=SyncStatus.SYNCED,
            )
            self.queue.save_bucket(b, employee_id=300)
            self.queue.mark_buckets_synced([b.bucket_id])

        # Insert 1 old PENDING bucket (> 30 days old)
        b_pending = ActivityBucket(
            bucket_id="b-old-pending",
            device_uuid=self.config.device_uuid,
            employee_id=300,
            bucket_start=old_time,
            sync_status=SyncStatus.PENDING,
        )
        self.queue.save_bucket(b_pending, employee_id=300)

        # Insert 1 old FAILED bucket (> 30 days old)
        b_failed = ActivityBucket(
            bucket_id="b-old-failed",
            device_uuid=self.config.device_uuid,
            employee_id=300,
            bucket_start=old_time,
            sync_status=SyncStatus.FAILED,
        )
        self.queue.save_bucket(b_failed, employee_id=300)
        self.queue.mark_buckets_failed([b_failed.bucket_id], "Temporary 500 error", is_fatal=False)

        # Insert 1 old DEAD_LETTER bucket (> 30 days old)
        b_dead = ActivityBucket(
            bucket_id="b-old-dead",
            device_uuid=self.config.device_uuid,
            employee_id=300,
            bucket_start=old_time,
            sync_status=SyncStatus.DEAD_LETTER,
        )
        self.queue.save_bucket(b_dead, employee_id=300)
        self.queue.mark_buckets_dead_letter([b_dead.bucket_id], "Permanent rejection")

        # Run pruning with 30-day cutoff
        pruned_count = self.queue.prune_synced_buckets(max_age_days=30)
        self.assertEqual(pruned_count, 3, "Only the 3 old SYNCED buckets should be pruned")

        # Verify remaining records
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("SELECT bucket_id, sync_status FROM activity_buckets;")
        remaining = {r[0]: r[1] for r in cursor.fetchall()}
        conn.close()

        # Recent SYNCED buckets preserved
        self.assertIn("b-recent-synced-0", remaining)
        self.assertIn("b-recent-synced-1", remaining)

        # All unsynced records preserved regardless of age
        self.assertIn("b-old-pending", remaining)
        self.assertEqual(remaining["b-old-pending"], SyncStatus.PENDING.value)
        self.assertIn("b-old-failed", remaining)
        self.assertEqual(remaining["b-old-failed"], SyncStatus.FAILED.value)
        self.assertIn("b-old-dead", remaining)
        self.assertEqual(remaining["b-old-dead"], SyncStatus.DEAD_LETTER.value)

    def test_concurrent_queue_operations(self):
        """Scenario 16: Multiple threads performing concurrent writes and reads without corruption."""
        errors = []
        buckets_per_thread = 25
        num_threads = 4

        def worker(thread_idx: int):
            try:
                for i in range(buckets_per_thread):
                    b_id = f"b-thread-{thread_idx}-{i}"
                    b = ActivityBucket(
                        bucket_id=b_id,
                        device_uuid="dev-concurrency",
                        employee_id=thread_idx,
                        active_seconds=30,
                        idle_seconds=30,
                        state=TrackerState.ACTIVE,
                    )
                    self.queue.save_bucket(b, employee_id=thread_idx)

                    # Read while writing
                    pending = self.queue.get_pending_buckets(employee_id=thread_idx, limit=10)
                    self.assertGreaterEqual(len(pending), 1)

                    if i % 5 == 0:
                        self.queue.mark_buckets_synced([b_id])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(len(errors), 0, f"Concurrent queue operations threw errors: {errors}")
        total_expected = buckets_per_thread * num_threads
        self.assertEqual(self.queue.get_total_bucket_count(allow_unfiltered=True), total_expected)

    def test_employee_upload_isolation_with_syncer(self):
        """Phase G #1: Verifies BackgroundSyncer never uploads Employee A's records when Employee B is authenticated."""
        idle_detector = WindowsIdleDetector(idle_provider=lambda: 0.0)
        session_notifier = WindowsSessionNotifier()
        power_notifier = WindowsPowerNotifier()
        sm = TrackingStateMachine(idle_detector, session_notifier, power_notifier)

        mock_api = MagicMock()
        uploaded_batches = []

        def fake_sync_buckets(device_uuid, token, buckets):
            uploaded_batches.append((token, [b.bucket_id for b in buckets], [b.employee_id for b in buckets]))
            return SyncResult(success=True, synced_count=len(buckets), accepted_bucket_ids=[b.bucket_id for b in buckets])

        mock_api.sync_buckets.side_effect = fake_sync_buckets

        syncer = BackgroundSyncer(
            config=self.config,
            api_client=mock_api,
            queue=self.queue,
            auth_manager=self.auth,
            state_machine=sm,
        )

        # 1. Employee A (ID 101) creates 2 buckets and unpairs
        self.auth.bind_employee(101, "Agent Rahul")
        self.auth.set_token("token_rahul_101")
        b_rahul_1 = ActivityBucket(bucket_id="b-rahul-101", device_uuid=self.config.device_uuid, employee_id=101, active_seconds=60)
        b_rahul_2 = ActivityBucket(bucket_id="b-rahul-102", device_uuid=self.config.device_uuid, employee_id=101, active_seconds=60)
        self.queue.save_bucket(b_rahul_1, employee_id=101)
        self.queue.save_bucket(b_rahul_2, employee_id=101)

        self.auth.clear_credentials()

        # 2. Employee B (ID 102) logs in and creates 1 bucket
        self.auth.bind_employee(102, "Agent Priya")
        self.auth.set_token("token_priya_102")
        b_priya_1 = ActivityBucket(bucket_id="b-priya-201", device_uuid=self.config.device_uuid, employee_id=102, active_seconds=60)
        self.queue.save_bucket(b_priya_1, employee_id=102)

        # 3. Syncer runs sync_pending_now() under Priya's authenticated session
        res = syncer.sync_pending_now()
        self.assertTrue(res.success)
        self.assertEqual(res.synced_count, 1)

        # 4. Assert ONLY Priya's bucket was uploaded with Priya's token
        self.assertEqual(len(uploaded_batches), 1)
        used_token, uploaded_ids, uploaded_emp_ids = uploaded_batches[0]
        self.assertEqual(used_token, "token_priya_102")
        self.assertEqual(uploaded_ids, ["b-priya-201"])
        self.assertEqual(uploaded_emp_ids, [102])

        # 5. Assert Rahul's buckets remain safely queued in SQLite and were NOT touched
        rahul_pending = self.queue.get_pending_buckets(employee_id=101)
        self.assertEqual(len(rahul_pending), 2)
        self.assertEqual({b.bucket_id for b in rahul_pending}, {"b-rahul-101", "b-rahul-102"})

    def test_interrupted_migration_rolls_back_and_recovers_safely(self):
        """Phase G #4: Interrupted or failed migrations roll back transactionally and recover cleanly on retry."""
        test_db = self.data_dir / "interrupted_migration.db"

        # Create a v1 legacy database
        conn = sqlite3.connect(str(test_db), isolation_level=None)
        conn.execute("""
            CREATE TABLE activity_buckets (
                bucket_id TEXT PRIMARY KEY,
                device_uuid TEXT NOT NULL,
                session_id INTEGER,
                bucket_start TEXT NOT NULL,
                bucket_end TEXT NOT NULL,
                active_seconds INTEGER NOT NULL,
                idle_seconds INTEGER NOT NULL,
                state TEXT NOT NULL,
                is_locked INTEGER NOT NULL,
                is_sleeping INTEGER NOT NULL,
                is_paused INTEGER NOT NULL,
                apps_json TEXT NOT NULL,
                sync_status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            INSERT INTO activity_buckets VALUES (
                'test-legacy-rec', 'dev-1', 10, '2026-11-01T10:00:00Z', '2026-11-01T10:01:00Z',
                60, 0, 'ACTIVE', 0, 0, 0, '[]', 'PENDING', 0, NULL, '2026-11-01T10:01:00Z'
            );
        """)
        conn.execute("PRAGMA user_version = 0;")
        conn.close()

        # Monkeypatch _migrate_to_v2 to simulate a crash midway through the migration
        orig_migrate = ActivityQueue._migrate_to_v2

        def failing_migrate(self_q, conn_arg):
            conn_arg.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);")
            conn_arg.execute("ALTER TABLE activity_buckets ADD COLUMN employee_id INTEGER;")
            # Simulate sudden failure (e.g. disk I/O error or power interruption)
            raise sqlite3.OperationalError("Simulated sudden power loss or disk failure")

        ActivityQueue._migrate_to_v2 = failing_migrate
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ActivityQueue(test_db)
        finally:
            ActivityQueue._migrate_to_v2 = orig_migrate

        # Verify rollback: PRAGMA user_version MUST still be 0, and partial DDL was rolled back
        conn = sqlite3.connect(str(test_db), isolation_level=None)
        cur = conn.execute("PRAGMA user_version;")
        self.assertEqual(cur.fetchone()[0], 0, "Failed migration must not increment user_version")

        cur = conn.execute("PRAGMA table_info(activity_buckets);")
        cols = [r[1] for r in cur.fetchall()]
        self.assertNotIn("employee_id", cols, "Rolled back migration must not leave partial columns")

        # Original legacy record is completely intact
        cur = conn.execute("SELECT bucket_id, sync_status FROM activity_buckets;")
        row = cur.fetchone()
        self.assertEqual(row[0], "test-legacy-rec")
        self.assertEqual(row[1], "PENDING")
        conn.close()

        # Re-attempt initialization: normal migration succeeds cleanly
        recovered_queue = ActivityQueue(test_db)

        conn = sqlite3.connect(str(test_db), isolation_level=None)
        cur = conn.execute("PRAGMA user_version;")
        self.assertEqual(cur.fetchone()[0], CURRENT_SCHEMA_VERSION, "Recovered database must have current schema version")
        conn.close()

        # Legacy record is now safely quarantined (ORPHANED)
        orphaned = recovered_queue.get_orphaned_buckets()
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(orphaned[0].bucket_id, "test-legacy-rec")
        self.assertEqual(orphaned[0].sync_status, SyncStatus.ORPHANED)

    def test_retry_limit_transitions_to_dead_letter(self):
        """Phase G #7: Bounded retries transition permanently failing records to DEAD_LETTER after MAX_RETRY_COUNT."""
        b_id = "b-bounded-retry-1"
        b = ActivityBucket(
            bucket_id=b_id,
            device_uuid=self.config.device_uuid,
            employee_id=50,
            active_seconds=60,
            sync_status=SyncStatus.PENDING,
        )
        self.queue.save_bucket(b, employee_id=50)

        # Fail 9 times (less than MAX_RETRY_COUNT = 10)
        for i in range(1, MAX_RETRY_COUNT):
            self.queue.mark_buckets_failed([b_id], f"Temporary HTTP 503 #{i}", is_fatal=False)
            pending = self.queue.get_pending_buckets(employee_id=50)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].retry_count, i)
            self.assertEqual(pending[0].sync_status, SyncStatus.FAILED)

        # 10th failure hits MAX_RETRY_COUNT -> transitions to DEAD_LETTER
        self.queue.mark_buckets_failed([b_id], "Temporary HTTP 503 #10", is_fatal=False)

        # Must no longer be returned in pending buckets
        pending = self.queue.get_pending_buckets(employee_id=50)
        self.assertEqual(len(pending), 0, "Record exceeding max retries must not appear in pending queue")

        # Must appear in dead letter inspection
        dead = self.queue.get_dead_letter_buckets(employee_id=50)
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0].bucket_id, b_id)
        self.assertEqual(dead[0].retry_count, 10)
        self.assertEqual(dead[0].sync_status, SyncStatus.DEAD_LETTER)
        self.assertIn("Temporary HTTP 503 #10", dead[0].last_error)

    def test_dead_letter_and_orphaned_buckets_not_uploaded_by_syncer(self):
        """Phase G #9: DEAD_LETTER and ORPHANED records are never uploaded automatically by syncer."""
        idle_detector = WindowsIdleDetector(idle_provider=lambda: 0.0)
        session_notifier = WindowsSessionNotifier()
        power_notifier = WindowsPowerNotifier()
        sm = TrackingStateMachine(idle_detector, session_notifier, power_notifier)

        mock_api = MagicMock()
        mock_api.sync_buckets.return_value = SyncResult(success=True, synced_count=1)

        syncer = BackgroundSyncer(
            config=self.config,
            api_client=mock_api,
            queue=self.queue,
            auth_manager=self.auth,
            state_machine=sm,
        )

        emp_id = 99
        self.auth.bind_employee(emp_id, "Test Syncer Quarantine")
        self.auth.set_token("token_emp_99")

        # 1. Create a DEAD_LETTER bucket
        b_dead = ActivityBucket(bucket_id="b-dead-no-sync", device_uuid=self.config.device_uuid, employee_id=emp_id, active_seconds=60)
        self.queue.save_bucket(b_dead, employee_id=emp_id)
        self.queue.mark_buckets_dead_letter([b_dead.bucket_id], "Fatal syntax error")

        # 2. Create an active PENDING bucket
        b_active = ActivityBucket(bucket_id="b-active-sync", device_uuid=self.config.device_uuid, employee_id=emp_id, active_seconds=60)
        self.queue.save_bucket(b_active, employee_id=emp_id)

        # 3. Run sync
        res = syncer.sync_pending_now()
        self.assertTrue(res.success)

        # 4. Verify mock_api.sync_buckets was called with ONLY b-active-sync, never b-dead-no-sync
        mock_api.sync_buckets.assert_called_once()
        _, kwargs = mock_api.sync_buckets.call_args
        synced_buckets = kwargs.get("buckets", [])
        self.assertEqual(len(synced_buckets), 1)
        self.assertEqual(synced_buckets[0].bucket_id, "b-active-sync")

        # DEAD_LETTER record remains intact and un-synced
        dead_records = self.queue.get_dead_letter_buckets(employee_id=emp_id)
        self.assertEqual(len(dead_records), 1)
        self.assertEqual(dead_records[0].bucket_id, "b-dead-no-sync")

    def test_unbound_employee_summary_and_dashboard_isolation(self):
        """Verifies summary and view models fail closed with unbound fallback when unauthenticated."""
        sm = TrackingStateMachine(
            idle_detector=MagicMock(spec=WindowsIdleDetector),
            session_notifier=MagicMock(spec=WindowsSessionNotifier),
            power_notifier=MagicMock(spec=WindowsPowerNotifier),
        )

        # Unauthenticated / unbound workstation
        self.assertFalse(self.auth.is_authenticated())
        html = generate_summary_html(self.config, sm, self.queue, auth_manager=self.auth)
        self.assertIn("Not Paired", html)
        self.assertIn("0s", html)  # Zeroed work duration
        self.assertIn("&mdash;", html)  # Clean entity for title

        # Now bind employee 505
        self.auth.bind_employee(505, "Vikram Mehta", "vikram@vaani.internal")
        self.auth.set_token("token_emp_505")
        b = ActivityBucket(
            bucket_id="b-vikram-1",
            device_uuid=self.config.device_uuid,
            employee_id=505,
            active_seconds=120,
            idle_seconds=0,
            state=TrackerState.ACTIVE,
        )
        self.queue.save_bucket(b, employee_id=505)

        html_bound = generate_summary_html(self.config, sm, self.queue, auth_manager=self.auth)
        self.assertIn("Vikram Mehta", html_bound)
        self.assertIn("505", html_bound)

    def test_quarantine_diagnostics_visibility(self):
        """Verifies quarantine diagnostics report orphaned and dead-letter records correctly."""
        # Create an orphaned bucket
        b_orph = ActivityBucket(bucket_id="b-diag-orph", device_uuid=self.config.device_uuid, active_seconds=30)
        self.queue.save_bucket(b_orph)
        self.queue.mark_buckets_dead_letter(["b-diag-orph"], "Corrupted payload format")

        diag = self.queue.get_quarantine_diagnostics()
        self.assertGreaterEqual(diag["dead_letter_count"], 1)
        self.assertGreaterEqual(diag["total_quarantined"], 1)
        self.assertIn("Corrupted payload format", diag["sample_reasons"])

        counts = self.queue.get_device_diagnostic_counts()
        self.assertGreaterEqual(counts["DEAD_LETTER"], 1)
        self.assertGreaterEqual(counts["total"], 1)

    def test_auth_state_explicit_bound_required(self):
        """S-1 Regression: Verifies that ONLY the exact auth_state 'BOUND' allows authentication."""
        # 1. Properly bound state succeeds
        self.auth.bind_employee(42, "Valid Agent", "agent42@vaani.co.in")
        self.auth.set_token("token_agent_42")
        self.assertEqual(self.auth.queue.get_meta("auth_state"), "BOUND")
        self.assertEqual(self.auth.get_authenticated_employee_id(), 42)

        # 2. auth_state is None (missing metadata key) fails closed
        self.queue.clear_meta()
        # Restore token and employee_id without auth_state
        from vaani_tracker.security.dpapi import protect_token
        self.queue.set_meta("auth_token", protect_token("token_agent_42"))
        self.queue.set_meta("employee_id", "42")
        self.assertIsNone(self.queue.get_meta("auth_state"))
        self.assertIsNone(self.auth.get_authenticated_employee_id(), "Missing auth_state must fail closed")

        # 3. Empty string auth_state fails closed
        self.queue.set_meta("auth_state", "")
        self.assertIsNone(self.auth.get_authenticated_employee_id(), "Empty auth_state must fail closed")

        # 4. Unknown / corrupt auth_states fail closed
        for bad_state in ["CORRUPT", "UNKNOWN", "TOKEN_ONLY", "EMPLOYEE_PENDING", "CLEARED", "bound", "Bound"]:
            self.queue.set_meta("auth_state", bad_state)
            self.assertIsNone(self.auth.get_authenticated_employee_id(), f"State '{bad_state}' must fail closed")

        # 5. Mismatched employee ID fails closed even if state is BOUND
        self.queue.set_meta("auth_state", "BOUND")
        self.queue.set_meta("employee_id", "99")  # Config has 42
        self.assertIsNone(self.auth.get_authenticated_employee_id(), "Mismatched employee_id must fail closed")

        # 6. Undecryptable token fails closed
        self.queue.set_meta("employee_id", "42")
        undecryptable = "dpapi:v1:invalid_corrupted_blob_not_decryptable"
        self.queue.set_meta("auth_token", undecryptable)
        self.config.auth_token = undecryptable
        self.auth._cached_token = None
        self.assertIsNone(self.auth.get_authenticated_employee_id(), "Undecryptable token must fail closed")


if __name__ == "__main__":
    unittest.main()

