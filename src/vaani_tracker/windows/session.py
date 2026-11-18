"""Windows session notification listener (lock, unlock, logoff).

Listens for Windows Terminal Services (WTS) session changes to detect
workstation locking and unlocking via a dedicated Win32 message window.
"""

import ctypes
import logging
import platform
import threading
from typing import Callable, Optional, List

from ..constants import (
    WM_WTSSESSION_CHANGE,
    WTS_SESSION_LOCK,
    WTS_SESSION_UNLOCK,
    WTS_SESSION_LOGOFF,
    WM_QUERYENDSESSION,
    WM_ENDSESSION,
)
from .win32 import (
    HWND,
    WPARAM,
    LPARAM,
    LRESULT,
    UINT,
    BOOL,
    DWORD,
    WNDPROC,
    MSG,
    WNDCLASSEXW,
    WM_QUIT,
    PM_NOREMOVE,
    ERROR_CLASS_ALREADY_EXISTS,
    VAANI_ACTIVATE_MESSAGE,
    init_win32_prototypes,
)

logger = logging.getLogger(__name__)

NOTIFY_FOR_THIS_SESSION = 0


class WindowsSessionNotifier:
    """Listens for Windows workstation lock, unlock, and logoff events."""

    def __init__(self):
        self._is_windows = platform.system() == "Windows"
        self._state_lock = threading.Lock()
        self._lock_callbacks: List[Callable[[], None]] = []
        self._unlock_callbacks: List[Callable[[], None]] = []
        self._logoff_callbacks: List[Callable[[], None]] = []
        self._query_end_session_callbacks: List[Callable[[], Optional[bool]]] = []
        self._end_session_callbacks: List[Callable[[bool], None]] = []
        self._activate_callbacks: List[Callable[[], None]] = []
        self._activate_msg: int = 0
        self._is_locked: bool = False
        self._is_logged_off: bool = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready_event = threading.Event()
        self._init_error: Optional[Exception] = None
        self._running: bool = False
        self._wnd_proc = None  # Prevents ctypes callback garbage collection

    @property
    def is_locked(self) -> bool:
        with self._state_lock:
            return self._is_locked

    @property
    def is_logged_off(self) -> bool:
        with self._state_lock:
            return self._is_logged_off

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def on_lock(self, callback: Callable[[], None]) -> None:
        with self._state_lock:
            self._lock_callbacks.append(callback)

    def on_unlock(self, callback: Callable[[], None]) -> None:
        with self._state_lock:
            self._unlock_callbacks.append(callback)

    def on_logoff(self, callback: Callable[[], None]) -> None:
        with self._state_lock:
            self._logoff_callbacks.append(callback)

    def on_query_end_session(self, callback: Callable[[], Optional[bool]]) -> None:
        with self._state_lock:
            self._query_end_session_callbacks.append(callback)

    def on_end_session(self, callback: Callable[[bool], None]) -> None:
        with self._state_lock:
            self._end_session_callbacks.append(callback)

    def trigger_query_end_session(self, lparam: int = 0) -> bool:
        """Explicitly triggers query end session callbacks.

        Returns True if all callbacks permit ending the session (or return None),
        False if any callback explicitly denies (returns False).
        """
        with self._state_lock:
            callbacks = list(self._query_end_session_callbacks)
        logger.info(f"Session state query: WM_QUERYENDSESSION (lparam=0x{lparam:X})")
        allow_end = True
        for cb in callbacks:
            try:
                import inspect
                sig = inspect.signature(cb)
                if len(sig.parameters) >= 1:
                    res = cb(lparam)
                else:
                    res = cb()
                if res is False:
                    allow_end = False
            except Exception as e:
                logger.error(f"Error in query_end_session callback: {e}", exc_info=True)
        return allow_end

    def trigger_end_session(self, is_ending: bool, lparam: int = 0) -> None:
        """Explicitly triggers end session callbacks."""
        with self._state_lock:
            callbacks = list(self._end_session_callbacks)
        logger.info(f"Session state change: WM_ENDSESSION (is_ending={is_ending}, lparam=0x{lparam:X})")
        for cb in callbacks:
            try:
                import inspect
                sig = inspect.signature(cb)
                if len(sig.parameters) >= 2:
                    cb(is_ending, lparam)
                elif len(sig.parameters) == 1:
                    cb(is_ending)
                else:
                    cb()
            except Exception as e:
                logger.error(f"Error in end_session callback: {e}", exc_info=True)

    def on_activate(self, callback: Callable[[], None]) -> None:
        """Registers a callback invoked when a secondary instance requests activation."""
        with self._state_lock:
            self._activate_callbacks.append(callback)

    def trigger_activate(self) -> None:
        """Explicitly triggers activation callbacks."""
        with self._state_lock:
            callbacks = list(self._activate_callbacks)
        logger.info("Session state change: EXTERNAL_ACTIVATION_REQUEST")
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in activate callback: {e}", exc_info=True)

    def trigger_lock(self) -> None:
        """Explicitly triggers lock event (also useful for testing/simulation)."""
        with self._state_lock:
            if self._is_locked:
                return
            self._is_locked = True
            callbacks = list(self._lock_callbacks)
        logger.info("Session state changed: WORKSTATION LOCKED")
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in lock callback: {e}", exc_info=True)

    def trigger_unlock(self) -> None:
        """Explicitly triggers unlock event for workstation lock/unlock in the current session.

        Note on Windows lifecycle:
        Workstation lock and unlock occur within the same active user session.
        Windows logoff ends the interactive user session, stopping local tracking permanently
        for this process without altering the CRM shift. A subsequent WTS_SESSION_UNLOCK
        is not treated as a new login; upon a new Windows login, a newly launched tracker
        process checks the authoritative CRM backend shift before resuming tracking.
        """
        with self._state_lock:
            if not self._is_locked:
                return
            self._is_locked = False
            callbacks = list(self._unlock_callbacks)
        logger.info("Session state changed: WORKSTATION UNLOCKED")
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in unlock callback: {e}", exc_info=True)

    def trigger_logoff(self) -> None:
        """Explicitly triggers logoff event.

        Windows logoff ends the interactive user session. Tracking stops locally
        without modifying or completing the CRM shift. Once logged off, this process
        remains STOPPED; the application must be launched again after a new Windows login.
        """
        with self._state_lock:
            if self._is_logged_off:
                return
            self._is_logged_off = True
            self._is_locked = False
            callbacks = list(self._logoff_callbacks)
        logger.info("Session state changed: USER LOGOFF")
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in logoff callback: {e}", exc_info=True)

    def start(self, timeout: float = 2.0) -> bool:
        """Starts the background session notification listener thread on Windows."""
        if not self._is_windows:
            logger.info("Non-Windows OS: WindowsSessionNotifier running in simulated mode.")
            self._running = True
            return True

        if self._running and self._thread is not None and self._thread.is_alive():
            return True

        self._running = True
        self._init_error = None
        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_message_loop, daemon=True, name="SessionNotifierThread")
        self._thread.start()

        ready = self._ready_event.wait(timeout=timeout)
        if not ready or self._init_error is not None:
            logger.error(f"Failed to start WindowsSessionNotifier within {timeout}s: {self._init_error}")
            self.stop(timeout=1.0)
            return False
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        """Stops the notification listener and terminates the message loop."""
        if not self._running and (self._thread is None or not self._thread.is_alive()):
            return True

        self._running = False

        if not self._is_windows:
            return True

        # Ensure listener thread has initialized its queue before posting WM_QUIT
        self._ready_event.wait(timeout=min(timeout, 1.0))

        if self._thread_id:
            try:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                success = user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                if not success:
                    err = kernel32.GetLastError()
                    logger.warning(f"PostThreadMessageW WM_QUIT failed in SessionNotifier (err={err})")
            except Exception as e:
                logger.debug(f"PostThreadMessageW exception in SessionNotifier: {e}")

        terminated = True
        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.error(f"Session notifier thread failed to terminate within {timeout}s.")
                terminated = False
            else:
                self._thread = None
                self._thread_id = None

        return terminated

    def _run_message_loop(self) -> None:
        """Win32 hidden message window receiving WTSSessionNotification."""
        hinstance = None
        cls_name = "VaaniSessionNotifierClass"

        try:
            init_win32_prototypes()

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            wtsapi32 = ctypes.windll.wtsapi32

            self._thread_id = kernel32.GetCurrentThreadId()

            try:
                self._activate_msg = user32.RegisterWindowMessageW(VAANI_ACTIVATE_MESSAGE)
            except Exception as e:
                logger.debug(f"Failed to register activation message: {e}")
                self._activate_msg = 0

            # Force creation of the thread message queue before signaling readiness
            dummy_msg = MSG()
            user32.PeekMessageW(ctypes.byref(dummy_msg), None, 0, 0, PM_NOREMOVE)

            def wnd_proc(hwnd, msg, wparam, lparam):
                try:
                    if self._activate_msg and msg == self._activate_msg:
                        self.trigger_activate()
                        return 1
                    elif msg == WM_WTSSESSION_CHANGE:
                        if wparam == WTS_SESSION_LOCK:
                            self.trigger_lock()
                        elif wparam == WTS_SESSION_UNLOCK:
                            self.trigger_unlock()
                        elif wparam == WTS_SESSION_LOGOFF:
                            self.trigger_logoff()
                        return 0
                    elif msg == WM_QUERYENDSESSION:
                        can_end = self.trigger_query_end_session(lparam)
                        return 1 if can_end else 0
                    elif msg == WM_ENDSESSION:
                        is_ending = bool(wparam)
                        self.trigger_end_session(is_ending, lparam)
                        return 0
                    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                except Exception as e:
                    logger.error(f"Error in session wnd_proc (msg=0x{msg:X}): {e}", exc_info=True)
                    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            # Store callback on instance to prevent Python garbage collection
            self._wnd_proc = WNDPROC(wnd_proc)

            hinstance = kernel32.GetModuleHandleW(None)

            wcls = WNDCLASSEXW()
            wcls.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wcls.style = 0
            wcls.lpfnWndProc = self._wnd_proc
            wcls.cbClsExtra = 0
            wcls.cbWndExtra = 0
            wcls.hInstance = hinstance
            wcls.hIcon = None
            wcls.hCursor = None
            wcls.hbrBackground = None
            wcls.lpszMenuName = None
            wcls.lpszClassName = cls_name
            wcls.hIconSm = None

            atom = user32.RegisterClassExW(ctypes.byref(wcls))
            if not atom:
                err = kernel32.GetLastError()
                if err != ERROR_CLASS_ALREADY_EXISTS:
                    raise ctypes.WinError(err)

            self._hwnd = user32.CreateWindowExW(
                0,
                cls_name,
                "VaaniSessionNotificationWindow",
                0,
                0, 0, 0, 0,
                None, None, hinstance, None
            )

            if not self._hwnd:
                err = kernel32.GetLastError()
                raise ctypes.WinError(err)

            if not wtsapi32.WTSRegisterSessionNotification(self._hwnd, NOTIFY_FOR_THIS_SESSION):
                err = kernel32.GetLastError()
                raise ctypes.WinError(err)

            logger.info("Successfully registered Windows session change notification listener.")
            self._init_error = None

            # Queue is established and window registered; signal readiness
            self._ready_event.set()

            msg = MSG()
            p_msg = ctypes.byref(msg)
            while self._running:
                ret = user32.GetMessageW(p_msg, None, 0, 0)
                if ret <= 0:  # 0 indicates WM_QUIT, -1 indicates error
                    if ret == -1:
                        err = kernel32.GetLastError()
                        logger.error(f"GetMessageW error in session notifier: {err}")
                    break
                user32.TranslateMessage(p_msg)
                user32.DispatchMessageW(p_msg)

        except Exception as e:
            logger.error(f"Error in WindowsSessionNotifier thread: {e}", exc_info=True)
            self._init_error = e
            self._ready_event.set()  # Prevent stop() or start() from waiting indefinitely
            return
        finally:
            self._ready_event.set()
            if self._hwnd:
                hwnd_cleanup = self._hwnd
                self._hwnd = None
                try:
                    wtsapi32.WTSUnRegisterSessionNotification(hwnd_cleanup)
                except Exception as e:
                    logger.debug(f"WTSUnRegisterSessionNotification cleanup error: {e}")
                try:
                    user32.DestroyWindow(hwnd_cleanup)
                except Exception as e:
                    logger.debug(f"DestroyWindow cleanup error: {e}")
            if hinstance and cls_name:
                try:
                    user32.UnregisterClassW(cls_name, hinstance)
                except Exception as e:
                    logger.debug(f"UnregisterClassW cleanup error: {e}")
            self._wnd_proc = None
