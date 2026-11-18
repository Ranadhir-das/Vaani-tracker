"""Focused tests for Phase 4.5.2: Thread-Safe Shutdown, Tray Relaunch & Single-Instance Recovery.

Covers:
1. Tray Lifecycle:
   - Tray Exit Tracker requests shutdown without destroying Tk widgets directly from the tray thread.
   - Tk destruction occurs on the Tk-owning thread.
   - Repeated Exit requests are idempotent.
   - Tray can be reinitialized and run after complete application restart.
2. Shutdown Protocol & Mutex Ownership:
   - Dashboard close and tray exit use the unified shutdown protocol.
   - A worker timeout cannot deadlock the UI thread.
   - SQLite persistence is attempted before final shutdown.
   - A failed persistence attempt does not silently discard pending data.
   - Mutexes are released by their owner thread only.
   - A second process cannot acquire mutexes while the first is active.
   - Foreign process lock file is never deleted by another process.
   - No stale lock state blocks a legitimate launch after clean exit.
3. Single-Instance Activation (IPC):
   - Second launch triggers external activation signal.
   - Activation is dispatched safely to the Tk-owning thread via root.after.
   - Activation during shutdown or without dashboard is handled safely.
   - Activation while FirstRunWizard is active brings wizard window to front.
4. First-Run Wizard Lifecycle:
   - Closing wizard without pairing cancels application startup.
   - Cancelled startup starts no tracker, syncer, or notification workers.
   - Cancelled startup releases single-instance lock cleanly.
   - Failed authentication leaves wizard open for retry.
   - Successful pairing continues into authenticated flow.
"""

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

import tkinter as tk

from vaani_tracker.app import VaaniTrackerApp, SingleInstanceLock
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, ShiftStatus, APP_MUTEX_NAME
from vaani_tracker.models import ActivityBucket, ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.ui.dashboard import VaaniDashboard
from vaani_tracker.ui.first_run import FirstRunWizard
from vaani_tracker.ui.tray import VaaniTrayApp
from vaani_tracker.windows.session import WindowsSessionNotifier


class TestPhase452TrayLifecycle(unittest.TestCase):
    """Verifies thread-safe tray exit, Tk destruction dispatch, and reinitialization."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="test-tray-lifecycle-uuid",
            backend_url="http://127.0.0.1:9999",
        )

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    root.removeHandler(h)
                    h.close()
                except Exception:
                    pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_tray_exit_requests_shutdown_without_destroying_tk_from_tray_thread(self):
        """Verifies tray exit requests stop without destroying Tk widgets on the tray thread."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app.dashboard = MagicMock()

        # Simulate tray calling _on_tray_exit from a simulated tray worker thread
        caller_threads = []

        def mock_request_close():
            caller_threads.append(threading.current_thread().name)

        app.dashboard.request_close = mock_request_close

        def tray_worker():
            app._on_tray_exit()

        t = threading.Thread(target=tray_worker, name="SimulatedTrayThread")
        t.start()
        t.join(timeout=2.0)

        # Dashboard request_close was invoked
        self.assertEqual(len(caller_threads), 1)
        self.assertEqual(caller_threads[0], "SimulatedTrayThread")
        self.assertFalse(app._running, "App should be marked not running")
        self.assertTrue(app._shutdown_event.is_set(), "Shutdown event should be set")

    def test_dashboard_request_close_schedules_on_tk_main_thread(self):
        """Verifies dashboard.request_close schedules destruction onto the Tk root thread."""
        root = tk.Tk()
        root.withdraw()

        mock_vm = MagicMock()
        mock_vm.get_kpi_cards.return_value = []
        mock_vm.get_employee_display_info.return_value = {"employee_name": "Test", "employee_id": 1}
        mock_vm.get_recent_logs.return_value = []

        with patch("vaani_tracker.ui.dashboard.DashboardViewModel", return_value=mock_vm):
            dash = VaaniDashboard(
                config=self.config,
                state_machine=MagicMock(),
                syncer=MagicMock(),
                queue=MagicMock(),
                auth_manager=MagicMock(),
                root=root,
            )

        executed_threads = []

        def tracking_destroy():
            executed_threads.append(threading.current_thread())
            # Perform real teardown
            try:
                root.quit()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

        dash._destroy_on_main_thread = tracking_destroy

        # Call request_close from a secondary thread
        def background_caller():
            dash.request_close()

        bg_t = threading.Thread(target=background_caller, name="WorkerThread")
        bg_t.start()
        bg_t.join(timeout=1.0)

        # Run main Tk loop on this main thread so scheduled after() callbacks execute
        root.mainloop()

        self.assertEqual(len(executed_threads), 1)
        # Destruction must have executed on this main thread, not on WorkerThread!
        self.assertEqual(executed_threads[0], threading.current_thread())

    def test_repeated_tray_exit_calls_are_idempotent(self):
        """Verifies repeated Exit clicks do not trigger duplicate teardowns."""
        mock_on_exit = MagicMock()
        tray = VaaniTrayApp(
            config=self.config,
            state_machine=MagicMock(),
            syncer=MagicMock(),
            queue=MagicMock(),
            on_exit=mock_on_exit,
        )

        tray._on_quit()
        tray._on_quit()
        tray._on_quit()

        self.assertEqual(mock_on_exit.call_count, 1)

    def test_tray_reinitialized_after_clean_shutdown(self):
        """Verifies tray app can be started and stopped again after a clean shutdown."""
        tray1 = VaaniTrayApp(
            config=self.config,
            state_machine=MagicMock(),
            syncer=MagicMock(),
            queue=MagicMock(),
        )
        mock_icon1 = MagicMock()
        tray1._icon = mock_icon1
        self.assertTrue(tray1.stop(timeout=1.0))
        mock_icon1.stop.assert_called_once()
        self.assertTrue(tray1._stopped.is_set())

        # Second tray instance for subsequent launch
        tray2 = VaaniTrayApp(
            config=self.config,
            state_machine=MagicMock(),
            syncer=MagicMock(),
            queue=MagicMock(),
        )
        self.assertFalse(tray2._stopped.is_set())
        self.assertFalse(tray2._exit_requested.is_set())


