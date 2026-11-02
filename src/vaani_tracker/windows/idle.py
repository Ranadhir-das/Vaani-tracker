"""Windows idle time detection using GetLastInputInfo."""

import ctypes
import logging
import platform
import time
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


class WindowsIdleDetector:
    """Detects seconds elapsed since the user last provided keyboard or mouse input."""

    def __init__(self, idle_provider: Optional[Callable[[], float]] = None):
        self._custom_provider = idle_provider
        self._is_windows = platform.system() == "Windows"
        self._user32 = None
        self._kernel32 = None

        if self._is_windows and not self._custom_provider:
            try:
                self._user32 = ctypes.windll.user32
                self._kernel32 = ctypes.windll.kernel32
            except Exception as e:
                logger.warning(f"Could not load Windows user32/kernel32 libraries: {e}")

    def get_idle_seconds(self) -> float:
        """Returns the number of seconds since the last mouse or keyboard input."""
        if self._custom_provider is not None:
            return float(self._custom_provider())

        if not self._is_windows or not self._user32 or not self._kernel32:
            # Fallback for non-Windows dev environments
            return 0.0

        try:
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)

            if not self._user32.GetLastInputInfo(ctypes.byref(lii)):
                logger.debug("GetLastInputInfo call returned FALSE")
                return 0.0

            # Prefer 64-bit tick count to avoid 49.7 day wrap
            if hasattr(self._kernel32, "GetTickCount64"):
                current_ticks = self._kernel32.GetTickCount64()
            else:
                current_ticks = self._kernel32.GetTickCount()

            last_input_ticks = lii.dwTime

            # Handle 32-bit tick counter wrap-around
            elapsed_ms = (current_ticks - last_input_ticks) & 0xFFFFFFFF
            return max(0.0, elapsed_ms / 1000.0)

        except Exception as e:
            logger.error(f"Error querying Windows idle duration: {e}", exc_info=True)
            return 0.0

    def is_idle(self, threshold_seconds: float = 60.0) -> bool:
        """Returns True if the user has been inactive for >= threshold_seconds."""
        return self.get_idle_seconds() >= threshold_seconds

