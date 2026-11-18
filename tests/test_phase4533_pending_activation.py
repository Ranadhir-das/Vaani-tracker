"""Focused regression and integration tests for Phase 4.5.3.3:
Reliable Pending Activation & Foreground PID Hardening.

Covers:
1. Activation arriving before wizard mainloop() stored safely without RuntimeError.
2. Activation arriving before dashboard mainloop() stored safely without RuntimeError.
3. Pending activation consumed after wizard mainloop() begins (genuine Tkinter integration).
4. Pending activation consumed after dashboard mainloop() begins (genuine Tkinter integration).
5. Multiple activation requests during startup consolidated without duplication.
6. Wizard cancellation before pending activation is consumed cleans up state.
7. Shutdown with pending activation clears state without executing against destroyed windows.
8. Activation arriving after event loop is running consumed cleanly (genuine Tkinter integration).
9. Foreground PID derived authoritatively from HWND using GetWindowThreadProcessId.
10. Foreground permission failure does not block message delivery.
11. Broadcast fallback does not authorize an unverified PID from lock file.
12. Stale or destroyed root does not receive restoration callbacks.
"""

import ctypes
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
from vaani_tracker.windows.win32 import HWND_BROADCAST, VAANI_ACTIVATE_MESSAGE


class TestPhase4533PendingActivation(unittest.TestCase):
    """Verifies pending activation mechanism and foreground PID hardening."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.config.auth_token = None
        self.config.registered_employee_id = None
        self.config.device_uuid = "test-phase4533-uuid"
        self._apps = []

    def make_app(self, **kwargs):
        kw = {"config": self.config, "headless": False}
        kw.update(kwargs)
        app = VaaniTrackerApp(**kw)
        self._apps.append(app)
        return app

    def tearDown(self):
        for app in getattr(self, "_apps", []):
            try:
                app.close_logging()
                app.stop()
            except Exception:
                pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # =========================================================================
    # Task A: Pending Activation Tests
    # =========================================================================

    def test_01_activation_arriving_before_wizard_mainloop_stored_safely(self):
        """1. Verifies activation arriving before wizard mainloop() is recorded without error."""
        app = self.make_app()
        app._running = True

        # Activation arrives from Win32 background thread before wizard mainloop starts
        app._handle_external_activation()

        # Must record pending activation in thread-safe state
        self.assertTrue(app._pending_activation)
        self.assertTrue(app.consume_pending_activation())
        self.assertFalse(app._pending_activation)

    def test_02_activation_arriving_before_dashboard_mainloop_stored_safely(self):
        """2. Verifies activation arriving before dashboard mainloop() is recorded without error."""
        app = self.make_app()
        app._running = True

        # Activation arrives before dashboard is created
        app._handle_external_activation()

        self.assertTrue(app._pending_activation)
        self.assertTrue(app.consume_pending_activation())
        self.assertFalse(app.consume_pending_activation())

    def test_03_pending_activation_consumed_after_wizard_mainloop_begins(self):
        """3. Genuine Tkinter integration: Wizard poller consumes pending activation on main thread."""
        app = self.make_app()
        app._running = True

        root = tk.Tk()
        root.withdraw()
        app._wizard_root = root

        restored = []
        def _restore_wizard():
            restored.append(True)

        app._setup_activation_poller(root, _restore_wizard, poll_interval_ms=40)

        # Signal arrives before mainloop starts
        app._handle_external_activation()
        self.assertTrue(app._pending_activation)

        # Enter mainloop briefly and quit
        root.after(100, root.quit)
        root.mainloop()

        self.assertEqual(restored, [True], "Wizard restore function must be executed on main thread")
        self.assertFalse(app._pending_activation, "Pending activation must be cleared after consumption")
        root.destroy()

    def test_04_pending_activation_consumed_after_dashboard_mainloop_begins(self):
        """4. Genuine Tkinter integration: Dashboard poller consumes pending activation on main thread."""
        app = self.make_app()
        app._running = True

        root = tk.Tk()
        root.withdraw()

        restored = []
        app._setup_activation_poller(root, lambda: restored.append(True), poll_interval_ms=40)

        # Signal arrives before dashboard mainloop
        app._handle_external_activation()

        root.after(100, root.quit)
        root.mainloop()

        self.assertEqual(restored, [True], "Dashboard restore function must be executed on main thread")
        self.assertFalse(app._pending_activation)
        root.destroy()

    def test_05_multiple_activation_requests_during_startup(self):
        """5. Genuine Tkinter integration: Multiple rapid activation pings consolidate safely."""
        app = self.make_app()
        app._running = True

        root = tk.Tk()
        root.withdraw()

        restored_count = []
        app._setup_activation_poller(root, lambda: restored_count.append(1), poll_interval_ms=40)

        # 5 rapid pings during startup
        for _ in range(5):
            app._handle_external_activation()

        self.assertTrue(app._pending_activation)

        root.after(100, root.quit)
        root.mainloop()

        self.assertEqual(len(restored_count), 1, "Consolidated into a single clean window restoration")
        self.assertFalse(app._pending_activation)
        root.destroy()

    def test_06_wizard_cancellation_before_pending_activation_consumed(self):
        """6. Verifies wizard cancellation cleans up pending activation and prevents leakage."""
        app = self.make_app()
        app._running = True

        mock_root = MagicMock()
        app._wizard_root = mock_root

        # Activation ping arrives
        app._handle_external_activation()
        self.assertTrue(app._pending_activation)

        # User cancels wizard: root cleaned up, cancellation cleanup called
        app.clear_pending_activation()
        app._wizard_root = None

        self.assertFalse(app.consume_pending_activation(), "Pending activation must be cleanly cleared")
        self.assertIsNone(app._wizard_root)

    def test_07_shutdown_with_pending_activation(self):
        """7. Verifies shutdown cleans up pending activation and exits poller gracefully."""
        app = self.make_app()
        app._running = True

        root = tk.Tk()
        root.withdraw()

        restored = []
        app._setup_activation_poller(root, lambda: restored.append(True), poll_interval_ms=30)

        app._handle_external_activation()
        self.assertTrue(app._pending_activation)

        # Application shutdown begins before poller executes
        app._running = False
        app._shutdown_event.set()
        app.clear_pending_activation()

        root.after(80, root.quit)
        root.mainloop()

        self.assertEqual(restored, [], "Restore must not execute when application is shutting down")
        self.assertFalse(app.consume_pending_activation())
        root.destroy()

    def test_08_activation_after_event_loop_is_running(self):
        """8. Genuine Tkinter integration: Activation arriving while mainloop is active."""
        app = self.make_app()
        app._running = True

        root = tk.Tk()
        root.withdraw()

        restored = []
        app._setup_activation_poller(root, lambda: restored.append(True), poll_interval_ms=30)

        def bg_activation():
            time.sleep(0.04)  # Wait until mainloop is running
            app._handle_external_activation()

        t = threading.Thread(target=bg_activation, daemon=True)
        t.start()

        root.after(150, root.quit)
        root.mainloop()
        t.join(timeout=1.0)

        self.assertEqual(restored, [True], "Activation arriving mid-loop must be consumed by poller")
        self.assertFalse(app._pending_activation)
        root.destroy()

    # =========================================================================
    # Task B: Foreground PID Hardening Tests
    # =========================================================================

    def test_09_foreground_pid_derived_from_actual_hwnd(self):
        """9. Unit test: Foreground target PID is derived from target HWND via GetWindowThreadProcessId."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0x9999
        mock_user32.PostMessageW.return_value = 1
        mock_user32.AllowSetForegroundWindow = MagicMock(return_value=True)

        call_order = []
        def fake_allow(pid):
            call_order.append(("allow", pid))
            return True
        mock_user32.AllowSetForegroundWindow.side_effect = fake_allow

        def fake_post(hwnd, msg, wparam, lparam):
            call_order.append(("post", hwnd))
            return 1
        mock_user32.PostMessageW.side_effect = fake_post

        def fake_get_pid(hwnd, pid_ptr):
            call_order.append(("get_pid", hwnd))
            pid_ptr._obj.value = 54321
            return 1234
        mock_user32.GetWindowThreadProcessId = MagicMock(side_effect=fake_get_pid)

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)

            # GetWindowThreadProcessId called on target HWND
            mock_user32.GetWindowThreadProcessId.assert_called_once()
            # AllowSetForegroundWindow called with authoritative PID derived from HWND
            mock_user32.AllowSetForegroundWindow.assert_called_once_with(54321)
            # AllowSetForegroundWindow called BEFORE PostMessageW
            self.assertEqual(
                call_order,
                [("get_pid", 0x9999), ("allow", 54321), ("post", 0x9999)],
                "AllowSetForegroundWindow must be called BEFORE PostMessageW",
            )

    def test_10_foreground_permission_failure_does_not_block_message_delivery(self):
        """10. Unit test: Failure of AllowSetForegroundWindow does not prevent PostMessageW."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0x9999
        mock_user32.PostMessageW.return_value = 1

        def fake_get_pid(hwnd, pid_ptr):
            pid_ptr._obj.value = 54321
            return 1234
        mock_user32.GetWindowThreadProcessId = MagicMock(side_effect=fake_get_pid)

        # AllowSetForegroundWindow fails (returns FALSE / 0)
        mock_user32.AllowSetForegroundWindow = MagicMock(return_value=0)

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent, "Message delivery must succeed even if AllowSetForegroundWindow fails")
            mock_user32.PostMessageW.assert_called_once_with(0x9999, 0xC042, os.getpid(), 0)

    def test_11_broadcast_fallback_does_not_authorize_unverified_pid(self):
        """11. Unit test: Broadcast fallback does not authorize unverified PID from lock file."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        # Notification window NOT found
        mock_user32.FindWindowExW.return_value = 0
        mock_user32.PostMessageW.return_value = 1
        mock_user32.AllowSetForegroundWindow = MagicMock()

        # Write unverified PID to tracker.lock
        os.makedirs(self.data_dir, exist_ok=True)
        with open(app._lock.lock_path, "w") as f:
            f.write("99999")

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)

            # Broadcast sent
            mock_user32.PostMessageW.assert_called_once_with(HWND_BROADCAST, 0xC042, os.getpid(), 0)
            # AllowSetForegroundWindow must NEVER be called on broadcast fallback
            mock_user32.AllowSetForegroundWindow.assert_not_called()

    def test_12_stale_or_destroyed_root_does_not_receive_restoration_callbacks(self):
        """12. Unit test: Stale or destroyed Tk root does not trigger callbacks or raise."""
        app = self.make_app()
        app._running = True

        mock_root = MagicMock()
        mock_root.winfo_exists.return_value = False  # Destroyed

        restored = []
        app._setup_activation_poller(mock_root, lambda: restored.append(True), poll_interval_ms=40)
        app._handle_external_activation()

        # Simulate poller execution against destroyed root
        poll_fn = mock_root.after.call_args[0][1]
        poll_fn()

        self.assertEqual(restored, [], "Destroyed root must not receive restoration callbacks")

    # =========================================================================
    # Task C: Phase 4.5.3.3.2 Transition & Pure Decoupling Tests
    # =========================================================================

    def test_13_pending_activation_preserved_across_successful_pairing(self):
        """13. Verifies pending activation immediately before pairing success is preserved for dashboard."""
        app = self.make_app()
        app._running = True

        # 1. Wizard is active
        mock_wizard_root = MagicMock()
        mock_wizard_root.winfo_exists.return_value = True
        app._wizard_root = mock_wizard_root

        # 2. Activation ping arrives immediately before pairing finishes
        app._handle_external_activation()
        self.assertTrue(app._pending_activation, "Pending activation must be recorded")

        # 3. Successful pairing: mainloop exits, root cleaned up, but pending activation NOT cleared
        app._wizard_root = None
        app._active_ui_root = None
        app._active_ui_restore_func = None

        # Assert activation is PRESERVED across transition
        self.assertTrue(app._pending_activation, "Pending activation must survive successful pairing transition")

        # 4. Dashboard initializes and attaches its poller
        dash_restored = []
        mock_dash_root = MagicMock()
        mock_dash_root.winfo_exists.return_value = True
        mock_dash = MagicMock()
        mock_dash.show.side_effect = lambda: dash_restored.append(True)
        app.dashboard = mock_dash

        app._setup_activation_poller(mock_dash_root, mock_dash.show, poll_interval_ms=40)

        # Dashboard poller consumes the preserved pending activation
        poll_fn = mock_dash_root.after.call_args[0][1]
        poll_fn()

        self.assertEqual(dash_restored, [True], "Dashboard must consume preserved activation")
        self.assertFalse(app.consume_pending_activation())

    def test_14_activation_during_wizard_to_dashboard_transition_gap(self):
        """14. Verifies activation arriving in the gap between wizard exit and dashboard start is preserved."""
        app = self.make_app()
        app._running = True

        # In the transition gap: wizard is gone, dashboard not yet created
        app._wizard_root = None
        app.dashboard = None
        app._active_ui_root = None
        app._active_ui_restore_func = None

        # Activation ping arrives during background worker startup gap
        app._handle_external_activation()
        self.assertTrue(app._pending_activation)

        # Dashboard starts
        mock_dash_root = MagicMock()
        mock_dash_root.winfo_exists.return_value = True
        mock_dash = MagicMock()
        dash_restored = []
        mock_dash.show.side_effect = lambda: dash_restored.append(True)
        app.dashboard = mock_dash

        app._setup_activation_poller(mock_dash_root, mock_dash.show, poll_interval_ms=40)

        # Poller ticks
        poll_fn = mock_dash_root.after.call_args[0][1]
        poll_fn()

        self.assertEqual(dash_restored, [True])
        self.assertFalse(app.consume_pending_activation())

    def test_15_notification_thread_never_calls_tkinter_apis(self):
        """15. Verifies _handle_external_activation makes zero cross-thread Tkinter calls."""
        app = self.make_app()
        app._running = True

        mock_root = MagicMock()
        app._wizard_root = mock_root
        app._active_ui_root = mock_root

        mock_dash_root = MagicMock()
        mock_dash = MagicMock()
        mock_dash.root = mock_dash_root
        app.dashboard = mock_dash

        # Call activation handler as SessionNotifierThread would
        app._handle_external_activation()

        # Zero Tkinter calls allowed from the notifier thread
        mock_root.after.assert_not_called()
        mock_root.event_generate.assert_not_called()
        mock_dash_root.after.assert_not_called()
        mock_dash_root.event_generate.assert_not_called()
        mock_dash.show.assert_not_called()

        # Activation must be recorded purely in memory
        self.assertTrue(app._pending_activation)


if __name__ == "__main__":
    unittest.main()
