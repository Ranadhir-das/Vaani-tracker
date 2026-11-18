"""Unit and regression tests for Windows lifecycle, shutdown safety, DPI awareness, and mutex coordination."""

import logging
import os
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vaani_tracker.app import VaaniTrackerApp, SingleInstanceLock
from vaani_tracker.config import TrackerConfig
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.constants import (
    TrackerState,
    ShiftStatus,
    APP_MUTEX_NAME,
    WM_QUERYENDSESSION,
    WM_ENDSESSION,
)
from vaani_tracker.models import ShiftInfo
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.win32 import (
    enable_high_dpi_awareness,
    reset_dpi_state,
)


def _cleanup_logging_handlers(target_path: Path):
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            base_fn = getattr(h, "baseFilename", "")
            if str(target_path).lower() in base_fn.lower():
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass


class TestWindowsShutdownLifecycle(unittest.TestCase):
    """Verifies safe handling of WM_QUERYENDSESSION and WM_ENDSESSION."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="test-shutdown-uuid-43",
            backend_url="http://127.0.0.1:9999",
            sync_interval_seconds=60,
            heartbeat_interval_seconds=30,
        )

    def tearDown(self):
        _cleanup_logging_handlers(self.data_dir)
        self.temp_dir.cleanup()

    def test_query_end_session_persists_bucket_locally(self):
        """WM_QUERYENDSESSION immediately flushes in-memory activity to SQLite."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")

        # Set shift active and accumulate 15 seconds of work
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(15):
            app.aggregator.tick(1.0)

        # Trigger WM_QUERYENDSESSION
        res = app._handle_query_end_session()
        self.assertTrue(res, "WM_QUERYENDSESSION handler must return True")

        # Bucket must now be persisted in the local queue
        pending = app.queue.get_pending_buckets(employee_id=101, limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].active_seconds + pending[0].idle_seconds, 15)
        self.assertEqual(pending[0].employee_id, 101)

        # Clean shutdown
        app.stop()

    def test_query_end_session_does_not_trigger_network_sync(self):
        """WM_QUERYENDSESSION must not initiate network sync or wait indefinitely."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.syncer = MagicMock()

        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(10):
            app.aggregator.tick(1.0)

        res = app._handle_query_end_session()
        self.assertTrue(res)

        # Syncer sync_pending_now should NOT be called by query_end_session
        app.syncer.sync_pending_now.assert_not_called()
        app.stop()

    def test_query_end_session_repeated_calls_prevent_duplicate_buckets(self):
        """Repeated WM_QUERYENDSESSION messages must not persist duplicate buckets."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")

        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(20):
            app.aggregator.tick(1.0)

        # First query flushes the 20s bucket
        app._handle_query_end_session()
        # Second immediate query sees empty accumulators
        app._handle_query_end_session()

        pending = app.queue.get_pending_buckets(employee_id=101, limit=10)
        self.assertEqual(len(pending), 1, "Exactly one bucket should be saved despite repeated queries")
        app.stop()

    def test_query_end_session_handles_persistence_error_by_vetoing_shutdown(self):
        """If persistence raises an error, WM_QUERYENDSESSION returns False to prevent data loss."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.aggregator.close_current_bucket = MagicMock(side_effect=RuntimeError("SQLite disk I/O failure"))

        # Must return False to veto shutdown so Windows does not destroy in-memory activity
        res = app._handle_query_end_session()
        self.assertFalse(res, "Must return False (veto shutdown) when persistence fails")
        app.stop()

    def test_end_session_with_confirmed_termination_requests_async_shutdown(self):
        """WM_ENDSESSION with is_ending=True initiates asynchronous shutdown handoff."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.request_stop = MagicMock()

        app._handle_end_session(is_ending=True, lparam=0)
        app.request_stop.assert_called_once_with(wait=False)

    def test_end_session_without_confirmed_termination_continues_running(self):
        """WM_ENDSESSION with is_ending=False (aborted shutdown) does NOT request shutdown."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.request_stop = MagicMock()

        app._handle_end_session(is_ending=False, lparam=0)
        app.request_stop.assert_not_called()

    def test_end_session_repeated_calls_are_idempotent(self):
        """Repeated WM_ENDSESSION calls do not corrupt state or trigger multiple stops."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.start(start_tick_thread=False)

        # Send confirmed end session twice
        app._handle_end_session(is_ending=True, lparam=0)
        app._handle_end_session(is_ending=True, lparam=0)

        self.assertFalse(app._running)
        self.assertTrue(app._shutdown_event.is_set())
        if app._teardown_thread:
            app._teardown_thread.join(timeout=3.0)

    def test_end_session_returns_immediately_without_waiting_for_worker_shutdown(self):
        """WM_ENDSESSION returns promptly while teardown continues on coordinator thread."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.start(start_tick_thread=False)

        # Simulate slow worker teardown (e.g. syncer waiting for network timeout)
        slow_event = threading.Event()
        real_syncer_stop = app.syncer.stop

        def slow_syncer_stop(timeout=None):
            slow_event.wait(timeout=1.0)
            return real_syncer_stop(timeout=timeout)

        app.syncer.stop = slow_syncer_stop

        t0 = time.monotonic()
        app._handle_end_session(is_ending=True, lparam=0)
        elapsed = time.monotonic() - t0

        # The message handler must return in well under 0.2s despite slow worker
        self.assertLess(elapsed, 0.2, f"Handler blocked for {elapsed:.3f}s")
        self.assertTrue(app._teardown_started)

        # Allow teardown thread to finish and cleanly stop real workers
        slow_event.set()
        if app._teardown_thread:
            app._teardown_thread.join(timeout=3.0)

    def test_end_session_repeated_messages_do_not_spawn_duplicate_coordinators(self):
        """Repeated WM_ENDSESSION calls spawn only a single coordinator thread."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.start(start_tick_thread=False)

        # Hold teardown thread in execution
        block_event = threading.Event()
        real_execute = app._execute_teardown
        execute_calls = []

        def wrapped_execute():
            execute_calls.append(True)
            block_event.wait(timeout=2.0)
            real_execute()

        app._execute_teardown = wrapped_execute

        # Send three consecutive end session messages
        app._handle_end_session(is_ending=True, lparam=0)
        first_thread = app._teardown_thread
        app._handle_end_session(is_ending=True, lparam=0)
        app._handle_end_session(is_ending=True, lparam=0)

        # Must not replace or spawn multiple threads
        self.assertIs(app._teardown_thread, first_thread)
        block_event.set()
        first_thread.join(timeout=3.0)
        self.assertEqual(len(execute_calls), 1, "Teardown must execute exactly once")

    def test_query_end_session_lock_timeout_handles_contention(self):
        """When aggregator lock is held by another thread, close_current_bucket with lock_timeout raises TimeoutError and preserves state."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(10):
            app.aggregator.tick(1.0)

        # Deliberately acquire aggregator lock on a background thread to simulate real lock contention
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with app.aggregator._lock:
                lock_held.set()
                release_lock.wait()

        contender = threading.Thread(target=hold_lock, daemon=True)
        contender.start()
        lock_held.wait()

        try:
            # Invoking with a small lock timeout from main thread should raise TimeoutError without resetting accumulators
            with self.assertRaises(TimeoutError):
                app.aggregator.close_current_bucket(lock_timeout=0.05)
            # Accumulators must remain preserved
            self.assertGreater(app.aggregator._active_seconds_float + app.aggregator._idle_seconds_float, 0.0)
        finally:
            release_lock.set()
            contender.join(timeout=1.0)

        app.stop()

    def test_query_end_session_retains_accumulators_on_sqlite_error(self):
        """If SQLite write fails during WM_QUERYENDSESSION, accumulators are retained and shutdown is vetoed."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(12):
            app.aggregator.tick(1.0)

        initial_raw = app.aggregator._active_seconds_float + app.aggregator._idle_seconds_float
        self.assertAlmostEqual(initial_raw, 12.0)

        # Mock queue save_bucket to fail
        with patch.object(app.queue, "save_bucket", side_effect=sqlite3.OperationalError("database is locked")):
            res = app._handle_query_end_session()
            # Must return False (veto shutdown) so Windows does not terminate with unsaved in-memory activity
            self.assertFalse(res, "Must return False to veto shutdown when SQLite persistence fails")
            # Accumulators must NOT have been reset
            retained_raw = app.aggregator._active_seconds_float + app.aggregator._idle_seconds_float
            self.assertAlmostEqual(retained_raw, 12.0)

        app.stop()

    def test_query_end_session_empty_bucket_allows_shutdown(self):
        """When no activity exists in memory, WM_QUERYENDSESSION returns True to allow shutdown."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        res = app._handle_query_end_session()
        self.assertTrue(res, "Empty in-memory bucket must permit shutdown")
        app.stop()

    def test_query_end_session_queue_lock_timeout_vetoes_shutdown(self):
        """Queue lock timeout during WM_QUERYENDSESSION returns False and preserves accumulators."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(8):
            app.aggregator.tick(1.0)

        with patch.object(app.queue, "save_bucket", side_effect=TimeoutError("ActivityQueue lock acquisition timed out")):
            res = app._handle_query_end_session()
            self.assertFalse(res, "Queue lock timeout must veto shutdown")
            # Accumulators remain preserved
            retained_raw = app.aggregator._active_seconds_float + app.aggregator._idle_seconds_float
            self.assertAlmostEqual(retained_raw, 8.0)

        app.stop()

    def test_query_end_session_retry_after_failure_persists_without_duplication(self):
        """Failed WM_QUERYENDSESSION followed by successful retry persists bucket cleanly without duplicate."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(15):
            app.aggregator.tick(1.0)

        # First query fails due to locked database
        with patch.object(app.queue, "save_bucket", side_effect=sqlite3.OperationalError("database is locked")):
            res1 = app._handle_query_end_session()
            self.assertFalse(res1, "First query should fail and veto shutdown")

        # Second query succeeds (e.g. database contention cleared)
        res2 = app._handle_query_end_session()
        self.assertTrue(res2, "Second query retry must succeed and permit shutdown")

        pending = app.queue.get_pending_buckets(employee_id=101, limit=10)
        self.assertEqual(len(pending), 1, "Exactly one bucket should be saved across retry")
        self.assertEqual(pending[0].active_seconds + pending[0].idle_seconds, 15)

        app.stop()

    def test_query_end_session_timings_measured_and_bounded(self):
        """Measures WM_QUERYENDSESSION execution duration under normal and contested conditions."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        for _ in range(10):
            app.aggregator.tick(1.0)

        # 1. Normal duration: measured in single-digit milliseconds
        t0 = time.monotonic()
        res = app._handle_query_end_session()
        normal_duration = time.monotonic() - t0
        self.assertTrue(res)
        self.assertLess(normal_duration, 0.5, f"Normal query took {normal_duration:.4f}s; expected < 0.5s")

        # 2. Contended duration: bounded by lock_timeout
        for _ in range(10):
            app.aggregator.tick(1.0)

        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with app.aggregator._lock:
                lock_held.set()
                release_lock.wait()

        contender = threading.Thread(target=hold_lock, daemon=True)
        contender.start()
        lock_held.wait()

        try:
            t0 = time.monotonic()
            with patch("vaani_tracker.app.SHUTDOWN_LOCK_TIMEOUT_SECONDS", 0.1):
                res_contended = app._handle_query_end_session()
            contended_duration = time.monotonic() - t0

            self.assertFalse(res_contended, "Contended query must veto shutdown")
            self.assertLess(contended_duration, 0.4, f"Contended query took {contended_duration:.4f}s; expected < 0.4s")
        finally:
            release_lock.set()
            contender.join(timeout=1.0)

        app.stop()

    def test_end_session_aggregator_stop_bounded_under_lock_contention(self):
        """Verifies that aggregator.stop() does not block indefinitely when lock is contested."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)

        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with app.aggregator._lock:
                lock_held.set()
                release_lock.wait()

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        lock_held.wait()

        try:
            t0 = time.monotonic()
            stopped = app.aggregator.stop(timeout=0.1)
            duration = time.monotonic() - t0
            self.assertFalse(stopped)
            self.assertTrue(app.aggregator._is_stopped, "Stop flag must be set even if lock timed out")
            self.assertLess(duration, 0.3, f"stop() blocked for {duration:.4f}s; expected < 0.3s")
        finally:
            release_lock.set()
            t.join(timeout=1.0)

        app.stop()

    def test_query_end_session_concurrent_with_tick_loop(self):
        """WM_QUERYENDSESSION flushes cleanly while tick thread is actively running."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.auth_manager.register_and_bind(token="test-token", employee_id=101, employee_name="Test Employee")
        app.state_machine.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        # Accumulate 5 seconds of work
        for _ in range(5):
            app.aggregator.tick(1.0)

        app.start(start_tick_thread=True)

        # Trigger query while tick thread is actively running
        res = app._handle_query_end_session()
        self.assertTrue(res)

        time.sleep(0.1)
        app.stop()

        pending = app.queue.get_pending_buckets(employee_id=101, limit=10)
        self.assertGreaterEqual(len(pending), 1)

    def test_sqlite_queue_busy_timeout_configured(self):
        """Verifies that ActivityQueue applies finite busy timeout to SQLite connections."""
        queue = ActivityQueue(self.data_dir / "test_timeout.db", busy_timeout_ms=2500)
        self.assertEqual(queue.busy_timeout_ms, 2500)

        conn = queue._get_connection()
        try:
            row = conn.execute("PRAGMA busy_timeout;").fetchone()
            self.assertEqual(row[0], 2500)
        finally:
            conn.close()

    def test_normal_ui_shutdown_and_windows_shutdown_coordinate_safely(self):
        """UI stop while async teardown is running joins cleanly without double-execution."""
        app = VaaniTrackerApp(config=self.config, headless=True, enable_app_mutex=False)
        app.start(start_tick_thread=False)

        # Trigger async teardown as if WM_ENDSESSION arrived
        app._handle_end_session(is_ending=True, lparam=0)

        # Concurrently, UI or signal handler calls stop()
        app.stop(timeout=5.0)

        self.assertFalse(app._running)
        self.assertTrue(app._shutdown_event.is_set())
        self.assertTrue(app._teardown_completed)

    def test_session_notifier_triggers_query_and_end_session_callbacks(self):
        """Verifies that WindowsSessionNotifier dispatches query and end session events."""
        notifier = WindowsSessionNotifier()
        query_called = []
        end_called = []

        notifier.on_query_end_session(lambda: query_called.append(True))
        notifier.on_end_session(lambda is_ending: end_called.append(is_ending))

        can_end = notifier.trigger_query_end_session()
        self.assertTrue(can_end)
        self.assertEqual(len(query_called), 1)

        notifier.trigger_end_session(is_ending=True)
        self.assertEqual(end_called, [True])

        notifier.trigger_end_session(is_ending=False)
        self.assertEqual(end_called, [True, False])


