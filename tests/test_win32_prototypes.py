"""Tests for pointer-sized Win32 types, structures, and ctypes prototypes."""

import ctypes
import platform
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.windows.win32 import (
    HWND,
    WPARAM,
    LPARAM,
    LRESULT,
    UINT,
    BOOL,
    HINSTANCE,
    LPCWSTR,
    DWORD,
    LONG,
    POINT,
    MSG,
    WNDCLASSEXW,
    WNDPROC,
    init_win32_prototypes,
)
from vaani_tracker.windows.session import (
    HWND as S_HWND,
    WPARAM as S_WPARAM,
    LPARAM as S_LPARAM,
    LRESULT as S_LRESULT,
    WNDPROC as S_WNDPROC,
    MSG as S_MSG,
    WNDCLASSEXW as S_WNDCLASSEXW,
)
from vaani_tracker.windows.power import (
    HWND as P_HWND,
    WPARAM as P_WPARAM,
    LPARAM as P_LPARAM,
    LRESULT as P_LRESULT,
    WNDPROC as P_WNDPROC,
    MSG as P_MSG,
    WNDCLASSEXW as P_WNDCLASSEXW,
)


class TestWin32Prototypes(unittest.TestCase):

    def test_pointer_sized_types(self):
        is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
        expected_size = 8 if is_64bit else 4

        self.assertEqual(ctypes.sizeof(HWND), expected_size)
        self.assertEqual(ctypes.sizeof(WPARAM), expected_size)
        self.assertEqual(ctypes.sizeof(LPARAM), expected_size)
        self.assertEqual(ctypes.sizeof(LRESULT), expected_size)
        self.assertEqual(ctypes.sizeof(HINSTANCE), expected_size)

        # Cross-module exports consistency
        self.assertIs(S_HWND, HWND)
        self.assertIs(S_WPARAM, WPARAM)
        self.assertIs(S_LPARAM, LPARAM)
        self.assertIs(S_LRESULT, LRESULT)

        self.assertIs(P_HWND, HWND)
        self.assertIs(P_WPARAM, WPARAM)
        self.assertIs(P_LPARAM, LPARAM)
        self.assertIs(P_LRESULT, LRESULT)

    def test_point_structure_layout(self):
        self.assertEqual(ctypes.sizeof(POINT), 8)
        p = POINT(x=100, y=200)
        self.assertEqual(p.x, 100)
        self.assertEqual(p.y, 200)

    def test_msg_structure_size_and_alignment(self):
        is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
        # Standard Win32 MSG struct size: 48 bytes on x64, 28 bytes on x86
        expected_size = 48 if is_64bit else 28
        self.assertEqual(ctypes.sizeof(MSG), expected_size)
        self.assertEqual(ctypes.sizeof(S_MSG), expected_size)
        self.assertEqual(ctypes.sizeof(P_MSG), expected_size)

    def test_wndclassexw_structure_size_and_alignment(self):
        is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
        # Standard Win32 WNDCLASSEXW struct size: 80 bytes on x64, 48 bytes on x86
        expected_size = 80 if is_64bit else 48
        self.assertEqual(ctypes.sizeof(WNDCLASSEXW), expected_size)
        self.assertEqual(ctypes.sizeof(S_WNDCLASSEXW), expected_size)
        self.assertEqual(ctypes.sizeof(P_WNDCLASSEXW), expected_size)

    def test_wndproc_callback_signature(self):
        # Create a dummy callback function using WNDPROC
        called = []

        def my_wnd_proc(hwnd, msg, wparam, lparam):
            called.append((hwnd, msg, wparam, lparam))
            return 42

        cb = WNDPROC(my_wnd_proc)
        # Invoke via ctypes
        result = cb(ctypes.c_void_p(0x1000), 0x0111, 0x2000, 0x3000)
        self.assertEqual(result, 42)
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0], (0x1000, 0x0111, 0x2000, 0x3000))

    @unittest.skipUnless(platform.system() == "Windows", "Requires Windows API libraries")
    def test_windows_api_prototypes_and_overflow_protection(self):
        init_win32_prototypes()

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        wtsapi32 = ctypes.windll.wtsapi32

        # Verify DefWindowProcW prototypes
        self.assertEqual(user32.DefWindowProcW.argtypes, [HWND, UINT, WPARAM, LPARAM])
        self.assertEqual(user32.DefWindowProcW.restype, LRESULT)

        # Verify RegisterClassExW / CreateWindowExW prototypes
        self.assertEqual(user32.RegisterClassExW.argtypes, [ctypes.POINTER(WNDCLASSEXW)])
        self.assertEqual(user32.CreateWindowExW.restype, HWND)

        # Verify WTS prototypes
        self.assertEqual(wtsapi32.WTSRegisterSessionNotification.argtypes, [HWND, DWORD])
        self.assertEqual(wtsapi32.WTSUnRegisterSessionNotification.argtypes, [HWND])

        # Test that large 64-bit integer / pointer arguments do NOT trigger OverflowError
        # (This is the exact regression that previously caused OverflowError in DefWindowProcW)
        large_wparam = 0xFFFFFFFF
        large_lparam = 0x7FFFFFFFFFFFFFFF if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x7FFFFFFF

        try:
            # Passing None/0 hwnd to DefWindowProcW returns 0 without crashing
            ret = user32.DefWindowProcW(None, 0x0000, large_wparam, large_lparam)
            self.assertEqual(ret, 0)
        except ctypes.ArgumentError as e:
            self.fail(f"DefWindowProcW overflow regression failed: {e}")


if __name__ == "__main__":
    unittest.main()

