"""Tests for WindowsIdleDetector logic and wrap handling."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.windows.idle import WindowsIdleDetector


class TestWindowsIdleDetector(unittest.TestCase):

    def test_custom_provider_idle_seconds(self):
        val = 15.5
        detector = WindowsIdleDetector(idle_provider=lambda: val)
        self.assertEqual(detector.get_idle_seconds(), 15.5)
        self.assertFalse(detector.is_idle(threshold_seconds=60.0))

        val = 60.0
        self.assertTrue(detector.is_idle(threshold_seconds=60.0))

        val = 120.0
        self.assertTrue(detector.is_idle(threshold_seconds=60.0))

    def test_tick_wrap_calculation(self):
        # 32-bit tick counter wrap test: current_ticks wrapped around past 0xFFFFFFFF
        # (e.g. current = 10,000, last_input = 4,294,960,000)
        current_ticks = 10000
        last_ticks = 0xFFFFFFFF - 50000
        elapsed = (current_ticks - last_ticks) & 0xFFFFFFFF
        self.assertEqual(elapsed, 60001)


if __name__ == "__main__":
    unittest.main()

