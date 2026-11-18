"""Win32 API types, structures, and function prototypes for Windows desktop tracking.

Defines exact pointer-sized Win32 types and 64-bit/32-bit compliant structures
to prevent ctypes argument conversion overflows (e.g. DefWindowProcW OverflowError)
and provides thread-message and mutex prototypes for reliable shutdown and single-instance locks.
"""

import ctypes
import logging
import platform

logger = logging.getLogger(__name__)

# Pointer-sized Win32 types
HWND = ctypes.c_void_p
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

# Fixed-width Win32 scalar types
UINT = ctypes.c_uint
BOOL = ctypes.c_int
HINSTANCE = ctypes.c_void_p
HICON = ctypes.c_void_p
HCURSOR = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
DWORD = ctypes.c_ulong
LONG = ctypes.c_long
ATOM = ctypes.c_ushort
HANDLE = ctypes.c_void_p

# Common Win32 Constants
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
ERROR_ALREADY_EXISTS = 183
ERROR_CLASS_ALREADY_EXISTS = 1410

# Standard Win32 Window Procedure callback signature
# LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam);
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

HWND_BROADCAST = 0xFFFF
VAANI_ACTIVATE_MESSAGE = "VaaniDesktopTracker_Activate"


class POINT(ctypes.Structure):
    """Win32 POINT structure."""
    _fields_ = [
        ("x", LONG),
        ("y", LONG),
    ]


class MSG(ctypes.Structure):
    """Win32 MSG structure."""
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
    ]


class WNDCLASSEXW(ctypes.Structure):
    """Win32 WNDCLASSEXW structure."""
    _fields_ = [
        ("cbSize", UINT),
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
        ("hIconSm", HICON),
    ]


_PROTOTYPES_INITIALIZED = False


