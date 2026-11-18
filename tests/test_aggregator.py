"""Tests for ActivityAggregator 1-minute bucket generator."""

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import TrackerState, ShiftStatus
from vaani_tracker.models import ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier
from vaani_tracker.windows.apps import WindowsProcessIdentifier
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.aggregator import ActivityAggregator


class TestActivityAggregator(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_events.db"
        self.queue = ActivityQueue(self.db_path)

        self.mock_idle_seconds = 0.0
        self.mock_foreground_process = "code.exe"

        self.idle_detector = WindowsIdleDetector(idle_provider=lambda: self.mock_idle_seconds)
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.process_id = WindowsProcessIdentifier(provider=lambda: self.mock_foreground_process)

        self.sm = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=self.session_notifier,
            power_notifier=self.power_notifier,
            idle_threshold_seconds=60.0,
        )

        self.aggregator = ActivityAggregator(
            state_machine=self.sm,
            process_identifier=self.process_id,
            queue=self.queue,
            device_uuid="dev-test-1",
            bucket_duration_seconds=60,
            track_apps=True,
            employee_id=1,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_no_tracking_when_stopped(self):
        # Shift not started
        for _ in range(60):
            res = self.aggregator.tick(1.0)
            self.assertIsNone(res)

        self.assertEqual(self.queue.get_pending_count(), 0)

    def test_60_second_active_bucket_creation(self):
        # Start shift
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        # 45 seconds on code.exe, 15 seconds on slack.exe
        for _ in range(45):
            self.aggregator.tick(1.0)

        self.mock_foreground_process = "slack.exe"
        closed_bucket = None
        for _ in range(15):
            b = self.aggregator.tick(1.0)
            if b:
                closed_bucket = b

        self.assertIsNotNone(closed_bucket)
        self.assertEqual(closed_bucket.active_seconds, 60)
        self.assertEqual(closed_bucket.idle_seconds, 0)
        self.assertEqual(closed_bucket.session_id, 1)

        # Check queue
        pending = self.queue.get_pending_buckets(employee_id=1)
        self.assertEqual(len(pending), 1)
        apps = {a.process_name: a.duration_seconds for a in pending[0].app_usages}
        self.assertEqual(apps.get("code.exe"), 45)
        self.assertEqual(apps.get("slack.exe"), 15)

    def test_mixed_active_and_idle_seconds(self):
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        # 30 seconds active
        self.mock_idle_seconds = 5.0
        for _ in range(30):
            self.aggregator.tick(1.0)

        # 30 seconds idle (idle_seconds >= 60)
        self.mock_idle_seconds = 70.0
        closed_bucket = None
        for _ in range(30):
            b = self.aggregator.tick(1.0)
            if b:
                closed_bucket = b

        self.assertIsNotNone(closed_bucket)
        self.assertEqual(closed_bucket.active_seconds, 30)
        self.assertEqual(closed_bucket.idle_seconds, 30)

    def test_session_boundary_finalizes_in_progress_bucket(self):
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        # 20 seconds active in session 1
        for _ in range(20):
            self.aggregator.tick(1.0)

        # Session 1 ends and Session 2 starts
        self.sm.update_shift_status(ShiftInfo(session_id=2, status=ShiftStatus.ACTIVE))
        bucket = self.aggregator.tick(1.0)

        self.assertIsNotNone(bucket)
        self.assertEqual(bucket.session_id, 1)
        self.assertEqual(bucket.active_seconds, 20)

    def test_tick_after_stop_returns_none_and_records_no_activity(self):
        """R-1 Regression: Calling tick() after stop() must be a no-op and add zero database activity."""
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        # Accumulate 15 seconds
        for _ in range(15):
            self.aggregator.tick(1.0)

        # Stop aggregator
        self.aggregator.stop()

        # Subsequent ticks after stop must return None and add zero buckets/activity
        for _ in range(120):
            res = self.aggregator.tick(1.0)
            self.assertIsNone(res, "tick() after stop() must return None")

        # Close in-progress bucket: closes the 15-second partial bucket
        b1 = self.aggregator.close_current_bucket()
        self.assertIsNotNone(b1)
        self.assertEqual(b1.active_seconds, 15)

        # Subsequent close_current_bucket calls return None
        self.assertIsNone(self.aggregator.close_current_bucket())

    def test_repeated_stop_calls_are_safe_and_idempotent(self):
        """R-1 Regression: Repeated stop calls must be safe, idempotent, and not raise or re-persist."""
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(10):
            self.aggregator.tick(1.0)

        self.aggregator.stop()
        b = self.aggregator.close_current_bucket()
        self.assertIsNotNone(b)
        count1 = self.queue.get_total_bucket_count(allow_unfiltered=True)

        # Second and third stop calls
        self.aggregator.stop()
        self.aggregator.stop()
        self.assertIsNone(self.aggregator.close_current_bucket())
        count2 = self.queue.get_total_bucket_count(allow_unfiltered=True)
        self.assertEqual(count1, count2, "Repeated stop calls must not duplicate buckets")

    def test_tick_racing_with_shutdown(self):
        """R-1 Regression: Concurrent tick calls racing with stop() must not deadlock or corrupt state."""
        import threading
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        errors = []

        def tick_worker():
            try:
                for _ in range(100):
                    self.aggregator.tick(0.1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=tick_worker) for _ in range(4)]
        for t in threads:
            t.start()

        # Stop while workers are ticking
        self.aggregator.stop()

        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Race condition during shutdown threw: {errors}")
        # Verify subsequent ticks remain blocked
        self.assertIsNone(self.aggregator.tick(1.0))

    def test_invalid_or_negative_elapsed_duration(self):
        """Preserves existing guard against invalid or non-positive durations."""
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        self.assertIsNone(self.aggregator.tick(0.0))
        self.assertIsNone(self.aggregator.tick(-1.0))


if __name__ == "__main__":
    unittest.main()

