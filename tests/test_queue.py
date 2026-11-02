"""Tests for SQLite WAL activity queue."""

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import TrackerState, SyncStatus
from vaani_tracker.models import ActivityBucket, AppUsage
from vaani_tracker.storage.queue import ActivityQueue


class TestActivityQueue(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_events.db"
        self.queue = ActivityQueue(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_retrieve_pending_bucket(self):
        bucket = ActivityBucket(
            bucket_id="b-101",
            device_uuid="dev-test-1",
            session_id=1,
            bucket_start="2026-11-02T10:00:00Z",
            bucket_end="2026-11-02T10:01:00Z",
            active_seconds=45,
            idle_seconds=15,
            state=TrackerState.ACTIVE,
            app_usages=[AppUsage(process_name="code.exe", duration_seconds=45)],
        )
        self.queue.save_bucket(bucket)

        pending = self.queue.get_pending_buckets(limit=10)
        self.assertEqual(len(pending), 1)
        retrieved = pending[0]
        self.assertEqual(retrieved.bucket_id, "b-101")
        self.assertEqual(retrieved.active_seconds, 45)
        self.assertEqual(retrieved.idle_seconds, 15)
        self.assertEqual(retrieved.state, TrackerState.ACTIVE)
        self.assertEqual(len(retrieved.app_usages), 1)
        self.assertEqual(retrieved.app_usages[0].process_name, "code.exe")
        self.assertEqual(retrieved.app_usages[0].duration_seconds, 45)

    def test_idempotent_save_updates_existing_bucket(self):
        bucket1 = ActivityBucket(
            bucket_id="b-dup-1",
            device_uuid="dev-1",
            session_id=1,
            active_seconds=30,
            idle_seconds=0,
        )
        self.queue.save_bucket(bucket1)

        # Update same bucket_id with newer values
        bucket2 = ActivityBucket(
            bucket_id="b-dup-1",
            device_uuid="dev-1",
            session_id=1,
            active_seconds=50,
            idle_seconds=10,
        )
        self.queue.save_bucket(bucket2)

        pending = self.queue.get_pending_buckets()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].active_seconds, 50)
        self.assertEqual(pending[0].idle_seconds, 10)

    def test_mark_synced_removes_from_pending(self):
        b1 = ActivityBucket(bucket_id="b-1", device_uuid="dev-1", active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-2", device_uuid="dev-1", active_seconds=60)
        self.queue.save_bucket(b1)
        self.queue.save_bucket(b2)

        self.assertEqual(self.queue.get_pending_count(), 2)

        # Mark b1 synced
        self.queue.mark_buckets_synced(["b-1"])
        self.assertEqual(self.queue.get_pending_count(), 1)

        pending = self.queue.get_pending_buckets()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].bucket_id, "b-2")

    def test_mark_failed_increments_retry_count(self):
        b = ActivityBucket(bucket_id="b-fail-1", device_uuid="dev-1", active_seconds=30)
        self.queue.save_bucket(b)

        self.queue.mark_buckets_failed(["b-fail-1"], "Connection timeout")
        pending = self.queue.get_pending_buckets()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].retry_count, 1)
        self.assertEqual(pending[0].sync_status, SyncStatus.FAILED)

    def test_summary_stats_aggregation(self):
        b1 = ActivityBucket(
            bucket_id="b-s1",
            device_uuid="dev-1",
            bucket_start="2026-11-02T10:00:00Z",
            active_seconds=40,
            idle_seconds=20,
            app_usages=[
                AppUsage(process_name="code.exe", duration_seconds=30),
                AppUsage(process_name="slack.exe", duration_seconds=10),
            ]
        )
        b2 = ActivityBucket(
            bucket_id="b-s2",
            device_uuid="dev-1",
            bucket_start="2026-11-02T10:01:00Z",
            active_seconds=50,
            idle_seconds=10,
            app_usages=[
                AppUsage(process_name="code.exe", duration_seconds=50),
            ]
        )
        self.queue.save_bucket(b1)
        self.queue.save_bucket(b2)

        stats = self.queue.get_summary_stats("2026-11-02T10:00:00Z")
        self.assertEqual(stats["total_active_seconds"], 90)
        self.assertEqual(stats["total_idle_seconds"], 30)
        self.assertEqual(stats["total_bucket_count"], 2)
        self.assertEqual(stats["app_breakdown"]["code.exe"], 80)
        self.assertEqual(stats["app_breakdown"]["slack.exe"], 10)

    def test_meta_key_value_storage(self):
        self.assertIsNone(self.queue.get_meta("non_existent"))
        self.queue.set_meta("device_token", "secret_token_123")
        self.assertEqual(self.queue.get_meta("device_token"), "secret_token_123")

        # Test dictionary JSON serialization
        self.queue.set_meta("employee_info", {"id": 42, "name": "Tuhi"})
        info = self.queue.get_meta("employee_info")
        self.assertEqual(info["id"], 42)
        self.assertEqual(info["name"], "Tuhi")

        self.queue.clear_meta()
        self.assertIsNone(self.queue.get_meta("device_token"))


if __name__ == "__main__":
    unittest.main()

