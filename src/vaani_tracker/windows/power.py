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
    WM_QUIT,
    PM_NOREMOVE,
    ERROR_CLASS_ALREADY_EXISTS,
    init_win32_prototypes,
)

logger = logging.getLogger(__name__)


class WindowsPowerNotifier:
    """Listens for Windows power management events (suspend, sleep, resume)."""

    def __init__(self):
        self._is_windows = platform.system() == "Windows"
        self._state_lock = threading.Lock()
        self._suspend_callbacks: List[Callable[[], None]] = []
        self._resume_callbacks: List[Callable[[], None]] = []
        self._is_sleeping: bool = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready_event = threading.Event()
        self._init_error: Optional[Exception] = None
        self._running: bool = False
        self._wnd_proc = None  # Prevents ctypes callback garbage collection

    @property
    def is_sleeping(self) -> bool:
        with self._state_lock:
            return self._is_sleeping

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def on_suspend(self, callback: Callable[[], None]) -> None:
        with self._state_lock:
            self._suspend_callbacks.append(callback)

    def on_resume(self, callback: Callable[[], None]) -> None:
        with self._state_lock:
            self._resume_callbacks.append(callback)

    def trigger_suspend(self) -> None:
        """Explicitly triggers system suspend / sleep event."""
        with self._state_lock:
            if self._is_sleeping:
                return
            self._is_sleeping = True
            callbacks = list(self._suspend_callbacks)
        logger.info("System power state changed: ENTERING SLEEP / SUSPEND")
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in power suspend callback: {e}", exc_info=True)

    def trigger_resume(self) -> None:
        """Explicitly triggers system resume from sleep event."""
        with self._state_lock:
            if not self._is_sleeping:
                return
            self._is_sleeping = False
            callbacks = list(self._resume_callbacks)
        logger.info("System power state changed: RESUMED FROM SLEEP")
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in power resume callback: {e}", exc_info=True)

    def start(self, timeout: float = 2.0) -> bool:
        """Starts the power broadcast listener thread on Windows."""
        if not self._is_windows:
            logger.info("Non-Windows OS: WindowsPowerNotifier running in simulated mode.")
            self._running = True
            return True

        if self._running and self._thread is not None and self._thread.is_alive():
            return True

        self._running = True
        self._init_error = None
        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_message_loop, daemon=True, name="PowerNotifierThread")
        self._thread.start()

        ready = self._ready_event.wait(timeout=timeout)
        if not ready or self._init_error is not None:
            logger.error(f"Failed to start WindowsPowerNotifier within {timeout}s: {self._init_error}")
            self.stop(timeout=1.0)
            return False
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        """Stops the power listener and terminates the message loop."""
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
                    logger.warning(f"PostThreadMessageW WM_QUIT failed in PowerNotifier (err={err})")
            except Exception as e:
                logger.debug(f"PostThreadMessageW exception in PowerNotifier: {e}")

        terminated = True
        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.error(f"Power notifier thread failed to terminate within {timeout}s.")
                terminated = False
            else:
                self._thread = None
                self._thread_id = None

        return terminated

    def _run_message_loop(self) -> None:
        """Win32 hidden message window receiving WM_POWERBROADCAST."""
        hinstance = None
        cls_name = "VaaniPowerNotifierClass"

        try:
            init_win32_prototypes()

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            self._thread_id = kernel32.GetCurrentThreadId()

            # Force creation of the thread message queue before signaling readiness
            dummy_msg = MSG()
            user32.PeekMessageW(ctypes.byref(dummy_msg), None, 0, 0, PM_NOREMOVE)

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
                        logger.error(f"GetMessageW error in power notifier: {err}")
                    break
                user32.TranslateMessage(p_msg)
                user32.DispatchMessageW(p_msg)

        except Exception as e:
            logger.error(f"Error in WindowsPowerNotifier thread: {e}", exc_info=True)
            self._init_error = e
            self._ready_event.set()  # Prevent stop() or start() from waiting indefinitely
            return
        finally:
            self._ready_event.set()
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