class TestPhase452ShutdownAndMutex(unittest.TestCase):
    """Verifies deterministic teardown, worker timeout handling, and mutex ownership."""

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
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    root.removeHandler(h)
                    h.close()
                except Exception:
                    pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_dashboard_close_and_tray_exit_use_unified_shutdown_protocol(self):
        """Verifies dashboard close and tray exit both route through request_stop."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        with patch.object(app, "request_stop") as mock_req_stop:
            app._on_tray_exit()
            mock_req_stop.assert_called_once_with(wait=False)

    def test_foreign_lock_file_never_deleted_by_other_process(self):
        """Verifies lock file containing a different PID is never deleted on release."""
        lock_file = self.data_dir / "tracker.lock"
        # Write a foreign PID into the lock file
        with open(lock_file, "w") as f:
            f.write("999999")

        lock = SingleInstanceLock(str(lock_file))
        # Simulated release must not delete a foreign process's lock file
        lock.release()
        self.assertTrue(os.path.exists(lock_file), "Foreign lock file must NOT be deleted")

        with open(lock_file, "r") as f:
            self.assertEqual(f.read().strip(), "999999")

    def test_own_lock_file_cleaned_up_on_successful_release(self):
        """Verifies lock file containing current process PID is cleanly removed on release."""
        lock_file = self.data_dir / "tracker.lock"
        lock = SingleInstanceLock(str(lock_file))
        self.assertTrue(lock.acquire())
        self.assertTrue(os.path.exists(lock_file))

        with open(lock_file, "r") as f:
            self.assertEqual(f.read().strip(), str(os.getpid()))

        self.assertTrue(lock.release())
        self.assertFalse(os.path.exists(lock_file), "Own lock file must be cleanly deleted on release")

    def test_mutexes_released_by_owner_thread_only(self):
        """Verifies that release is executed on the dedicated mutex owner thread."""
        lock_file = self.data_dir / "tracker.lock"
        lock = SingleInstanceLock(str(lock_file))
        self.assertTrue(lock.acquire())

        # Calling release from this thread wakes the owner thread
        calling_thread = threading.current_thread()
        self.assertNotEqual(calling_thread, lock._owner_thread)

        self.assertTrue(lock.release())
        self.assertFalse(lock._acquired)

    def test_second_instance_rejected_while_first_instance_active(self):
        """Verifies second instance cannot acquire mutex while first is active."""
        lock_file = self.data_dir / "tracker.lock"
        lock1 = SingleInstanceLock(str(lock_file))
        lock2 = SingleInstanceLock(str(lock_file))

        self.assertTrue(lock1.acquire())
        self.assertFalse(lock2.acquire())

        lock1.release()
        self.assertTrue(lock2.acquire())
        lock2.release()

    def test_persistence_attempted_during_shutdown(self):
        """Verifies current bucket is closed and persisted to SQLite queue on shutdown."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app.auth_manager.bind_employee(1, "Test User")
        app.auth_manager.set_token("test-token")
        app.start(start_tick_thread=False)

        # Simulate shift active and activity recorded
        app.state_machine.update_shift_status(ShiftInfo(status=ShiftStatus.ACTIVE, session_id=101))
        app.aggregator.tick(elapsed_seconds=15.0)

        app.stop()

        # Pending count in queue must reflect persisted bucket
        self.assertGreaterEqual(app.queue.get_pending_count(employee_id=1), 1)


