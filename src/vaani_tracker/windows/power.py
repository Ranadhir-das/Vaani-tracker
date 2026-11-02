"""Windows power broadcast listener (sleep, hibernation, resume).

Listens for WM_POWERBROADCAST messages via a dedicated Win32 message window
to detect system sleep/suspend and wake/resume transitions.
"""

import ctypes
import logging
import platform
import threading
from typing import Callable, Optional, List

from ..constants import (
    WM_POWERBROADCAST,
    PBT_APMSUSPEND,
    PBT_APMRESUMEAUTOMATIC,
    PBT_APMRESUMESUSPEND,
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

ERROR_CLASS_ALREADY_EXISTS = 1410
WM_QUIT = 0x0012


class WindowsPowerNotifier:
    """Listens for Windows power management events (suspend, sleep, resume)."""

    def __init__(self):
        self._is_windows = platform.system() == "Windows"
        self._suspend_callbacks: List[Callable[[], None]] = []
        self._resume_callbacks: List[Callable[[], None]] = []
        self._is_sleeping: bool = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._wnd_proc = None  # Prevents ctypes callback garbage collection

    @property
    def is_sleeping(self) -> bool:
        return self._is_sleeping

    def on_suspend(self, callback: Callable[[], None]) -> None:
        self._suspend_callbacks.append(callback)

    def on_resume(self, callback: Callable[[], None]) -> None:
        self._resume_callbacks.append(callback)

    def trigger_suspend(self) -> None:
        """Explicitly triggers system suspend / sleep event."""
        logger.info("System power state changed: ENTERING SLEEP / SUSPEND")
        self._is_sleeping = True
        for cb in self._suspend_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in power suspend callback: {e}", exc_info=True)

    def trigger_resume(self) -> None:
        """Explicitly triggers system resume from sleep event."""
        logger.info("System power state changed: RESUMED FROM SLEEP")
        self._is_sleeping = False
        for cb in self._resume_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in power resume callback: {e}", exc_info=True)

    def start(self) -> None:
        """Starts the power broadcast listener thread on Windows."""
        if not self._is_windows:
            logger.info("Non-Windows OS: WindowsPowerNotifier running in simulated mode.")
            return

        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_message_loop, daemon=True, name="PowerNotifierThread")
        self._thread.start()

    def stop(self) -> None:
        """Stops the power listener and terminates the message loop."""
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
        """Win32 hidden message window receiving WM_POWERBROADCAST."""
        hinstance = None
        cls_name = "VaaniPowerNotifierClass"

        try:
            init_win32_prototypes()

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            def wnd_proc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_POWERBROADCAST:
                        if wparam == PBT_APMSUSPEND:
                            self.trigger_suspend()
                        elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                            self.trigger_resume()
                        return 1  # TRUE for power messages
                    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                except Exception as e:
                    logger.error(f"Error in power wnd_proc (msg=0x{msg:X}): {e}", exc_info=True)
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
                "VaaniPowerNotificationWindow",
                0,
                0, 0, 0, 0,
                None, None, hinstance, None
            )

            if not self._hwnd:
                err = kernel32.GetLastError()
                raise ctypes.WinError(err)

            logger.info("Successfully registered Windows power notification listener.")

            msg = MSG()
            p_msg = ctypes.byref(msg)
            while self._running:
                ret = user32.GetMessageW(p_msg, None, 0, 0)
                if ret <= 0:  # 0 indicates WM_QUIT, -1 indicates error
                    if ret == -1:
                        err = kernel32.GetLastError()
                        logger.error(f"GetMessageW error in power notifier: {err}")
                    break
                user32.TranslateMessage(p_msg)
                user32.DispatchMessageW(p_msg)

        except Exception as e:
            logger.error(f"Error in WindowsPowerNotifier thread: {e}", exc_info=True)
            raise
        finally:
            if self._hwnd:
                hwnd_cleanup = self._hwnd
                self._hwnd = None
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
