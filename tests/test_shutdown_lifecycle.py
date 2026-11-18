"""Tests for Vaani Desktop Tracker shutdown lifecycle and worker teardown."""

import logging
import os
import platform
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.app import VaaniTrackerApp, SingleInstanceLock
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.syncer import BackgroundSyncer
from vaani_tracker.models import ActivityBucket
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.ui.tray import VaaniTrayApp
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.power import WindowsPowerNotifier
from vaani_tracker.windows.session import WindowsSessionNotifier


class TestShutdownLifecycle(unittest.TestCase):
    """Verifies all shutdown lifecycle and clean resource termination scenarios."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="test-shutdown-uuid",
            backend_url="http://127.0.0.1:9999",
            sync_interval_seconds=60,
            heartbeat_interval_seconds=30,
        )

    def tearDown(self):
        # Close any handlers referencing temp_dir to allow clean directory removal on Windows
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                if str(self.data_dir).lower() in getattr(h, "baseFilename", "").lower():
                    root.removeHandler(h)
                    h.close()
        self.temp_dir.cleanup()

    def test_scenario_1_tray_exit_invokes_shutdown_exactly_once(self):
        """Scenario 1: Tray Exit invokes shutdown exactly once."""
        on_exit_mock = MagicMock()
        state_machine = MagicMock()
        syncer = MagicMock()
        queue = MagicMock()

        tray = VaaniTrayApp(
            config=self.config,
            state_machine=state_machine,
            syncer=syncer,
            queue=queue,
            on_exit=on_exit_mock,
        )

        tray._on_quit()
        on_exit_mock.assert_called_once()

    def test_scenario_2_repeated_tray_exit_calls_do_not_repeat_shutdown(self):
        """Scenario 2: Repeated tray Exit calls do not repeat shutdown."""
        on_exit_mock = MagicMock()
        state_machine = MagicMock()
        syncer = MagicMock()
        queue = MagicMock()

        tray = VaaniTrayApp(
            config=self.config,
            state_machine=state_machine,
            syncer=syncer,
            queue=queue,
            on_exit=on_exit_mock,
        )

        # Simulate 3 repeated exit requests as observed in physical tests
        tray._on_quit()
        tray._on_quit()
        tray._on_quit()

        self.assertEqual(on_exit_mock.call_count, 1)

        # Actions like manual sync or summary must be ignored during/after exit
        tray._on_sync_now()
        syncer.sync_pending_now.assert_not_called()

    def test_scenario_3_ctrl_c_and_tray_exit_concurrently_do_not_race(self):
        """Scenario 3: Ctrl+C and tray Exit concurrently do not race."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app.start()

        # Track number of times close_current_bucket is invoked
        close_bucket_count = 0
        original_close = app.aggregator.close_current_bucket

        def wrapped_close():
            nonlocal close_bucket_count
            close_bucket_count += 1
            original_close()

        app.aggregator.close_current_bucket = wrapped_close

        errors = []

        def worker_stop():
            try:
                app.stop()
            except Exception as e:
                errors.append(e)

        # Run two concurrent stop invocations simultaneously
        t1 = threading.Thread(target=worker_stop)
        t2 = threading.Thread(target=worker_stop)

        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent stops caused errors: {errors}")
        self.assertFalse(app._running)
        self.assertEqual(close_bucket_count, 1, "Aggregator close should occur exactly once")

    def test_scenario_4_session_notifier_stops_message_loop(self):
        """Scenario 4: Session notifier stops its message loop."""
        notifier = WindowsSessionNotifier()
        notifier.start()

        if platform.system() == "Windows":
            # Give listener thread a brief moment to initialize its message loop
            time.sleep(0.1)
            self.assertTrue(notifier.is_running, "Notifier should be running after start")

        stopped = notifier.stop(timeout=2.0)
        self.assertTrue(stopped, "Notifier stop should return True")
        self.assertFalse(notifier.is_running, "Notifier should not be running after stop")
        if notifier._thread:
            self.assertFalse(notifier._thread.is_alive(), "Message loop thread must be dead")

    def test_scenario_5_power_notifier_stops_message_loop(self):
        """Scenario 5: Power notifier stops its message loop."""
        notifier = WindowsPowerNotifier()
        notifier.start()

        if platform.system() == "Windows":
            time.sleep(0.1)
            self.assertTrue(notifier.is_running, "Power notifier should be running after start")

        stopped = notifier.stop(timeout=2.0)
        self.assertTrue(stopped, "Power notifier stop should return True")
        self.assertFalse(notifier.is_running, "Power notifier should not be running after stop")
        if notifier._thread:
            self.assertFalse(notifier._thread.is_alive(), "Message loop thread must be dead")

    def test_scenario_6_stop_called_before_listener_startup_completes(self):
        """Scenario 6: Stop called before listener startup completes."""
        session_notifier = WindowsSessionNotifier()
        power_notifier = WindowsPowerNotifier()

        # Call stop without calling start
        self.assertTrue(session_notifier.stop(timeout=1.0))
        self.assertTrue(power_notifier.stop(timeout=1.0))

        # Start and immediately stop without delay
        session_notifier.start()
        self.assertTrue(session_notifier.stop(timeout=2.0))

        power_notifier.start()
        self.assertTrue(power_notifier.stop(timeout=2.0))

    def test_scenario_7_listener_startup_failure_cleanup(self):
        """Scenario 7: Listener startup failure cleanup."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        with patch.object(app.session_notifier, "start", side_effect=RuntimeError("Simulated window creation error")):
            result = app.start()
            self.assertFalse(result, "App start must return False on startup exception")
            self.assertFalse(app._running, "App must not remain running after startup failure")
            # Verify lock was released so another instance could acquire
            lock_test = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))
            self.assertTrue(lock_test.acquire(), "Lock must be released following startup failure")
            lock_test.release()

    def test_scenario_8_tray_icon_stop_and_run_loop_termination(self):
        """Scenario 8: Tray icon stop and run-loop termination."""
        tray = VaaniTrayApp(
            config=self.config,
            state_machine=MagicMock(),
            syncer=MagicMock(),
            queue=MagicMock(),
        )

        mock_icon = MagicMock()
        tray._icon = mock_icon

        # Stop should call icon.stop() and mark tray as stopped
        stopped = tray.stop(timeout=1.0)
        self.assertTrue(stopped)
        mock_icon.stop.assert_called_once()
        self.assertTrue(tray._stopped.is_set())
        self.assertIsNone(tray._icon)

    def test_scenario_9_no_background_worker_remains_alive_after_successful_shutdown(self):
        """Scenario 9: No background worker remains alive after successful shutdown."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        started = app.start()
        self.assertTrue(started)

        # Verify threads are running
        self.assertTrue(app._tick_thread.is_alive())
        self.assertTrue(app.syncer._sync_thread.is_alive())
        self.assertTrue(app.syncer._heartbeat_thread.is_alive())

        app.stop()

        # Verify all threads are dead
        self.assertFalse(app._tick_thread is not None and app._tick_thread.is_alive(), "Tick thread still alive")
        self.assertFalse(app.syncer._sync_thread is not None and app.syncer._sync_thread.is_alive(), "Sync thread still alive")
        self.assertFalse(app.syncer._heartbeat_thread is not None and app.syncer._heartbeat_thread.is_alive(), "Heartbeat thread still alive")
        if app.session_notifier._thread:
            self.assertFalse(app.session_notifier._thread.is_alive(), "Session notifier thread still alive")
        if app.power_notifier._thread:
            self.assertFalse(app.power_notifier._thread.is_alive(), "Power notifier thread still alive")

    def test_scenario_12_pending_offline_buckets_remain_stored_in_sqlite(self):
        """Scenario 12: Pending offline buckets remain stored in SQLite."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        queue = app.queue
        app.auth_manager.bind_employee(1, "Test User")
        app.auth_manager.set_token("test_token")

        # Pre-populate SQLite with an offline bucket
        b1 = ActivityBucket(
            bucket_id="bucket-offline-001",
            device_uuid=self.config.device_uuid,
            employee_id=1,
            bucket_start="2026-11-02T10:00:00Z",
            bucket_end="2026-11-02T10:01:00Z",
            active_seconds=45,
            idle_seconds=15,
            session_id=101,
        )
        queue.save_bucket(b1, employee_id=1)
        self.assertEqual(queue.get_pending_count(employee_id=1), 1)

        # Start app, simulate active tracking bucket in progress
        app.start()
        app.state_machine.update_shift_status(MagicMock(is_active=True, session_id=101))
        app.aggregator.tick(elapsed_seconds=10.0)

        # Stop app
        app.stop()

        # Database must still have b1 plus the closed in-progress bucket
        pending = queue.get_pending_buckets(employee_id=1, limit=10)
        self.assertGreaterEqual(len(pending), 1)
        bucket_ids = [b.bucket_id for b in pending]
        self.assertIn("bucket-offline-001", bucket_ids, "Existing offline bucket must be preserved in SQLite")

    def test_scenario_13_app_does_not_log_successful_shutdown_if_worker_remains_alive(self):
        """Scenario 13: Application does not log successful shutdown if worker remains alive."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app.start()

        # Mock syncer.stop to simulate a hung worker
        real_syncer_stop = app.syncer.stop
        try:
            with patch.object(app.syncer, "stop", return_value=False):
                with self.assertLogs(logger="vaani_tracker.app", level="INFO") as cm:
                    app.stop()

                    # "Vaani Desktop Tracker stopped." must NOT appear in logs
                    for msg in cm.output:
                        self.assertNotIn("Vaani Desktop Tracker stopped.", msg)

                    # An error log indicating unstopped components must be present
                    has_error = any("stop completed with unstopped components" in msg for msg in cm.output)
                    self.assertTrue(has_error, f"Expected unstopped components log in {cm.output}")

                    # Single instance lock must NOT be released while workers remain unstopped
                    lock_check = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))
                    self.assertFalse(lock_check.acquire(), "Lock must NOT be released while worker is unstopped")
        finally:
            # Cleanly stop real syncer threads and release lock
            real_syncer_stop()
            app._lock.release()

    def test_notifier_start_returns_false_on_init_failure(self):
        """Scenario 14: Notifier start returns False when initialization raises an error."""
        notifier = WindowsSessionNotifier()

        # Mock _run_message_loop to simulate window creation failure
        def failing_run():
            notifier._init_error = RuntimeError("CreateWindowExW failed (code 1400)")
            notifier._ready_event.set()

        notifier._run_message_loop = failing_run
        started = notifier.start(timeout=1.0)
        self.assertFalse(started, "Notifier start() must return False on init failure")
        self.assertFalse(notifier.is_running)

    def test_syncer_is_running_reflects_actual_thread_state(self):
        """Scenario 15: Syncer is_running returns False if worker threads are dead."""
        syncer = BackgroundSyncer(
            config=self.config,
            api_client=MagicMock(),
            queue=MagicMock(),
            auth_manager=MagicMock(),
            state_machine=MagicMock(),
        )
        self.assertFalse(syncer.is_running)

        syncer.start()
        self.assertTrue(syncer.is_running)

        # Stop workers
        syncer.stop()
        self.assertFalse(syncer.is_running)

    def test_syncer_independent_worker_stop(self):
        """Scenario 16: Syncer sync and heartbeat workers can be stopped independently."""
        syncer = BackgroundSyncer(
            config=self.config,
            api_client=MagicMock(),
            queue=MagicMock(),
            auth_manager=MagicMock(),
            state_machine=MagicMock(),
        )
        syncer.start()
        self.assertTrue(syncer.is_running)

        # Stop sync worker only
        self.assertTrue(syncer.stop_sync(timeout=2.0))
        self.assertIsNone(syncer._sync_thread)
        # Heartbeat worker is still alive, so is_running remains True
        self.assertTrue(syncer.is_running)

        # Stop heartbeat worker
        self.assertTrue(syncer.stop_heartbeat(timeout=2.0))
        self.assertIsNone(syncer._heartbeat_thread)
        self.assertFalse(syncer.is_running)


if __name__ == "__main__":
    unittest.main()