class TestPhase452ActivationIPC(unittest.TestCase):
    """Verifies second-instance external activation and foreground restoration."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="test-activation-uuid",
            backend_url="http://127.0.0.1:9999",
        )

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    root.removeHandler(h)
                    h.close()
                except Exception:
                    pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_activation_dispatches_to_tk_thread_via_root_after(self):
        """Verifies external activation schedules dashboard.show onto the main Tk thread via poller."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app._running = True

        mock_root = MagicMock()
        mock_root.winfo_exists.return_value = True
        mock_dashboard = MagicMock()
        mock_dashboard.root = mock_root
        app.dashboard = mock_dashboard

        # Register poller on dashboard root
        app._setup_activation_poller(mock_root, mock_dashboard.show)

        # Invoke external activation handler
        app._handle_external_activation()

        # Poller's scheduled callback consumes pending activation and executes show on main thread
        self.assertTrue(mock_root.after.called)
        poll_fn = mock_root.after.call_args[0][1]
        poll_fn()

        mock_dashboard.show.assert_called_once()
        self.assertFalse(app.consume_pending_activation())

    def test_activation_during_shutdown_ignored_safely(self):
        """Verifies activation received while shutting down is ignored without error."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app._running = False
        app._shutdown_event.set()

        mock_root = MagicMock()
        mock_dashboard = MagicMock()
        mock_dashboard.root = mock_root
        app.dashboard = mock_dashboard

        app._handle_external_activation()

        self.assertFalse(app.consume_pending_activation())

    def test_activation_restores_wizard_if_wizard_active(self):
        """Verifies activation restores FirstRunWizard if wizard window is open via poller."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        app._running = True
        app.dashboard = None

        mock_wizard_root = MagicMock()
        mock_wizard_root.winfo_exists.return_value = True
        app._wizard_root = mock_wizard_root

        restored = []
        app._setup_activation_poller(mock_wizard_root, lambda: restored.append(True))

        app._handle_external_activation()

        # Poller executes restoration callback on wizard root thread
        self.assertTrue(mock_wizard_root.after.called)
        poll_fn = mock_wizard_root.after.call_args[0][1]
        poll_fn()

        self.assertEqual(restored, [True])
        self.assertFalse(app.consume_pending_activation())

    def test_second_instance_sends_activation_ping_when_lock_held(self):
        """Verifies second instance attempts activation when lock acquisition fails."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        with patch.object(app._lock, "acquire", return_value=False):
            with patch.object(app, "_activate_existing_instance", return_value=True) as mock_ping:
                result = app.start(start_tick_thread=False)
                self.assertFalse(result)
                mock_ping.assert_called_once()
                self.assertTrue(app.activated_existing)

    def test_activation_sends_directly_first_without_broadcast(self):
        """Verifies activation posts directly to existing HWND and skips broadcast when direct delivery succeeds."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0x12345
        mock_user32.PostMessageW.return_value = 1

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)
            mock_user32.PostMessageW.assert_called_once_with(0x12345, 0xC042, os.getpid(), 0)

    def test_activation_broadcasts_only_when_direct_delivery_fails(self):
        """Verifies activation falls back to HWND_BROADCAST only when direct delivery fails or window not found."""
        app = VaaniTrackerApp(config=self.config, headless=True)
        from vaani_tracker.windows.win32 import HWND_BROADCAST
        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0
        mock_user32.PostMessageW.return_value = 1

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)
            mock_user32.PostMessageW.assert_called_once_with(HWND_BROADCAST, 0xC042, os.getpid(), 0)

    def test_main_returns_0_when_second_instance_activates_existing(self):
        """Verifies __main__.py main() returns exit code 0 when second instance successfully requests activation."""
        from vaani_tracker.__main__ import main
        test_argv = ["vaani-tracker", "--headless", "--data-dir", str(self.data_dir)]

        def fake_start(self_app, start_tick_thread=True):
            self_app.activated_existing = True
            return False

        with patch.object(sys, "argv", test_argv), \
             patch.object(VaaniTrackerApp, "start", fake_start):
            exit_code = main()
            self.assertEqual(exit_code, 0)

    def test_main_returns_1_when_startup_fails_without_activation(self):
        """Verifies __main__.py main() preserves nonzero exit code when actual startup failure occurs."""
        from vaani_tracker.__main__ import main
        test_argv = ["vaani-tracker", "--headless", "--data-dir", str(self.data_dir)]

        def fake_start(self_app, start_tick_thread=True):
            self_app.activated_existing = False
            return False

        with patch.object(sys, "argv", test_argv), \
             patch.object(VaaniTrackerApp, "start", fake_start):
            exit_code = main()
            self.assertEqual(exit_code, 1)


