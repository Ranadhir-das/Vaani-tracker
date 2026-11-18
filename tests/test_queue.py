"""Tests for SQLite WAL activity queue."""

import tempfile
import unittest
import uuid
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
        self.emp_id = 1

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_retrieve_pending_bucket(self):
        bucket = ActivityBucket(
            bucket_id="b-101",
            device_uuid="dev-test-1",
            employee_id=self.emp_id,
            session_id=1,
            bucket_start="2026-11-02T10:00:00Z",
            bucket_end="2026-11-02T10:01:00Z",
            active_seconds=45,
            idle_seconds=15,
            state=TrackerState.ACTIVE,
            app_usages=[AppUsage(process_name="code.exe", duration_seconds=45)],
        )
        self.queue.save_bucket(bucket, employee_id=self.emp_id)

        pending = self.queue.get_pending_buckets(limit=10, employee_id=self.emp_id)
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
            employee_id=self.emp_id,
            session_id=1,
            active_seconds=30,
            idle_seconds=0,
        )
        self.queue.save_bucket(bucket1, employee_id=self.emp_id)

        # Update same bucket_id with newer values
        bucket2 = ActivityBucket(
            bucket_id="b-dup-1",
            device_uuid="dev-1",
            employee_id=self.emp_id,
            session_id=1,
            active_seconds=50,
            idle_seconds=10,
        )
        self.queue.save_bucket(bucket2, employee_id=self.emp_id)

        pending = self.queue.get_pending_buckets(employee_id=self.emp_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].active_seconds, 50)
        self.assertEqual(pending[0].idle_seconds, 10)

    def test_mark_synced_removes_from_pending(self):
        b1 = ActivityBucket(bucket_id="b-1", device_uuid="dev-1", employee_id=self.emp_id, active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-2", device_uuid="dev-1", employee_id=self.emp_id, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=self.emp_id)
        self.queue.save_bucket(b2, employee_id=self.emp_id)

        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 2)

        # Mark b1 synced
        self.queue.mark_buckets_synced(["b-1"], employee_id=self.emp_id)
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        pending = self.queue.get_pending_buckets(employee_id=self.emp_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].bucket_id, "b-2")

    def test_mark_failed_increments_retry_count(self):
        b = ActivityBucket(bucket_id="b-fail-1", device_uuid="dev-1", employee_id=self.emp_id, active_seconds=30)
        self.queue.save_bucket(b, employee_id=self.emp_id)

        self.queue.mark_buckets_failed(["b-fail-1"], "Connection timeout", employee_id=self.emp_id)
        pending = self.queue.get_pending_buckets(employee_id=self.emp_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].retry_count, 1)
        self.assertEqual(pending[0].sync_status, SyncStatus.FAILED)

    def test_summary_stats_aggregation(self):
        b1 = ActivityBucket(
            bucket_id="b-s1",
            device_uuid="dev-1",
            employee_id=self.emp_id,
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
            employee_id=self.emp_id,
            bucket_start="2026-11-02T10:01:00Z",
            active_seconds=50,
            idle_seconds=10,
            app_usages=[
                AppUsage(process_name="code.exe", duration_seconds=50),
            ]
        )
        self.queue.save_bucket(b1, employee_id=self.emp_id)
        self.queue.save_bucket(b2, employee_id=self.emp_id)

        stats = self.queue.get_summary_stats("2026-11-02T10:00:00Z", employee_id=self.emp_id)
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

    def test_prune_synced_buckets_validation(self):
        with self.assertRaises(ValueError):
            self.queue.prune_synced_buckets(max_age_days=-1)

    def test_mark_buckets_dead_letter_explicit(self):
        b = ActivityBucket(bucket_id="b-dl-1", device_uuid="dev-1", employee_id=self.emp_id, active_seconds=30)
        self.queue.save_bucket(b, employee_id=self.emp_id)
        self.queue.mark_buckets_dead_letter(["b-dl-1"], "Invalid format", employee_id=self.emp_id)

        pending = self.queue.get_pending_buckets(employee_id=self.emp_id)
        self.assertEqual(len(pending), 0)

        dead = self.queue.get_dead_letter_buckets(employee_id=self.emp_id)
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0].bucket_id, "b-dl-1")
        self.assertEqual(dead[0].sync_status, SyncStatus.DEAD_LETTER)
        self.assertEqual(dead[0].last_error, "Invalid format")

    def test_collision_resolved_bucket_id_uuidv5_validity_and_determinism(self):
        """C-1 Regression: Collision-resolved bucket IDs must be deterministic, valid RFC 4122 UUIDv5s."""
        from vaani_tracker.storage.queue import derive_collision_bucket_id

        # 1. Non-collision bucket retains its original UUIDv4
        orig_uuid = str(uuid.uuid4())
        b_emp1 = ActivityBucket(
            bucket_id=orig_uuid,
            device_uuid="dev-1",
            employee_id=101,
            active_seconds=60,
            idle_seconds=0,
            state=TrackerState.ACTIVE,
        )
        self.queue.save_bucket(b_emp1, employee_id=101)
        self.assertEqual(b_emp1.bucket_id, orig_uuid, "Non-collision bucket must preserve original ID")

        # 2. Collision occurs when employee 102 attempts to save with the same bucket_id
        b_emp2 = ActivityBucket(
            bucket_id=orig_uuid,
            device_uuid="dev-1",
            employee_id=102,
            active_seconds=30,
            idle_seconds=30,
            state=TrackerState.ACTIVE,
        )
        self.queue.save_bucket(b_emp2, employee_id=102)

        # 3. Verify resulting ID is a valid RFC 4122 UUIDv5
        derived_id = b_emp2.bucket_id
        parsed_uuid = uuid.UUID(derived_id)
        self.assertEqual(parsed_uuid.version, 5, "Collision-resolved bucket ID must be UUIDv5")
        self.assertEqual(parsed_uuid.variant, uuid.RFC_4122, "Collision-resolved bucket ID must have RFC 4122 variant")
        self.assertEqual((parsed_uuid.bytes[8] >> 6), 0b10, "RFC 4122 variant bits must be 0b10")
        self.assertNotEqual(derived_id, orig_uuid, "Collided bucket ID must differ from original")

        # 4. Verify original employee 101 record is preserved intact without mutation or reassignment
        emp1_buckets = self.queue.get_pending_buckets(employee_id=101)
        self.assertEqual(len(emp1_buckets), 1)
        self.assertEqual(emp1_buckets[0].bucket_id, orig_uuid)
        self.assertEqual(emp1_buckets[0].employee_id, 101)
        self.assertEqual(emp1_buckets[0].active_seconds, 60)

        # 5. Verify employee 102 record is queryable under the new valid UUID
        emp2_buckets = self.queue.get_pending_buckets(employee_id=102)
        self.assertEqual(len(emp2_buckets), 1)
        self.assertEqual(emp2_buckets[0].bucket_id, derived_id)
        self.assertEqual(emp2_buckets[0].employee_id, 102)
        self.assertEqual(emp2_buckets[0].active_seconds, 30)

        # 6. Determinism: derive_collision_bucket_id with identical inputs produces exact same UUID
        expected_uuid = derive_collision_bucket_id(orig_uuid, 102)
        self.assertEqual(derived_id, expected_uuid)

        # 7. Employee separation: employee 103 on same original ID produces a distinct UUID
        emp3_uuid = derive_collision_bucket_id(orig_uuid, 103)
        self.assertNotEqual(derived_id, emp3_uuid)
        self.assertEqual(uuid.UUID(emp3_uuid).version, 5)

        # 8. Stability across retries: re-saving b_emp2 updates in-place without generating a new UUID
        b_emp2.active_seconds = 45
        self.queue.save_bucket(b_emp2, employee_id=102)
        self.assertEqual(b_emp2.bucket_id, derived_id, "Resaving must retain the resolved UUID")
        emp2_after = self.queue.get_pending_buckets(employee_id=102)
        self.assertEqual(len(emp2_after), 1)
        self.assertEqual(emp2_after[0].bucket_id, derived_id)
        self.assertEqual(emp2_after[0].active_seconds, 45)

        # 9. Stability across restarts: instantiate a new ActivityQueue on the same database file
        restarted_queue = ActivityQueue(self.db_path)
        r_emp1 = restarted_queue.get_pending_buckets(employee_id=101)
        r_emp2 = restarted_queue.get_pending_buckets(employee_id=102)
        self.assertEqual(len(r_emp1), 1)
        self.assertEqual(r_emp1[0].bucket_id, orig_uuid)
        self.assertEqual(len(r_emp2), 1)
        self.assertEqual(r_emp2[0].bucket_id, derived_id)


if __name__ == "__main__":
    unittest.main()