def init_win32_prototypes() -> None:
    """Configures ctypes function prototypes (argtypes and restype) for Win32 APIs.

    Safe to call multiple times (idempotent). Does nothing on non-Windows platforms.
    """
    global _PROTOTYPES_INITIALIZED
    if _PROTOTYPES_INITIALIZED:
        return
    if platform.system() != "Windows":
        return

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        wtsapi32 = ctypes.windll.wtsapi32

        # user32.DefWindowProcW
        user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        # kernel32.GetModuleHandleW
        kernel32.GetModuleHandleW.argtypes = [LPCWSTR]
        kernel32.GetModuleHandleW.restype = HINSTANCE

        # kernel32.GetCurrentThreadId
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = DWORD

        # kernel32.GetLastError
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = DWORD

        # kernel32 Mutex functions for single-instance lock
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, BOOL, LPCWSTR]
        kernel32.CreateMutexW.restype = HANDLE

        kernel32.ReleaseMutex.argtypes = [HANDLE]
        kernel32.ReleaseMutex.restype = BOOL

        kernel32.CloseHandle.argtypes = [HANDLE]
        kernel32.CloseHandle.restype = BOOL

        # user32.RegisterClassExW
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW.restype = ATOM

        # user32.UnregisterClassW
        user32.UnregisterClassW.argtypes = [LPCWSTR, HINSTANCE]
        user32.UnregisterClassW.restype = BOOL

        # user32.CreateWindowExW
        user32.CreateWindowExW.argtypes = [
            DWORD, LPCWSTR, LPCWSTR, DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            HWND, ctypes.c_void_p, HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = HWND

        # user32.DestroyWindow
        user32.DestroyWindow.argtypes = [HWND]
        user32.DestroyWindow.restype = BOOL

        # user32.GetMessageW
        user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT]
        user32.GetMessageW.restype = BOOL

        # user32.PeekMessageW
        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT, UINT]
        user32.PeekMessageW.restype = BOOL

        # user32.TranslateMessage
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.TranslateMessage.restype = BOOL

        # user32.DispatchMessageW
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.restype = LRESULT

        # user32.PostMessageW
        user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        user32.PostMessageW.restype = BOOL

        # user32.PostThreadMessageW
        user32.PostThreadMessageW.argtypes = [DWORD, UINT, WPARAM, LPARAM]
        user32.PostThreadMessageW.restype = BOOL

        # user32.RegisterWindowMessageW
        user32.RegisterWindowMessageW.argtypes = [LPCWSTR]
        user32.RegisterWindowMessageW.restype = UINT

        # user32.FindWindowExW
        user32.FindWindowExW.argtypes = [HWND, HWND, LPCWSTR, LPCWSTR]
        user32.FindWindowExW.restype = HWND

        # user32.GetWindowThreadProcessId
        user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
        user32.GetWindowThreadProcessId.restype = DWORD

        # user32.AllowSetForegroundWindow
        user32.AllowSetForegroundWindow.argtypes = [DWORD]
        user32.AllowSetForegroundWindow.restype = BOOL

        # wtsapi32.WTSRegisterSessionNotification
        wtsapi32.WTSRegisterSessionNotification.argtypes = [HWND, DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = BOOL

        # wtsapi32.WTSUnRegisterSessionNotification
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [HWND]
        wtsapi32.WTSUnRegisterSessionNotification.restype = BOOL

        _PROTOTYPES_INITIALIZED = True
    except Exception:
        raise


_DPI_INITIALIZED = False


def reset_dpi_state() -> None:
    """Resets DPI initialization flag for isolated unit testing."""
    global _DPI_INITIALIZED
    _DPI_INITIALIZED = False


def enable_high_dpi_awareness() -> bool:
    """Configures high-DPI awareness on Windows before GUI creation.

    Tries modern Per-Monitor v2 first, then Per-Monitor v1 (shcore),
    then system DPI aware, with graceful fallback if running on older OS
    or non-Windows. Thread-safe and idempotent.
    """
    global _DPI_INITIALIZED
    if _DPI_INITIALIZED:
        return True

    if platform.system() != "Windows":
        _DPI_INITIALIZED = True
        return False

    try:
        # 1. Try Windows 10 Creators Update+ Per-Monitor V2
        try:
            user32 = ctypes.windll.user32
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            dpi_ctx = ctypes.c_void_p(-4)
            if hasattr(user32, "SetProcessDpiAwarenessContext"):
                user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user32.SetProcessDpiAwarenessContext.restype = BOOL
                res = user32.SetProcessDpiAwarenessContext(dpi_ctx)
                if res:
                    _DPI_INITIALIZED = True
                    logger.debug("Configured Windows Per-Monitor v2 DPI awareness.")
                    return True
        except Exception as e:
            logger.debug("SetProcessDpiAwarenessContext not available: %s", e)

        # 2. Try Windows 8.1+ SetProcessDpiAwareness via shcore
        try:
            shcore = ctypes.windll.shcore
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            if hasattr(shcore, "SetProcessDpiAwareness"):
                shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                shcore.SetProcessDpiAwareness.restype = ctypes.c_long  # HRESULT
                res = shcore.SetProcessDpiAwareness(2)
                if res == 0:  # S_OK
                    _DPI_INITIALIZED = True
                    logger.debug("Configured Windows Per-Monitor v1 DPI awareness via shcore.")
                    return True
        except Exception as e:
            logger.debug("SetProcessDpiAwareness not available: %s", e)

        # 3. Fallback to Windows Vista+ SetProcessDPIAware
        try:
            user32 = ctypes.windll.user32
            if hasattr(user32, "SetProcessDPIAware"):
                user32.SetProcessDPIAware.argtypes = []
                user32.SetProcessDPIAware.restype = BOOL
                res = user32.SetProcessDPIAware()
                if res:
                    _DPI_INITIALIZED = True
                    logger.debug("Configured system DPI awareness via SetProcessDPIAware.")
                    return True
        except Exception as e:
            logger.debug("SetProcessDPIAware failed: %s", e)

    except Exception as e:
        logger.warning(f"Could not set Windows DPI awareness: {e}")

    _DPI_INITIALIZED = True
    return False