class TestSingleInstanceAndAppMutex(unittest.TestCase):
    """Verifies dual-mutex single-instance and installer coordination."""

    def setUp(self):
        self.temp_dir1 = tempfile.TemporaryDirectory()
        self.temp_dir2 = tempfile.TemporaryDirectory()
        self.lock_path1 = Path(self.temp_dir1.name) / "test.lock"
        self.lock_path2 = Path(self.temp_dir2.name) / "test.lock"
        self.test_app_mutex = f"Local\\VaaniTestMutex_{os.getpid()}_{int(time.time())}"

    def tearDown(self):
        _cleanup_logging_handlers(Path(self.temp_dir1.name))
        _cleanup_logging_handlers(Path(self.temp_dir2.name))
        self.temp_dir1.cleanup()
        self.temp_dir2.cleanup()

    def test_single_instance_without_app_mutex_retains_isolation(self):
        """Two locks on different paths without app_mutex both acquire successfully."""
        lock1 = SingleInstanceLock(str(self.lock_path1))
        lock2 = SingleInstanceLock(str(self.lock_path2))

        self.assertTrue(lock1.acquire())
        self.assertTrue(lock2.acquire(), "Different lock paths without app_mutex must acquire independently")

        lock1.release()
        lock2.release()

    def test_app_mutex_prevents_duplicate_instance_across_paths(self):
        """Two locks on different paths sharing the same app_mutex cannot both acquire."""
        lock1 = SingleInstanceLock(str(self.lock_path1), app_mutex_name=self.test_app_mutex)
        lock2 = SingleInstanceLock(str(self.lock_path2), app_mutex_name=self.test_app_mutex)

        # First instance succeeds
        self.assertTrue(lock1.acquire())

        # Second instance must fail due to shared app_mutex
        if platform.system() == "Windows":
            self.assertFalse(lock2.acquire(), "Second instance must fail when app_mutex is held")

        # After releasing first, second can acquire
        lock1.release()
        if platform.system() == "Windows":
            self.assertTrue(lock2.acquire(), "Second instance acquires after first releases")
            lock2.release()

    def test_app_mutex_rollback_on_secondary_failure(self):
        """If app_mutex is held by another process, primary path mutex is released immediately."""
        if platform.system() != "Windows":
            return

        import ctypes
        kernel32 = ctypes.windll.kernel32

        # Manually hold the app mutex externally
        ext_handle = kernel32.CreateMutexW(None, True, self.test_app_mutex)
        self.assertTrue(ext_handle)

        try:
            lock = SingleInstanceLock(str(self.lock_path1), app_mutex_name=self.test_app_mutex)
            # Acquisition must fail because app_mutex is held
            self.assertFalse(lock.acquire())
            # Path mutex must not remain leaked
            path_mutex_name = SingleInstanceLock._get_mutex_name(str(self.lock_path1))
            test_h = kernel32.CreateMutexW(None, True, path_mutex_name)
            self.assertNotEqual(kernel32.GetLastError(), 183, "Path mutex must be released after app_mutex failure")
            kernel32.CloseHandle(test_h)
        finally:
            kernel32.ReleaseMutex(ext_handle)
            kernel32.CloseHandle(ext_handle)

    def test_vaani_app_default_mutex_configuration(self):
        """VaaniTrackerApp configures APP_MUTEX_NAME by default, and allows disabling for tests."""
        cfg1 = TrackerConfig(data_dir=Path(self.temp_dir1.name))
        app_default = VaaniTrackerApp(config=cfg1, headless=True)
        self.assertEqual(app_default._lock.app_mutex_name, APP_MUTEX_NAME)
        app_default.stop()

        cfg2 = TrackerConfig(data_dir=Path(self.temp_dir2.name))
        app_custom = VaaniTrackerApp(config=cfg2, headless=True, enable_app_mutex=False)
        self.assertIsNone(app_custom._lock.app_mutex_name)
        app_custom.stop()


