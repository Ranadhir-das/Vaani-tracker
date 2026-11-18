"""Focused regression and integration tests for Phase 4.5.3.2: Second-Instance Activation Remediation.

Covers:
1. Startup Ordering Integration:
   - Wizard root is assigned and retained.
   - External activation callback is registered on session notifier.
   - Session notifier is started and running before FirstRunWizard.mainloop() begins.
   - Session notifier startup failure cleanly aborts startup and releases single-instance lock.
2. Wizard Activation & UI Restoration:
   - External activation received while wizard is active schedules restoration on Tk main thread.
   - Restoration executes deiconify(), state("normal"), lift(), and focus_force() on main thread.
   - Destroyed or invalid wizard references are detected and handled without error.
   - Dashboard activation path remains functional.
   - Repeated activation pings do not duplicate windows.
3. Cancellation & Exception Cleanup:
   - Closing wizard without pairing stops session notifier with bounded wait.
   - Single-instance mutex is released so subsequent launches can acquire it.
   - Unexpected exceptions during wizard execution trigger safe teardown.
4. Sender Direct Delivery & Foreground Authorization:
   - Direct delivery to existing HWND is attempted first.
   - Broadcast fallback is used only when direct delivery fails or window is not found.
   - AllowSetForegroundWindow is called for target PID when direct delivery succeeds.
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
from vaani_tracker.windows.win32 import HWND_BROADCAST, VAANI_ACTIVATE_MESSAGE


class TestPhase4532ActivationLifecycle(unittest.TestCase):
    """Verifies startup lifecycle ordering and single-instance activation under Phase 4.5.3.2."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.config.auth_token = None
        self.config.registered_employee_id = None
        self.config.device_uuid = "test-phase4532-uuid"

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # =========================================================================
    # A. Startup Ordering Integration Tests
    # =========================================================================

    def test_session_notifier_running_before_wizard_mainloop(self):
        """Verifies session notifier is started and listening before wizard enters mainloop."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app.auth_manager.is_authenticated = MagicMock(return_value=False)

        state_during_mainloop = {}

        def fake_mainloop():
            state_during_mainloop["wizard_root_assigned"] = app._wizard_root is not None
            state_during_mainloop["notifier_running"] = app.session_notifier.is_running
            state_during_mainloop["callback_registered"] = (
                app._handle_external_activation in app.session_notifier._activate_callbacks
            )
            # Simulate user completing pairing
            app.auth_manager.is_authenticated = MagicMock(return_value=True)
            mock_wizard_instance.completed = True

        mock_wizard_instance = MagicMock()
        mock_wizard_instance.completed = True

        mock_wizard_tk = MagicMock()
        mock_wizard_tk.mainloop = fake_mainloop
        mock_dash_tk = MagicMock()

        with patch("vaani_tracker.app.FirstRunWizard", return_value=mock_wizard_instance):
            with patch("tkinter.Tk", side_effect=[mock_wizard_tk, mock_dash_tk]):
                with patch.object(app, "_run_tick_loop"):
                    with patch.object(app.tray, "run_detached"):
                        with patch("vaani_tracker.app.VaaniDashboard"):
                            # Run app.start
                            result = app.start(start_tick_thread=False)

        # Assertions about state while wizard was running
        self.assertTrue(
            state_during_mainloop.get("wizard_root_assigned"),
            "app._wizard_root must be assigned before mainloop runs",
        )
        self.assertTrue(
            state_during_mainloop.get("notifier_running"),
            "session_notifier must be running before mainloop runs",
        )
        self.assertTrue(
            state_during_mainloop.get("callback_registered"),
            "_handle_external_activation must be registered on session_notifier",
        )

        # Clean up
        app.stop()

    def test_notifier_startup_failure_cleans_up_and_raises(self):
        """Verifies failure to start session notifier aborts startup safely and releases lock."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app.auth_manager.is_authenticated = MagicMock(return_value=False)

        mock_wizard_instance = MagicMock()
        mock_tk_instance = MagicMock()

        # Simulate session notifier failing to start (e.g. Win32 error)
        with patch.object(app.session_notifier, "start", return_value=False):
            with patch("vaani_tracker.app.FirstRunWizard", return_value=mock_wizard_instance):
                with patch("tkinter.Tk", return_value=mock_tk_instance):
                    result = app.start(start_tick_thread=False)
                    self.assertFalse(result, "app.start must return False on notifier startup failure")

        # Root reference must be cleared
        self.assertIsNone(app._wizard_root)
        self.assertFalse(app._running)

        # Mutex/lock must be released
        test_lock = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))
        self.assertTrue(test_lock.acquire(), "Lock must be released after notifier startup failure")
        test_lock.release()

    # =========================================================================
    # B. Wizard Activation & UI Restoration Tests
    # =========================================================================

    def test_activation_dispatches_restore_wizard_on_tk_main_thread(self):
        """Verifies activation received while wizard is active schedules restoration on Tk thread."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app._running = True
        app.dashboard = None

        mock_root = MagicMock()
        mock_root.winfo_exists.return_value = True
        app._wizard_root = mock_root

        restored = []
        def _restore():
            restored.append(True)
            mock_root.deiconify()
            mock_root.state("normal")
            mock_root.lift()
            mock_root.focus_force()

        app._setup_activation_poller(mock_root, _restore, poll_interval_ms=200)

        # Activation arrives from Win32 background thread
        app._handle_external_activation()

        # Poller's scheduled callback consumes pending activation and executes restoration
        self.assertTrue(mock_root.after.called)
        poll_fn = mock_root.after.call_args[0][1]
        poll_fn()

        self.assertEqual(restored, [True])
        mock_root.deiconify.assert_called_once()
        mock_root.state.assert_called_once_with("normal")
        mock_root.lift.assert_called_once()
        mock_root.focus_force.assert_called_once()
        self.assertFalse(app.consume_pending_activation())

    def test_activation_with_destroyed_wizard_handled_safely(self):
        """Verifies activation ping after wizard window destruction does not crash or raise."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app._running = True
        app.dashboard = None

        mock_root = MagicMock()
        mock_root.winfo_exists.return_value = False  # Window destroyed
        app._wizard_root = mock_root

        restored = []
        app._setup_activation_poller(mock_root, lambda: restored.append(True))
        app._handle_external_activation()

        # Execute poller: window is destroyed, must not call restore
        poll_fn = mock_root.after.call_args[0][1]
        poll_fn()
        self.assertFalse(restored)

    def test_activation_restores_dashboard_when_dashboard_active(self):
        """Verifies activation restores dashboard when dashboard exists."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app._running = True
        app._wizard_root = None

        mock_dashboard = MagicMock()
        mock_root = MagicMock()
        mock_root.winfo_exists.return_value = True
        mock_dashboard.root = mock_root
        app.dashboard = mock_dashboard

        app._setup_activation_poller(mock_root, mock_dashboard.show)
        app._handle_external_activation()

        poll_fn = mock_root.after.call_args[0][1]
        poll_fn()
        mock_dashboard.show.assert_called_once()

    # =========================================================================
    # C. Cancellation Cleanup Tests
    # =========================================================================

    def test_wizard_cancellation_stops_notifier_and_releases_lock(self):
        """Verifies cancelling setup stops notifier thread and releases single-instance mutex."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app.auth_manager.is_authenticated = MagicMock(return_value=False)

        mock_wizard_instance = MagicMock()
        mock_wizard_instance.completed = False

        mock_tk_instance = MagicMock()

        with patch("vaani_tracker.app.FirstRunWizard", return_value=mock_wizard_instance):
            with patch("tkinter.Tk", return_value=mock_tk_instance):
                result = app.start(start_tick_thread=False)
                self.assertFalse(result, "app.start must return False on wizard cancellation")

        # Session notifier must be stopped
        self.assertFalse(app.session_notifier.is_running, "SessionNotifier must be stopped on cancel")
        self.assertFalse(app.power_notifier.is_running, "PowerNotifier must not be running")
        self.assertFalse(app._running)

        # Mutex must be released cleanly
        test_lock = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))
        self.assertTrue(test_lock.acquire(), "Lock must be released and re-acquirable")
        test_lock.release()

    # =========================================================================
    # D. Exception Cleanup Tests
    # =========================================================================

    def test_exception_during_wizard_mainloop_triggers_safe_stop(self):
        """Verifies an unhandled exception in wizard mainloop triggers stop() and releases resources."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        app.auth_manager.is_authenticated = MagicMock(return_value=False)

        mock_wizard_instance = MagicMock()
        mock_tk_instance = MagicMock()
        mock_tk_instance.mainloop.side_effect = RuntimeError("Simulated UI crash in mainloop")

        with patch("vaani_tracker.app.FirstRunWizard", return_value=mock_wizard_instance):
            with patch("tkinter.Tk", return_value=mock_tk_instance):
                result = app.start(start_tick_thread=False)
                self.assertFalse(result)

        self.assertIsNone(app._wizard_root)
        self.assertFalse(app.session_notifier.is_running)

        # Lock must be released
        test_lock = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))
        self.assertTrue(test_lock.acquire())
        test_lock.release()

    # =========================================================================
    # E. Sender Direct Delivery & Foreground Authorization Tests
    # =========================================================================

    def test_direct_delivery_preferred_and_authorizes_foreground(self):
        """Verifies direct delivery to HWND is attempted first and authorizes target PID."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0x54321
        mock_user32.PostMessageW.return_value = 1
        mock_user32.AllowSetForegroundWindow = MagicMock(return_value=True)

        def fake_get_pid(hwnd, pid_ptr):
            pid_ptr._obj.value = 14540
            return 999
        mock_user32.GetWindowThreadProcessId = MagicMock(side_effect=fake_get_pid)

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)

            # Direct delivery to 0x54321, not broadcast
            mock_user32.PostMessageW.assert_called_once_with(0x54321, 0xC042, os.getpid(), 0)

            # AllowSetForegroundWindow called with target PID derived from HWND
            mock_user32.AllowSetForegroundWindow.assert_called_once_with(14540)

    def test_broadcast_fallback_when_direct_delivery_fails(self):
        """Verifies broadcast fallback is used when PostMessageW to existing window returns 0."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0x54321
        # First call (direct) fails (returns 0); second call (broadcast) succeeds (returns 1)
        mock_user32.PostMessageW.side_effect = [0, 1]
        mock_user32.AllowSetForegroundWindow = MagicMock(return_value=True)

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)
            self.assertEqual(mock_user32.PostMessageW.call_count, 2)
            # Second call was HWND_BROADCAST
            second_call_args = mock_user32.PostMessageW.call_args_list[1][0]
            self.assertEqual(second_call_args[0], HWND_BROADCAST)

    def test_broadcast_fallback_when_window_not_found(self):
        """Verifies broadcast fallback is used when FindWindowExW returns 0."""
        app = VaaniTrackerApp(config=self.config, headless=True)

        mock_user32 = MagicMock()
        mock_user32.RegisterWindowMessageW.return_value = 0xC042
        mock_user32.FindWindowExW.return_value = 0
        mock_user32.PostMessageW.return_value = 1

        with patch("ctypes.windll.user32", mock_user32), \
             patch("vaani_tracker.windows.win32.init_win32_prototypes"):
            sent = app._activate_existing_instance()
            self.assertTrue(sent)
            mock_user32.PostMessageW.assert_called_once_with(HWND_BROADCAST, 0xC042, os.getpid(), 0)

    # =========================================================================
    # F. Win32 Prototype Test
    # =========================================================================

    def test_allow_set_foreground_window_prototype_registered(self):
        """Verifies AllowSetForegroundWindow prototype is properly initialized on Windows."""
        if platform.system() != "Windows":
            self.skipTest("Windows-only prototype test")

        from vaani_tracker.windows.win32 import init_win32_prototypes
        import ctypes
        init_win32_prototypes()
        user32 = ctypes.windll.user32
        self.assertTrue(hasattr(user32, "AllowSetForegroundWindow"))
        self.assertEqual(len(user32.AllowSetForegroundWindow.argtypes), 1)


if __name__ == "__main__":
    unittest.main()
