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
    init_win32_prototypes,
)

logger = logging.getLogger(__name__)

NOTIFY_FOR_THIS_SESSION = 0
ERROR_CLASS_ALREADY_EXISTS = 1410
WM_QUIT = 0x0012


class WindowsSessionNotifier:
    """Listens for Windows workstation lock, unlock, and logoff events."""

    def __init__(self):
        self._is_windows = platform.system() == "Windows"
        self._lock_callbacks: List[Callable[[], None]] = []
        self._unlock_callbacks: List[Callable[[], None]] = []
        self._logoff_callbacks: List[Callable[[], None]] = []
        self._is_locked: bool = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._wnd_proc = None  # Prevents ctypes callback garbage collection

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    def on_lock(self, callback: Callable[[], None]) -> None:
        self._lock_callbacks.append(callback)

    def on_unlock(self, callback: Callable[[], None]) -> None:
        self._unlock_callbacks.append(callback)

    def on_logoff(self, callback: Callable[[], None]) -> None:
        self._logoff_callbacks.append(callback)

    def trigger_lock(self) -> None:
        """Explicitly triggers lock event (also useful for testing/simulation)."""
        logger.info("Session state changed: WORKSTATION LOCKED")
        self._is_locked = True
        for cb in self._lock_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in lock callback: {e}", exc_info=True)

    def trigger_unlock(self) -> None:
        """Explicitly triggers unlock event (also useful for testing/simulation)."""
        logger.info("Session state changed: WORKSTATION UNLOCKED")
        self._is_locked = False
        for cb in self._unlock_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in unlock callback: {e}", exc_info=True)

    def trigger_logoff(self) -> None:
        """Explicitly triggers logoff event."""
        logger.info("Session state changed: USER LOGOFF")
        self._is_locked = False
        for cb in self._logoff_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in logoff callback: {e}", exc_info=True)

    def start(self) -> None:
        """Starts the background session notification listener thread on Windows."""
        if not self._is_windows:
            logger.info("Non-Windows OS: WindowsSessionNotifier running in simulated mode.")
            return

        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_message_loop, daemon=True, name="SessionNotifierThread")
        self._thread.start()

    def stop(self) -> None:
        """Stops the notification listener and terminates the message loop."""
        self._running = False
        if self._hwnd and self._is_windows:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
            except Exception as e:
                logger.debug(f"PostMessageW WM_QUIT failed: {e}")
        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=1.5)
            self._thread = None

    def _run_message_loop(self) -> None:
        """Win32 hidden message window receiving WTSSessionNotification."""
        hinstance = None
        cls_name = "VaaniSessionNotifierClass"

        try:
            init_win32_prototypes()

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            wtsapi32 = ctypes.windll.wtsapi32

            def wnd_proc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_WTSSESSION_CHANGE:
                        if wparam == WTS_SESSION_LOCK:
                            self.trigger_lock()
                        elif wparam == WTS_SESSION_UNLOCK:
                            self.trigger_unlock()
                        elif wparam == WTS_SESSION_LOGOFF:
                            self.trigger_logoff()
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
            raise
        finally:
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
