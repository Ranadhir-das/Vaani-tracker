"""Tests for session and power event dispatch, callbacks, and message routing."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import (
    WM_WTSSESSION_CHANGE,
    WTS_SESSION_LOCK,
    WTS_SESSION_UNLOCK,
    WTS_SESSION_LOGOFF,
    WM_POWERBROADCAST,
    PBT_APMSUSPEND,
    PBT_APMRESUMEAUTOMATIC,
    PBT_APMRESUMESUSPEND,
)
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier


class TestSessionAndPowerDispatch(unittest.TestCase):

    def test_session_notifier_callbacks_and_state(self):
        notifier = WindowsSessionNotifier()
        locks = []
        unlocks = []
        logoffs = []

        notifier.on_lock(lambda: locks.append(1))
        notifier.on_unlock(lambda: unlocks.append(1))
        notifier.on_logoff(lambda: logoffs.append(1))

        self.assertFalse(notifier.is_locked)

        # Trigger lock
        notifier.trigger_lock()
        self.assertTrue(notifier.is_locked)
        self.assertEqual(len(locks), 1)
        self.assertEqual(len(unlocks), 0)

        # Trigger unlock
        notifier.trigger_unlock()
        self.assertFalse(notifier.is_locked)
        self.assertEqual(len(locks), 1)
        self.assertEqual(len(unlocks), 1)

        # Trigger logoff
        notifier.trigger_logoff()
        self.assertFalse(notifier.is_locked)
        self.assertEqual(len(logoffs), 1)

    def test_session_callback_exception_isolation(self):
        notifier = WindowsSessionNotifier()

        def failing_callback():
            raise RuntimeError("Callback crash simulation")

        success_calls = []
        notifier.on_lock(failing_callback)
        notifier.on_lock(lambda: success_calls.append(1))

        # Must not raise exception, second callback must still execute
        notifier.trigger_lock()
        self.assertTrue(notifier.is_locked)
        self.assertEqual(len(success_calls), 1)

    def test_power_notifier_callbacks_and_state(self):
        notifier = WindowsPowerNotifier()
        suspends = []
        resumes = []

        notifier.on_suspend(lambda: suspends.append(1))
        notifier.on_resume(lambda: resumes.append(1))

        self.assertFalse(notifier.is_sleeping)

        # Trigger suspend
        notifier.trigger_suspend()
        self.assertTrue(notifier.is_sleeping)
        self.assertEqual(len(suspends), 1)
        self.assertEqual(len(resumes), 0)

        # Trigger resume
        notifier.trigger_resume()
        self.assertFalse(notifier.is_sleeping)
        self.assertEqual(len(suspends), 1)
        self.assertEqual(len(resumes), 1)

    def test_power_callback_exception_isolation(self):
        notifier = WindowsPowerNotifier()

        def failing_callback():
            raise ValueError("Power crash simulation")

        success_calls = []
        notifier.on_suspend(failing_callback)
        notifier.on_suspend(lambda: success_calls.append(1))

        notifier.trigger_suspend()
        self.assertTrue(notifier.is_sleeping)
        self.assertEqual(len(success_calls), 1)


if __name__ == "__main__":
    unittest.main()

