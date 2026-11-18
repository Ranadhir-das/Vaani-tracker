"""Pytest configuration and environment initialization for Vaani Tracker test suite."""

import os
import sys

# On Windows, expand MSVC and UCRT maximum stdio file descriptors from default 512 to 2048
# to prevent descriptor exhaustion during comprehensive automated test execution.
if os.name == "nt":
    try:
        import ctypes
        ctypes.cdll.msvcrt._setmaxstdio(2048)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.cdll.ucrtbase._setmaxstdio(2048)
    except Exception:
        pass
