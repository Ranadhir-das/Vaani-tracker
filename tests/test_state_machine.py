"""Tests for TrackingStateMachine enforcing shift, idle, lock, and sleep rules."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import TrackerState, ShiftStatus
from vaani_tracker.models import ShiftInfo
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier


class TestTrackingStateMachine(unittest.TestCase):

    def setUp(self):
        self.mock_idle_seconds = 0.0
        self.idle_detector = WindowsIdleDetector(idle_provider=lambda: self.mock_idle_seconds)
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()

        self.sm = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=self.session_notifier,
            power_notifier=self.power_notifier,
            idle_threshold_seconds=60.0,
        )

    def test_initial_state_without_shift_is_stopped(self):
        self.assertEqual(self.sm.current_state, TrackerState.STOPPED)
        # Even with user typing/moving mouse, without active shift it must be STOPPED
        self.mock_idle_seconds = 0.0
        self.assertEqual(self.sm.evaluate_state(), TrackerState.STOPPED)

    def test_start_shift_transitions_to_active(self):
        shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE, started_at="2026-11-02T09:00:00Z")
        self.sm.update_shift_status(shift)

        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

    def test_idle_detection_at_60_seconds(self):
        shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.sm.update_shift_status(shift)

        # User is active (< 60s)
        self.mock_idle_seconds = 10.0
        self.assertEqual(self.sm.evaluate_state(), TrackerState.ACTIVE)
        self.mock_idle_seconds = 59.9
        self.assertEqual(self.sm.evaluate_state(), TrackerState.ACTIVE)

        # 60 seconds without input indicates IDLE
        self.mock_idle_seconds = 60.0
        self.assertEqual(self.sm.evaluate_state(), TrackerState.IDLE)
        self.mock_idle_seconds = 120.0
        self.assertEqual(self.sm.evaluate_state(), TrackerState.IDLE)

        # User moves mouse -> back to ACTIVE
        self.mock_idle_seconds = 2.0
        self.assertEqual(self.sm.evaluate_state(), TrackerState.ACTIVE)

    def test_workstation_lock_indicates_locked(self):
        shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.sm.update_shift_status(shift)

        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # Workstation locked
        self.session_notifier.trigger_lock()
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

        # Workstation unlocked
        self.session_notifier.trigger_unlock()
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

    def test_system_sleep_indicates_sleeping(self):
        shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.sm.update_shift_status(shift)

        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # System enters suspend / sleep
        self.power_notifier.trigger_suspend()
        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)

        # System resumes
        self.power_notifier.trigger_resume()
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

    def test_break_pauses_work_tracking(self):
        shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.sm.update_shift_status(shift)
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # Employee starts break on CRM
        break_shift = ShiftInfo(session_id=1, status=ShiftStatus.PAUSED, current_break_name="Tea Break")
        self.sm.update_shift_status(break_shift)
        self.assertEqual(self.sm.current_state, TrackerState.PAUSED)

        # Employee resumes work
        resumed_shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.sm.update_shift_status(resumed_shift)
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

    def test_end_shift_stops_tracking(self):
        shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.sm.update_shift_status(shift)
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # Employee ends shift on CRM
        completed_shift = ShiftInfo(session_id=1, status=ShiftStatus.COMPLETED)
        self.sm.update_shift_status(completed_shift)
        self.assertEqual(self.sm.current_state, TrackerState.STOPPED)

    def test_state_change_listeners_fired(self):
        transitions = []
        self.sm.add_state_listener(lambda old_s, new_s: transitions.append((old_s, new_s)))

        # Start shift
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        self.assertEqual(transitions[-1], (TrackerState.STOPPED, TrackerState.ACTIVE))

        # Inactivity 60s
        self.mock_idle_seconds = 65.0
        self.sm.evaluate_state()
        self.assertEqual(transitions[-1], (TrackerState.ACTIVE, TrackerState.IDLE))

        # Lock
        self.session_notifier.trigger_lock()
        self.assertEqual(transitions[-1], (TrackerState.IDLE, TrackerState.LOCKED))

    def test_concurrent_state_transitions_are_serialized_and_ordered(self):
        """Concurrent calls to state machine maintain contiguous, strictly-ordered transitions without gaps or duplicates."""
        import threading
        import time

        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))

        transitions = []
        errors = []
        lock = threading.Lock()

        def record_transition(old_s, new_s):
            with lock:
                transitions.append((old_s, new_s))

        self.sm.add_state_listener(record_transition)

        def worker_lock():
            try:
                for _ in range(20):
                    self.session_notifier.trigger_lock()
                    time.sleep(0.001)
                    self.session_notifier.trigger_unlock()
            except Exception as e:
                errors.append(e)

        def worker_power():
            try:
                for _ in range(20):
                    self.power_notifier.trigger_suspend()
                    time.sleep(0.001)
                    self.power_notifier.trigger_resume()
            except Exception as e:
                errors.append(e)

        def worker_idle():
            try:
                for i in range(20):
                    self.mock_idle_seconds = 75.0 if i % 2 == 0 else 0.0
                    self.sm.evaluate_state()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def worker_shifts():
            try:
                for i in range(20):
                    st = ShiftStatus.PAUSED if i % 2 == 0 else ShiftStatus.ACTIVE
                    self.sm.update_shift_status(ShiftInfo(session_id=1, status=st))
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker_lock),
            threading.Thread(target=worker_power),
            threading.Thread(target=worker_idle),
            threading.Thread(target=worker_shifts),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent state updates caused errors: {errors}")

        # Verify strict FIFO continuity: every transition's target must match next transition's source
        self.assertGreater(len(transitions), 0)
        for i in range(len(transitions) - 1):
            cur_old, cur_new = transitions[i]
            next_old, next_new = transitions[i + 1]
            self.assertNotEqual(cur_old, cur_new, f"Transition {i} has identical states ({cur_old} -> {cur_new})")
            self.assertEqual(cur_new, next_old, f"Discontinuity at index {i}: {cur_new} != {next_old}")

    def test_reentrant_evaluate_state_from_listener_does_not_deadlock(self):
        """A state listener calling evaluate_state() or current_state does not cause recursive deadlocks."""
        reentrant_calls = []

        def reentrant_listener(old_s, new_s):
            # Read state inside listener
            st = self.sm.current_state
            reentrant_calls.append((old_s, new_s, st))
            # Call evaluate_state reentrantly
            self.sm.evaluate_state()

        self.sm.add_state_listener(reentrant_listener)

        # Trigger transition
        self.sm.update_shift_status(ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE))
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)
        self.assertGreater(len(reentrant_calls), 0)


if __name__ == "__main__":
    unittest.main()

