"""Win32 API types, structures, and function prototypes for Windows desktop tracking.

Defines exact pointer-sized Win32 types and 64-bit/32-bit compliant structures
to prevent ctypes argument conversion overflows (e.g. DefWindowProcW OverflowError).
"""

import ctypes
import platform

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

# Standard Win32 Window Procedure callback signature
# LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam);
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)


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

        # kernel32.GetLastError
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = DWORD

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

        # user32.TranslateMessage
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.TranslateMessage.restype = BOOL

        # user32.DispatchMessageW
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.restype = LRESULT

        # user32.PostMessageW
        user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        user32.PostMessageW.restype = BOOL

        # wtsapi32.WTSRegisterSessionNotification
        wtsapi32.WTSRegisterSessionNotification.argtypes = [HWND, DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = BOOL

        # wtsapi32.WTSUnRegisterSessionNotification
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [HWND]
        wtsapi32.WTSUnRegisterSessionNotification.restype = BOOL

        _PROTOTYPES_INITIALIZED = True
    except Exception:
        raise

