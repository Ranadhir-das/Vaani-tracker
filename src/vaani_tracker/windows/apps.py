"""Foreground application executable identification with strict privacy protection."""

import ctypes
import logging
import os
import platform
from typing import Optional, Callable

from ..privacy import PrivacyGuard

logger = logging.getLogger(__name__)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WindowsProcessIdentifier:
    """Safely identifies the foreground application's executable basename.

    Strict Privacy Principle:
    ONLY retrieves the process binary filename (e.g. 'code.exe').
    NEVER queries window titles, tab titles, URLs, or document contents.
    """

    def __init__(self, provider: Optional[Callable[[], Optional[str]]] = None):
        self._custom_provider = provider
        self._is_windows = platform.system() == "Windows"
        self._user32 = None
        self._kernel32 = None

        if self._is_windows and not self._custom_provider:
            try:
                self._user32 = ctypes.windll.user32
                self._kernel32 = ctypes.windll.kernel32
            except Exception as e:
                logger.warning(f"Could not load Windows user32/kernel32: {e}")

    def get_foreground_process_name(self) -> Optional[str]:
        """Returns the executable basename of the active foreground window (e.g. 'code.exe').

        Returns None if no window is active or resolution fails.
        """
        if self._custom_provider is not None:
            raw = self._custom_provider()
            return PrivacyGuard.sanitize_process_name(raw) if raw else None

        if not self._is_windows or not self._user32 or not self._kernel32:
            return None

        try:
            hwnd = self._user32.GetForegroundWindow()
            if not hwnd:
                return None

            pid = ctypes.c_ulong()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return None

            h_process = self._kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid.value,
            )
            if not h_process:
                return None

            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                # QueryFullProcessImageNameW is supported on Windows Vista and newer (Win 10/11)
                success = self._kernel32.QueryFullProcessImageNameW(
                    h_process,
                    0,
                    buf,
                    ctypes.byref(size),
                )
                if success:
                    raw_path = buf.value
                    basename = os.path.basename(raw_path)
                    return PrivacyGuard.sanitize_process_name(basename)
                return None
            finally:
                self._kernel32.CloseHandle(h_process)

        except Exception as e:
            logger.debug(f"Failed to resolve foreground process: {e}")
            return None

