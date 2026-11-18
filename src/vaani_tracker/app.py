"""Application orchestrator tying together all tracker subsystems."""

import ctypes
import hashlib
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

from .config import TrackerConfig
from .constants import (
    TrackerState,
    APP_MUTEX_NAME,
    SHUTDOWN_SQLITE_BUSY_TIMEOUT_MS,
    SHUTDOWN_LOCK_TIMEOUT_SECONDS,
    SHUTDOWN_TEARDOWN_TIMEOUT_SECONDS,
)
from .client.api import VaaniApiClient
from .client.auth import DeviceAuthManager
from .core.aggregator import ActivityAggregator
from .core.state_machine import TrackingStateMachine
from .core.syncer import BackgroundSyncer
from .models import DeviceInfo
from .storage.queue import ActivityQueue
from .ui.tray import VaaniTrayApp
from .ui.dashboard import VaaniDashboard
from .ui.first_run import FirstRunWizard
from .windows.apps import WindowsProcessIdentifier
from .windows.idle import WindowsIdleDetector
from .windows.power import WindowsPowerNotifier
from .windows.session import WindowsSessionNotifier

logger = logging.getLogger(__name__)


class SingleInstanceLock:
    """Ensures only a single instance of the tracker runs per user session."""

    def __init__(self, lock_path: str, app_mutex_name: Optional[str] = None):
        self.lock_path = str(lock_path)
        self.app_mutex_name = str(app_mutex_name) if app_mutex_name else None
        self._fd = None
        self._mutex_handle = None
        self._app_mutex_handle = None
        self._owner_thread: Optional[threading.Thread] = None
        self._release_event = threading.Event()
        self._ready_event = threading.Event()
        self._release_done_event = threading.Event()
        self._release_success: Optional[bool] = None
        self._release_error: Optional[int] = None
        self._release_error_msg: Optional[str] = None
        self._acquired = False
        self._lock = threading.Lock()

    @staticmethod
    def _get_mutex_name(lock_path: str) -> str:
        """Derives a deterministic, session-local Win32 mutex name from the lock path."""
        norm_path = os.path.abspath(lock_path).lower().replace("\\", "/")
        path_hash = hashlib.sha256(norm_path.encode("utf-8")).hexdigest()[:16]
        return f"Local\\VaaniTracker_{path_hash}"

    def acquire(self, timeout: float = 5.0) -> bool:
        """Acquires single-instance protection.

        On Windows: Uses a dedicated thread to create a named kernel mutex (CreateMutexW)
        with initial ownership. If app_mutex_name is set, also acquires the stable application
        mutex for installer detection. Ensures ReleaseMutex is always executed by the owning thread.
        Never removes existing lock files, preventing mutual exclusion bypass and race conditions.
        On non-Windows: Uses non-blocking fcntl.flock on an open file descriptor.
        """
        with self._lock:
            if self._acquired:
                return True

            if os.name == "nt":
                import ctypes
                from .windows.win32 import (
                    init_win32_prototypes,
                    ERROR_ALREADY_EXISTS,
                )
                init_win32_prototypes()

                kernel32 = ctypes.windll.kernel32
                mutex_name = self._get_mutex_name(self.lock_path)

                self._release_event.clear()
                self._ready_event.clear()
                self._release_done_event.clear()
                self._release_success = None
                self._release_error = None
                self._release_error_msg = None
                self._acquired = False
                self._mutex_handle = None
                self._app_mutex_handle = None

                def _mutex_owner_worker():
                    # 1. Acquire path-specific mutex
                    handle_path = kernel32.CreateMutexW(None, True, mutex_name)
                    if not handle_path:
                        self._acquired = False
                        self._ready_event.set()
                        return

                    err = kernel32.GetLastError()
                    if err == ERROR_ALREADY_EXISTS:
                        # Another instance already created and owns this path-specific mutex
                        kernel32.CloseHandle(handle_path)
                        self._acquired = False
                        self._ready_event.set()
                        return

                    # 2. If app_mutex_name specified, acquire stable app-wide mutex
                    handle_app = None
                    if self.app_mutex_name:
                        handle_app = kernel32.CreateMutexW(None, True, self.app_mutex_name)
                        if not handle_app:
                            kernel32.ReleaseMutex(handle_path)
                            kernel32.CloseHandle(handle_path)
                            self._acquired = False
                            self._ready_event.set()
                            return

                        err_app = kernel32.GetLastError()
                        if err_app == ERROR_ALREADY_EXISTS:
                            # Another instance already created and owns the stable app mutex
                            kernel32.CloseHandle(handle_app)
                            kernel32.ReleaseMutex(handle_path)
                            kernel32.CloseHandle(handle_path)
                            self._acquired = False
                            self._ready_event.set()
                            return

                    self._mutex_handle = handle_path
                    self._app_mutex_handle = handle_app
                    self._acquired = True
                    self._ready_event.set()

                    # Keep thread alive holding the mutex until release is requested
                    self._release_event.wait()

                    # Owning thread cleanly releases and closes both mutexes
                    rel_ok = True
                    err_code = 0
                    for h in [h for h in (handle_app, handle_path) if h]:
                        try:
                            res = kernel32.ReleaseMutex(h)
                            if not res:
                                rel_ok = False
                                err_code = kernel32.GetLastError()
                                logger.error(f"ReleaseMutex failed with Win32 error {err_code}")
                        except Exception as e:
                            rel_ok = False
                            logger.error(f"ReleaseMutex exception: {e}", exc_info=True)
                        try:
                            kernel32.CloseHandle(h)
                        except Exception as e:
                            logger.error(f"CloseHandle exception: {e}", exc_info=True)

                    self._release_success = rel_ok
                    self._release_error = err_code
                    if not rel_ok:
                        self._release_error_msg = f"ReleaseMutex failed with Win32 error {err_code}"
                    self._release_done_event.set()

                self._owner_thread = threading.Thread(
                    target=_mutex_owner_worker,
                    daemon=True,
                    name="VaaniSingleInstanceMutexOwner",
                )
                self._owner_thread.start()
                ready = self._ready_event.wait(timeout=timeout)

                if not ready:
                    logger.error(f"Mutex acquisition timed out waiting for owner thread readiness ({timeout}s).")
                    # Signal release so thread can unwind if it later wakes up
                    self._release_event.set()
                    if self._owner_thread.is_alive():
                        self._owner_thread.join(timeout=1.0)
                    if self._owner_thread.is_alive():
                        logger.error("Mutex owner thread still alive after acquisition readiness timeout.")
                        # Do not clear _owner_thread reference so it can be tracked and managed
                        return False
                    self._owner_thread = None
                    return False

                if not self._acquired:
                    self._release_event.set()
                    if self._owner_thread.is_alive():
                        self._owner_thread.join(timeout=1.0)
                    if not self._owner_thread.is_alive():
                        self._owner_thread = None
                    return False

                # Record PID for diagnostic/status inspection without relying on file deletion for locking
                try:
                    os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
                    with open(self.lock_path, "w") as f:
                        f.write(str(os.getpid()))
                except Exception:
                    pass
                return True
            else:
                import fcntl
                try:
                    os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
                    self._fd = open(self.lock_path, "w")
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd.write(str(os.getpid()))
                    self._fd.flush()
                    self._acquired = True
                    return True
                except (IOError, OSError):
                    if self._fd:
                        try:
                            self._fd.close()
                        except Exception:
                            pass
                        self._fd = None
                    self._acquired = False
                    return False

    def release(self, timeout: float = 2.0) -> bool:
        """Releases the instance lock and cleans up resources.

        Returns True if release was confirmed successful, or if already released.
        Returns False if ReleaseMutex failed or owner thread remains alive.
        """
        with self._lock:
            if not self._acquired:
                # Already released or never acquired: safe and idempotent
                return True

            if os.name == "nt":
                if self._owner_thread:
                    self._release_event.set()
                    # Wait for owner thread to signal that ReleaseMutex finished
                    self._release_done_event.wait(timeout=timeout)
                    self._owner_thread.join(timeout=timeout)

                    # 1. Check if owner thread actually terminated
                    if self._owner_thread.is_alive():
                        logger.error(f"Mutex owner thread failed to terminate within {timeout}s.")
                        return False

                    # 2. Check if ReleaseMutex was confirmed successful
                    if not self._release_success:
                        logger.error(f"ReleaseMutex unconfirmed or failed (err={self._release_error}): {self._release_error_msg}")
                        return False

                    # Release confirmed! Clean up references and state
                    self._owner_thread = None
                    self._mutex_handle = None
                    self._app_mutex_handle = None
                    self._acquired = False

                    # Remove diagnostic lock file only if it was created by this process
                    try:
                        if os.path.exists(self.lock_path):
                            with open(self.lock_path, "r") as f:
                                stored_pid = f.read().strip()
                            if stored_pid == str(os.getpid()):
                                os.remove(self.lock_path)
                            else:
                                logger.debug(f"Preserving foreign lock file holding PID {stored_pid}")
                    except Exception:
                        pass

                    return True

                self._acquired = False
                return True
            else:
                try:
                    if self._fd:
                        import fcntl
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                        self._fd.close()
                        self._fd = None
                    if os.path.exists(self.lock_path):
                        with open(self.lock_path, "r") as f:
                            stored_pid = f.read().strip()
                        if stored_pid == str(os.getpid()):
                            os.remove(self.lock_path)
                        else:
                            logger.debug(f"Preserving foreign lock file holding PID {stored_pid}")
                except Exception:
                    pass
                self._acquired = False
                return True


