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
        pending = self.queue.get_pending_buckets()
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


if __name__ == "__main__":
    unittest.main()