class TestHighDpiAwareness(unittest.TestCase):
    """Verifies Windows High-DPI awareness initialization and graceful fallback."""

    def setUp(self):
        reset_dpi_state()

    def tearDown(self):
        reset_dpi_state()

    def test_dpi_initialization_idempotent(self):
        """Calling enable_high_dpi_awareness multiple times is safe and returns True."""
        res1 = enable_high_dpi_awareness()
        res2 = enable_high_dpi_awareness()
        if platform.system() == "Windows":
            self.assertTrue(res1)
            self.assertTrue(res2)
        else:
            self.assertFalse(res1)
            self.assertTrue(res2)

    def test_dpi_per_monitor_v2_attempt(self):
        """Simulates SetProcessDpiAwarenessContext call on Windows."""
        if platform.system() != "Windows":
            return

        with patch("ctypes.windll.user32.SetProcessDpiAwarenessContext", create=True) as mock_v2:
            mock_v2.return_value = 1
            reset_dpi_state()
            res = enable_high_dpi_awareness()
            self.assertTrue(res)
            mock_v2.assert_called_once()

    def test_dpi_fallback_to_shcore_when_v2_unavailable(self):
        """Simulates fallback to shcore.SetProcessDpiAwareness when v2 is absent."""
        if platform.system() != "Windows":
            return

        with patch("ctypes.windll.user32.SetProcessDpiAwarenessContext", side_effect=AttributeError("No v2")):
            with patch("ctypes.windll.shcore.SetProcessDpiAwareness", create=True) as mock_shcore:
                mock_shcore.return_value = 0  # S_OK
                reset_dpi_state()
                res = enable_high_dpi_awareness()
                self.assertTrue(res)
                mock_shcore.assert_called_once_with(2)

    def test_dpi_fallback_to_vista_aware_when_shcore_fails(self):
        """Simulates fallback to SetProcessDPIAware when shcore fails."""
        if platform.system() != "Windows":
            return

        with patch("ctypes.windll.user32.SetProcessDpiAwarenessContext", side_effect=AttributeError("No v2")):
            with patch("ctypes.windll.shcore.SetProcessDpiAwareness", side_effect=AttributeError("No shcore")):
                with patch("ctypes.windll.user32.SetProcessDPIAware", create=True) as mock_vista:
                    mock_vista.return_value = 1
                    reset_dpi_state()
                    res = enable_high_dpi_awareness()
                    self.assertTrue(res)
                    mock_vista.assert_called_once()

    def test_dpi_graceful_handling_when_all_fail(self):
        """When all DPI APIs fail, returns False gracefully without raising exceptions."""
        with patch("platform.system", return_value="Windows"):
            with patch("ctypes.windll.user32.SetProcessDpiAwarenessContext", side_effect=Exception("Error")):
                with patch("ctypes.windll.shcore.SetProcessDpiAwareness", side_effect=Exception("Error")):
                    with patch("ctypes.windll.user32.SetProcessDPIAware", side_effect=Exception("Error")):
                        reset_dpi_state()
                        res = enable_high_dpi_awareness()
                        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
