"""Tests for BackgroundSyncer synchronization worker."""

import tempfile
import unittest
from unittest.mock import MagicMock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, ShiftStatus, SyncStatus
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.syncer import BackgroundSyncer
from vaani_tracker.models import ActivityBucket, ShiftInfo, SyncResult
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier


class TestBackgroundSyncer(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="dev-test-sync",
            auth_token="token_valid",
            registered_employee_id=1,
            registered_employee_name="Test User",
        )
        self.queue = ActivityQueue(self.config.db_path)
        self.auth = DeviceAuthManager(self.config, self.queue)
        self.auth.set_token("token_valid")
        self.auth.bind_employee(1, "Test User")

        self.idle_detector = WindowsIdleDetector(idle_provider=lambda: 0.0)
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.sm = TrackingStateMachine(self.idle_detector, self.session_notifier, self.power_notifier)

        self.mock_api = MagicMock()
        self.syncer = BackgroundSyncer(
            config=self.config,
            api_client=self.mock_api,
            queue=self.queue,
            auth_manager=self.auth,
            state_machine=self.sm,
        )

    def tearDown(self):
        self.syncer.stop()
        self.temp_dir.cleanup()

    def test_sync_pending_success(self):
        bucket = ActivityBucket(bucket_id="b-sync-1", device_uuid="dev-test-sync", employee_id=1, active_seconds=60)
        self.queue.save_bucket(bucket, employee_id=1)
        self.assertEqual(self.queue.get_pending_count(employee_id=1), 1)

        self.mock_api.sync_buckets.return_value = SyncResult(success=True, synced_count=1)

        res = self.syncer.sync_pending_now()
        self.assertTrue(res.success)
        self.assertEqual(self.queue.get_pending_count(employee_id=1), 0)

    def test_sync_token_revoked_clears_credentials(self):
        bucket = ActivityBucket(bucket_id="b-rev-1", device_uuid="dev-test-sync", employee_id=1, active_seconds=60)
        self.queue.save_bucket(bucket, employee_id=1)

        self.mock_api.sync_buckets.return_value = SyncResult(
            success=False,
            is_revoked=True,
            error_message="Device token revoked.",
        )

        res = self.syncer.sync_pending_now()
        self.assertFalse(res.success)
        # Auth must be cleared
        self.assertFalse(self.auth.is_authenticated())
        self.assertIsNone(self.config.auth_token)

    def test_heartbeat_updates_shift_state(self):
        new_shift = ShiftInfo(session_id=200, status=ShiftStatus.ACTIVE, started_at="2026-11-02T10:00:00Z")
        self.mock_api.fetch_status.return_value = (True, new_shift, {"idle_threshold_seconds": 60})

        online = self.syncer.heartbeat_now()
        self.assertTrue(online)
        self.assertEqual(self.sm.shift_info.session_id, 200)
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

    def test_start_does_not_create_duplicate_threads(self):
        """Repeated start() calls do not spawn duplicate worker threads."""
        self.syncer.start()
        self.assertTrue(self.syncer.is_running)
        sync_th = self.syncer._sync_thread
        hb_th = self.syncer._heartbeat_thread

        # Second call to start() must be an idempotent no-op
        self.syncer.start()
        self.assertIs(self.syncer._sync_thread, sync_th)
        self.assertIs(self.syncer._heartbeat_thread, hb_th)

        self.syncer.stop()
        self.assertFalse(self.syncer.is_running)

    def test_worker_reference_preserved_if_stop_times_out(self):
        """Worker reference is preserved if stop_sync fails to terminate the thread."""
        import threading
        hung_event = threading.Event()

        def hung_worker():
            hung_event.wait(timeout=2.0)

        hung_thread = threading.Thread(target=hung_worker, daemon=True)
        hung_thread.start()

        # Inject hung thread as sync worker
        self.syncer._sync_thread = hung_thread
        self.syncer._running = True
        self.syncer._sync_worker_running = True

        try:
            stopped = self.syncer.stop_sync(timeout=0.05)
            self.assertFalse(stopped, "stop_sync must return False when worker thread does not terminate")
            # Worker reference must NOT be discarded
            self.assertIs(self.syncer._sync_thread, hung_thread)
            self.assertTrue(self.syncer.is_running)
        finally:
            hung_event.set()
            hung_thread.join(timeout=1.0)
            self.syncer._sync_thread = None
            self.syncer.stop()

    def test_concurrent_start_and_stop_lifecycle(self):
        """Concurrent calls to start() and stop() do not race, corrupt state, or deadlock."""
        import threading

        errors = []

        def worker_cycle():
            try:
                for _ in range(10):
                    self.syncer.start()
                    self.syncer.stop(timeout=0.5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_cycle) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent lifecycle operations threw: {errors}")
        # Final cleanup
        self.syncer.stop()
        self.assertFalse(self.syncer.is_running)


if __name__ == "__main__":
    unittest.main()