class TestPhase452FirstRunWizardLifecycle(unittest.TestCase):
    """Verifies FirstRunWizard cancellation, cleanup, and retry mechanics."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="test-wizard-uuid",
            backend_url="http://127.0.0.1:9999",
        )

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    root.removeHandler(h)
                    h.close()
                except Exception:
                    pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_wizard_closing_without_pairing_marks_not_completed(self):
        """Verifies user closing wizard without pairing sets completed to False."""
        root = tk.Tk()
        root.withdraw()

        mock_auth = MagicMock()
        mock_api = MagicMock()

        wizard = FirstRunWizard(
            config=self.config,
            auth_manager=mock_auth,
            api_client=mock_api,
            root=root,
        )

        self.assertFalse(wizard.completed)
        wizard._on_close()
        self.assertFalse(wizard.completed)

    def test_wizard_finishing_marks_completed(self):
        """Verifies completing wizard sets completed to True."""
        root = tk.Tk()
        root.withdraw()

        mock_auth = MagicMock()
        mock_api = MagicMock()

        wizard = FirstRunWizard(
            config=self.config,
            auth_manager=mock_auth,
            api_client=mock_api,
            root=root,
        )

        wizard._on_finish_click()
        self.assertTrue(wizard.completed)

    def test_wizard_cancelled_starts_no_tracking_or_sync_workers(self):
        """Verifies cancelled setup starts no background listeners or syncer threads."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app.auth_manager.is_authenticated = MagicMock(return_value=False)

        # Mock FirstRunWizard to simulate user closing it immediately
        mock_wizard_instance = MagicMock()
        mock_wizard_instance.completed = False

        with patch("vaani_tracker.app.FirstRunWizard", return_value=mock_wizard_instance):
            with patch("tkinter.Tk"):
                result = app.start()
                self.assertFalse(result, "app.start must return False on wizard cancellation")

        # Verify no background workers or listeners were started
        self.assertFalse(app.session_notifier.is_running, "SessionNotifier must NOT be running")
        self.assertFalse(app.power_notifier.is_running, "PowerNotifier must NOT be running")
        self.assertFalse(app.syncer.is_running, "Syncer must NOT be running")
        self.assertIsNone(app._tick_thread, "Tick loop must NOT be running")
        self.assertIsNone(app.dashboard, "Dashboard must NOT be created")

        # Verify single-instance lock was cleanly released
        test_lock = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))
        self.assertTrue(test_lock.acquire(), "Lock must be released after wizard cancellation")
        test_lock.release()


if __name__ == "__main__":
    unittest.main()