class VaaniTrackerApp:
    """Main application lifecycle controller."""

    def __init__(
        self,
        config: Optional[TrackerConfig] = None,
        headless: bool = False,
        enable_app_mutex: bool = True,
    ):
        self.config = config or TrackerConfig.load()
        self.headless = headless
        self.enable_app_mutex = enable_app_mutex

        self._running = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._teardown_started = False
        self._teardown_completed = False
        self._teardown_thread: Optional[threading.Thread] = None
        self._execute_teardown_lock = threading.Lock()
        self._tick_thread: Optional[threading.Thread] = None

        app_mutex = APP_MUTEX_NAME if enable_app_mutex else None
        self._lock = SingleInstanceLock(
            str(self.config.data_dir / "tracker.lock"),
            app_mutex_name=app_mutex,
        )

        # Configure logging
        self._setup_logging()

        # Initialize storage
        self.queue = ActivityQueue(self.config.db_path)

        # Initialize authentication & networking
        self.auth_manager = DeviceAuthManager(self.config, self.queue)
        self.api_client = VaaniApiClient(self.config.backend_url)

        # Initialize Windows hardware sensors
        self.idle_detector = WindowsIdleDetector()
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.process_identifier = WindowsProcessIdentifier()

        # Wire Windows session lifecycle callbacks for shutdown safety
        self.session_notifier.on_query_end_session(self._handle_query_end_session)
        self.session_notifier.on_end_session(self._handle_end_session)

        # Initialize core logic
        self.state_machine = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=self.session_notifier,
            power_notifier=self.power_notifier,
            idle_threshold_seconds=self.config.idle_threshold_seconds,
        )

        self.aggregator = ActivityAggregator(
            state_machine=self.state_machine,
            process_identifier=self.process_identifier,
            queue=self.queue,
            device_uuid=self.config.device_uuid,
            bucket_duration_seconds=60,
            track_apps=self.config.track_apps,
            auth_manager=self.auth_manager,
        )

        self.syncer = BackgroundSyncer(
            config=self.config,
            api_client=self.api_client,
            queue=self.queue,
            auth_manager=self.auth_manager,
            state_machine=self.state_machine,
        )

        # Initialize UI
        self._wizard_root: Optional[Any] = None
        self.dashboard: Optional[VaaniDashboard] = None
        self.activated_existing: bool = False
        self._activation_lock = threading.Lock()
        self._pending_activation: bool = False
        self._dispatch_pending: bool = False
        self._active_ui_root: Optional[Any] = None
        self._active_ui_restore_func: Optional[Any] = None
        self.tray = VaaniTrayApp(
            config=self.config,
            state_machine=self.state_machine,
            syncer=self.syncer,
            queue=self.queue,
            on_exit=self._on_tray_exit,
            on_open_dashboard=self.show_dashboard,
            auth_manager=self.auth_manager,
        )

        # Wire external second-instance activation request
        self.session_notifier.on_activate(self._handle_external_activation)

        # Wire hardware power resume to reset tick loop timing anchors
        self._just_resumed = False
        self.power_notifier.on_resume(self._on_power_resume)

    def _on_tray_exit(self) -> None:
        """Handles user clicking 'Exit Tracker' in the system tray menu."""
        logger.info("Exit requested from system tray.")
        self.request_stop(wait=False)

    def consume_pending_activation(self) -> bool:
        """Atomically consumes and clears any pending activation request.

        Returns True if an activation was pending, False otherwise.
        """
        with self._activation_lock:
            if self._pending_activation:
                self._pending_activation = False
                return True
            return False

    def clear_pending_activation(self) -> None:
        """Clears any pending activation state without triggering UI actions."""
        with self._activation_lock:
            self._pending_activation = False
            self._dispatch_pending = False

    def _setup_activation_poller(
        self,
        root: Any,
        restore_func: Any,
        poll_interval_ms: int = 200,
    ) -> None:
        """Sets up a non-blocking main-thread poller to consume pending activations."""
        self._active_ui_root = root
        self._active_ui_restore_func = restore_func

        def _poll():
            if not self._running or self._shutdown_event.is_set():
                return
            try:
                if hasattr(root, "winfo_exists") and not root.winfo_exists():
                    return
            except Exception:
                return

            if self.consume_pending_activation():
                try:
                    restore_func()
                    logger.info("Consumed pending activation and restored window.")
                except Exception as e:
                    logger.error(f"Error executing activation restore function: {e}", exc_info=True)

            try:
                if self._running and not self._shutdown_event.is_set():
                    if hasattr(root, "winfo_exists") and root.winfo_exists():
                        root.after(poll_interval_ms, _poll)
            except Exception:
                pass

        # Schedule initial consumption check right after mainloop begins, and periodic checks thereafter
        try:
            root.after(50, _poll)
        except Exception as e:
            logger.debug(f"Could not schedule initial activation poller: {e}")

    def _activate_existing_instance(self) -> bool:
        """Sends an activation signal to the running instance of Vaani Desktop Tracker."""
        if os.name != "nt":
            return False

        try:
            from .windows.win32 import (
                HWND_BROADCAST,
                VAANI_ACTIVATE_MESSAGE,
                init_win32_prototypes,
                DWORD,
            )
            init_win32_prototypes()
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # 1. Register application activation message
            msg_id = user32.RegisterWindowMessageW(VAANI_ACTIVATE_MESSAGE)
            if not msg_id:
                logger.warning("Could not register activation window message.")
                return False

            # 2. Find existing instance's notification window
            hwnd = user32.FindWindowExW(None, None, "VaaniSessionNotifierClass", "VaaniSessionNotificationWindow")
            sent = False
            target_pid = None

            if hwnd:
                # Obtain authoritative owning PID directly from target HWND
                try:
                    pid_val = DWORD()
                    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
                    if tid and pid_val.value > 0:
                        target_pid = pid_val.value
                except Exception as e:
                    logger.debug(f"Failed to query target PID from HWND 0x{hwnd:X}: {e}")

                # Authorize target process to set foreground window BEFORE posting activation
                if target_pid:
                    try:
                        if hasattr(user32, "AllowSetForegroundWindow"):
                            res_fg = user32.AllowSetForegroundWindow(target_pid)
                            if not res_fg:
                                err = kernel32.GetLastError()
                                logger.debug(f"AllowSetForegroundWindow({target_pid}) returned FALSE (err={err})")
                            else:
                                logger.debug(f"Authorized target process PID={target_pid} to set foreground window.")
                    except Exception as e:
                        logger.debug(f"AllowSetForegroundWindow({target_pid}) exception: {e}")

                # Send direct activation message
                res = user32.PostMessageW(hwnd, msg_id, os.getpid(), 0)
                if res:
                    sent = True
                    logger.info(
                        f"Sent direct activation message to existing instance window (hwnd=0x{hwnd:X}, PID={target_pid or 'unknown'})."
                    )
                else:
                    err = kernel32.GetLastError()
                    logger.warning(
                        f"Direct PostMessageW to existing window failed (hwnd=0x{hwnd:X}, err={err}). Falling back to broadcast."
                    )

            # 3. Broadcast to user session as fallback only if direct delivery failed or window not found
            # Note: Broadcast fallback never authorizes an unverified PID from lock file
            if not sent:
                res_broadcast = user32.PostMessageW(HWND_BROADCAST, msg_id, os.getpid(), 0)
                if res_broadcast:
                    sent = True
                    logger.info(
                        "Broadcast activation request to user session. Note: Delivery cannot be confirmed via broadcast."
                    )
                else:
                    err = kernel32.GetLastError()
                    logger.error(f"Broadcast activation request failed (err={err}).")

            return sent
        except Exception as e:
            logger.error(f"Failed to signal existing instance: {e}", exc_info=True)
            return False

    def _handle_external_activation(self) -> None:
        """Handles activation request from a second instance attempting to launch.

        Stores the pending activation safely in thread-safe state without making
        any cross-thread calls into Tkinter. The active main-thread UI poller consumes
        the state and restores the appropriate window on the Tkinter thread.
        """
        if not self._running or self._shutdown_event.is_set():
            logger.info("Activation request received but application is currently shutting down.")
            return

        with self._activation_lock:
            self._pending_activation = True
            logger.info("External activation: Recorded pending activation request.")

    def show_dashboard(self) -> None:
        """Restores and focuses the main desktop dashboard window safely on the Tkinter main thread."""
        if threading.current_thread() is threading.main_thread():
            if self.dashboard and hasattr(self.dashboard, "show"):
                try:
                    self.dashboard.show()
                except Exception as e:
                    logger.error(f"Error showing dashboard on main thread: {e}")
        else:
            self._handle_external_activation()

    def close_logging(self) -> None:
        """Closes and removes file logging handler to release Windows file handles."""
        if hasattr(self, "_file_handler") and self._file_handler:
            try:
                root = logging.getLogger()
                root.removeHandler(self._file_handler)
                self._file_handler.close()
            except Exception:
                pass
            self._file_handler = None

    def __del__(self) -> None:
        try:
            self.close_logging()
        except Exception:
            pass

    def _on_power_resume(self) -> None:
        """Called when system resumes from sleep/suspend to reset tick timing anchors."""
        logger.info("System resumed from sleep, resetting tick timing anchor.")
        self._just_resumed = True

    def _setup_logging(self) -> None:
        """Sets up rotating file and console logging."""
        level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        root = logging.getLogger()
        root.setLevel(level)

        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler
        if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            root.addHandler(console)

        # File handler
        self._file_handler = None
        try:
            self._file_handler = logging.FileHandler(str(self.config.log_path), encoding="utf-8")
            self._file_handler.setFormatter(formatter)
            root.addHandler(self._file_handler)
        except Exception:
            pass

    def start(self, start_tick_thread: bool = True) -> bool:
        """Starts all tracking, sensing, and synchronization workers."""
        try:
            # Enable high-DPI awareness on Windows before creating any UI objects
            from .windows.win32 import enable_high_dpi_awareness
            enable_high_dpi_awareness()
        except Exception as e:
            logger.debug(f"High-DPI awareness initialization skipped: {e}")

        if not self._lock.acquire():
            logger.warning("Another instance of Vaani Desktop Tracker is already running.")
            if self._activate_existing_instance():
                self.activated_existing = True
            return False

        logger.info(f"Starting Vaani Desktop Tracker (Device UUID: {self.config.device_uuid})")
        logger.info(f"Target Backend: {self.config.backend_url}")

        self._running = True
        self._shutdown_event.clear()
        self._teardown_started = False
        self._teardown_completed = False
        self._teardown_thread = None

        try:
            # If unauthenticated, show guided setup wizard before starting background workers
            if not self.headless and not self.auth_manager.is_authenticated():
                logger.info("Workstation unauthenticated. Launching First-Run Pairing Wizard...")
                import tkinter as tk
                wizard_root = tk.Tk()
                self._wizard_root = wizard_root
                wizard = FirstRunWizard(
                    config=self.config,
                    auth_manager=self.auth_manager,
                    api_client=self.api_client,
                    root=wizard_root,
                )

                def _restore_wizard():
                    if hasattr(self, "_wizard_root") and self._wizard_root:
                        try:
                            if hasattr(self._wizard_root, "winfo_exists") and not self._wizard_root.winfo_exists():
                                self._wizard_root = None
                                return
                            self._wizard_root.deiconify()
                            self._wizard_root.state("normal")
                            self._wizard_root.lift()
                            self._wizard_root.focus_force()
                        except Exception as e:
                            logger.error(f"Error during wizard window restoration: {e}", exc_info=True)

                self._setup_activation_poller(wizard_root, _restore_wizard, poll_interval_ms=200)

                # Ensure external activation callback is registered on session notifier
                self.session_notifier.on_activate(self._handle_external_activation)

                # Start session notifier so notification window is ready before entering blocking mainloop
                if not self.session_notifier.start():
                    logger.error("Failed to start WindowsSessionNotifier before wizard display.")
                    self._wizard_root = None
                    self.clear_pending_activation()
                    try:
                        wizard_root.destroy()
                    except Exception:
                        pass
                    self._running = False
                    self._lock.release()
                    raise RuntimeError("Failed to start WindowsSessionNotifier")

                # Enter blocking Tkinter event loop
                wizard_root.mainloop()
                self._wizard_root = None
                self._active_ui_root = None
                self._active_ui_restore_func = None

                # If user cancelled wizard without completing pairing, cancel startup cleanly
                if not self.auth_manager.is_authenticated() or not wizard.completed:
                    logger.info("First-run pairing was not completed by user. Cancelling tracker startup.")
                    self._running = False
                    self.clear_pending_activation()
                    self.session_notifier.stop(timeout=2.0)
                    self._lock.release()
                    if hasattr(self, "_file_handler") and self._file_handler:
                        try:
                            logging.getLogger().removeHandler(self._file_handler)
                            self._file_handler.close()
                            self._file_handler = None
                        except Exception:
                            pass
                    return False

            # Start Windows event listeners
            if not self.session_notifier.start():
                raise RuntimeError("Failed to start WindowsSessionNotifier")
            if not self.power_notifier.start():
                raise RuntimeError("Failed to start WindowsPowerNotifier")

            # Start background sync & heartbeat
            self.syncer.start()

            # Start 1-second aggregator ticking thread if requested
            if start_tick_thread:
                self._tick_thread = threading.Thread(
                    target=self._run_tick_loop,
                    daemon=True,
                    name="VaaniAggregatorTickThread",
                )
                self._tick_thread.start()

            # Register OS signal handlers for graceful exit
            try:
                signal.signal(signal.SIGINT, lambda *_: self.stop())
                signal.signal(signal.SIGTERM, lambda *_: self.stop())
            except Exception:
                pass

            # Start UI (system tray and main dashboard)
            if not self.headless:
                import tkinter as tk

                # Start system tray in background thread
                self.tray.run_detached()

                # Run main desktop dashboard on main thread
                root = tk.Tk()
                self.dashboard = VaaniDashboard(
                    config=self.config,
                    state_machine=self.state_machine,
                    syncer=self.syncer,
                    queue=self.queue,
                    auth_manager=self.auth_manager,
                    on_exit=self.stop,
                    root=root,
                    aggregator=self.aggregator,
                )
                self._setup_activation_poller(root, self.dashboard.show, poll_interval_ms=200)
                logger.info("Desktop Dashboard interface started.")
                root.mainloop()

                # Mainloop has unblocked on main thread; run orderly teardown
                self._execute_teardown()
            else:
                logger.info("Running in headless mode (no system tray or dashboard).")

            return True
        except Exception as e:
            logger.error(f"Error starting Vaani Desktop Tracker: {e}", exc_info=True)
            self._wizard_root = None
            self.clear_pending_activation()
            self.stop()
            return False

    def _handle_query_end_session(self, lparam: int = 0) -> bool:
        """Handles Windows WM_QUERYENDSESSION.

        Persists current in-memory activity bucket to SQLite queue immediately.
        Bounded and thread-safe; does not initiate network synchronization or wait indefinitely.

        Policy:
        - If there is no pending activity or persistence succeeds, returns True (allows shutdown).
        - If persistence fails, times out, or encounters lock contention, returns False (vetoes shutdown)
          to prevent silent loss of in-memory activity while process terminates.
        """
        logger.info(f"WM_QUERYENDSESSION received (lparam=0x{lparam:X}): persisting in-memory activity to local queue.")
        try:
            bucket = self.aggregator.close_current_bucket(
                lock_timeout=SHUTDOWN_LOCK_TIMEOUT_SECONDS,
                db_timeout_ms=SHUTDOWN_SQLITE_BUSY_TIMEOUT_MS,
            )
            if bucket is not None:
                logger.info(
                    f"WM_QUERYENDSESSION successfully persisted bucket {bucket.bucket_id} "
                    f"({bucket.active_seconds}s active, {bucket.idle_seconds}s idle). Permitting shutdown."
                )
            else:
                logger.debug("WM_QUERYENDSESSION found no pending activity in memory. Permitting shutdown.")
            return True
        except Exception as e:
            logger.error(
                f"WM_QUERYENDSESSION failed to persist in-memory activity bucket: {e}. "
                f"Vetoing shutdown to preserve employee activity data.",
                exc_info=True,
            )
            # Fail closed / veto shutdown so Windows does not terminate with unsaved in-memory activity
            return False

    def _handle_end_session(self, is_ending: bool, lparam: int = 0) -> None:
        """Handles Windows WM_ENDSESSION.

        If is_ending is True, immediately halts incoming activity ticks and hands off
        teardown to a dedicated coordinator thread, returning promptly to Windows.
        If is_ending is False, shutdown was cancelled by Windows or user; tracking continues.
        """
        if is_ending:
            logger.info(f"WM_ENDSESSION received (confirmed termination, lparam=0x{lparam:X}): initiating asynchronous teardown.")
            self.request_stop(wait=False)
        else:
            logger.info(f"WM_ENDSESSION received (termination aborted, lparam=0x{lparam:X}): continuing normal tracking.")

    def request_stop(self, wait: bool = False, timeout: float = SHUTDOWN_TEARDOWN_TIMEOUT_SECONDS) -> bool:
        """Requests graceful application teardown.

        If wait is True, blocks until teardown finishes or timeout elapses.
        If wait is False, initiates asynchronous teardown via a dedicated coordinator thread
        and returns immediately without blocking the caller (essential for Windows message handlers).

        Idempotent: Safe to invoke repeatedly or concurrently from multiple threads.
        """
        target_thread = None
        acquired = self._shutdown_lock.acquire(timeout=SHUTDOWN_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            logger.warning(
                f"Could not acquire _shutdown_lock within {SHUTDOWN_LOCK_TIMEOUT_SECONDS}s in request_stop(); "
                f"forcing immediate aggregator stop."
            )
            self._running = False
            self._shutdown_event.set()
            try:
                self.aggregator.stop(timeout=1.0)
            except Exception:
                pass
            return False

        try:
            # Immediately halt incoming activity ticks and flag shutdown
            self._running = False
            self._shutdown_event.set()

            try:
                self.aggregator.stop()
            except Exception as e:
                logger.error(f"Error stopping aggregator on stop request: {e}", exc_info=True)

            # Signal dashboard to close cleanly on Tk-owning main thread
            if self.dashboard:
                try:
                    self.dashboard.request_close()
                except Exception as e:
                    logger.debug(f"Error requesting dashboard close: {e}")

            if self._teardown_started:
                target_thread = self._teardown_thread
            else:
                self._teardown_started = True
                if not wait:
                    self._teardown_thread = threading.Thread(
                        target=self._execute_teardown,
                        daemon=True,
                        name="VaaniShutdownCoordinator",
                    )
                    self._teardown_thread.start()
                    return True
        finally:
            self._shutdown_lock.release()

        if target_thread and target_thread.is_alive():
            if wait and target_thread != threading.current_thread():
                target_thread.join(timeout=timeout)
            return True

        if wait and not target_thread:
            self._execute_teardown()
            return True

        return True

    def stop(self, timeout: float = SHUTDOWN_TEARDOWN_TIMEOUT_SECONDS) -> None:
        """Cleanly halts all workers and releases resources (synchronous)."""
        self.request_stop(wait=True, timeout=timeout)

    def _execute_teardown(self) -> None:
        """Executes full worker teardown, bucket finalization, and mutex release."""
        with self._execute_teardown_lock:
            if self._teardown_completed:
                return

            logger.info("Stopping Vaani Desktop Tracker...")
            self._running = False
            self._shutdown_event.set()

            # 1. Stop aggregator callbacks so no more ticks or state changes are processed
            try:
                self.aggregator.stop()
            except Exception as e:
                logger.error(f"Error stopping aggregator: {e}", exc_info=True)

            # 2. Stop and join aggregator ticking thread before closing bucket
            if self._tick_thread and self._tick_thread.is_alive():
                if self._tick_thread != threading.current_thread():
                    self._tick_thread.join(timeout=2.0)
                    if not self._tick_thread.is_alive():
                        self._tick_thread = None

            # 3. Finalize and persist any partial bucket in progress
            try:
                self.aggregator.close_current_bucket()
            except Exception as e:
                logger.error(f"Error finalizing bucket on stop: {e}", exc_info=True)

            # 4. Stop background workers and track termination
            syncer_ok = self.syncer.stop(timeout=3.0)
            session_ok = self.session_notifier.stop(timeout=2.0)
            power_ok = self.power_notifier.stop(timeout=2.0)
            tray_ok = self.tray.stop(timeout=2.0)

            if self.dashboard:
                try:
                    self.dashboard.close()
                except Exception as e:
                    logger.debug(f"Error closing dashboard: {e}")
                self.dashboard = None
            self.clear_pending_activation()
            self._wizard_root = None

            all_stopped = syncer_ok and session_ok and power_ok and tray_ok
            if self._tick_thread and self._tick_thread.is_alive():
                all_stopped = False

            if all_stopped:
                # 5. Release single-instance lock ONLY IF all workers stopped cleanly
                mutex_ok = self._lock.release()
                if mutex_ok:
                    logger.info("Vaani Desktop Tracker stopped.")
                else:
                    logger.error("Vaani Desktop Tracker stop completed but failed to release single-instance lock.")
            else:
                failed = []
                if not syncer_ok: failed.append("Syncer")
                if not session_ok: failed.append("SessionNotifier")
                if not power_ok: failed.append("PowerNotifier")
                if not tray_ok: failed.append("Tray")
                if self._tick_thread and self._tick_thread.is_alive(): failed.append("TickThread")
                logger.error(f"Vaani Desktop Tracker stop completed with unstopped components: {', '.join(failed)}")
                # Mutex is preserved to prevent usurpation while components remain unstopped

            # 6. Clean up file logger handler so resources are not locked
            self.close_logging()

            self._teardown_completed = True

    def _run_tick_loop(self) -> None:
        """Ticks the activity aggregator using accurate monotonic intervals without timing drift."""
        last_tick_mono = time.monotonic()
        target_tick_mono = last_tick_mono + 1.0

        while self._running:
            now_mono = time.monotonic()
            sleep_time = max(0.0, target_tick_mono - now_mono)

            # Responsive wait: wakes up immediately when shutdown is signaled
            if self._shutdown_event.wait(timeout=sleep_time):
                break

            if not self._running:
                break

            current_mono = time.monotonic()

            # If system is currently suspended/sleeping, discard interval and do not tick work time
            if self.power_notifier.is_sleeping or self.state_machine.current_state == TrackerState.SLEEPING:
                last_tick_mono = current_mono
                target_tick_mono = current_mono + 1.0
                continue

            # If system just resumed from sleep, reset tick timing anchor so sleep duration is never counted
            if self._just_resumed:
                self._just_resumed = False
                last_tick_mono = current_mono
                target_tick_mono = current_mono + 1.0
                continue

            elapsed_seconds = current_mono - last_tick_mono
            last_tick_mono = current_mono

            if elapsed_seconds <= 0.0:
                continue

            # Advance target time by 1.0s to prevent drift accumulation
            target_tick_mono += 1.0
            if current_mono > target_tick_mono + 2.0:
                # System was delayed significantly (e.g. system suspend or heavy lag).
                # Reset target to prevent burst ticking catch-up while preserving true elapsed time.
                target_tick_mono = current_mono + 1.0
                # Clamp elapsed_seconds so a delayed tick does not burst-fill multiple artificial buckets
                elapsed_seconds = min(elapsed_seconds, float(self.aggregator.bucket_duration_seconds))

            try:
                self.aggregator.tick(elapsed_seconds=elapsed_seconds)
            except Exception as e:
                logger.error(f"Error in aggregator tick: {e}", exc_info=True)

