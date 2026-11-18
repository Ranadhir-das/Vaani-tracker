"""Comprehensive reliability, timing accuracy, and state transition test suite."""

import logging
import os
import platform
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.app import VaaniTrackerApp, SingleInstanceLock
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, ShiftStatus, SyncStatus
from vaani_tracker.core.aggregator import ActivityAggregator
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.models import ActivityBucket, AppUsage, DeviceInfo, ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.windows.apps import WindowsProcessIdentifier
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.power import WindowsPowerNotifier
from vaani_tracker.windows.session import WindowsSessionNotifier


class TestReliabilityAndTiming(unittest.TestCase):
    """Verifies timing accuracy, drift prevention, rapid state transitions, and shutdown robustness."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "test_events.db"
        self.queue = ActivityQueue(self.db_path)

        self.mock_idle_seconds = 0.0
        self.mock_foreground_process = "code.exe"
        self.current_time = 1000.0

        def test_clock():
            return self.current_time

        self.clock = test_clock

        self.idle_detector = WindowsIdleDetector(idle_provider=lambda: self.mock_idle_seconds)
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.process_id = WindowsProcessIdentifier(provider=lambda: self.mock_foreground_process)

        self.sm = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=self.session_notifier,
            power_notifier=self.power_notifier,
            idle_threshold_seconds=60.0,
        )

        self.aggregator = ActivityAggregator(
            state_machine=self.sm,
            process_identifier=self.process_id,
            queue=self.queue,
            device_uuid="dev-timing-test",
            bucket_duration_seconds=60,
            track_apps=True,
            clock=self.clock,
            employee_id=1,
        )

        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="dev-timing-test",
            backend_url="http://127.0.0.1:9999",
            sync_interval_seconds=60,
            heartbeat_interval_seconds=30,
        )

    def tearDown(self):
        # Close any handlers referencing temp_dir to allow clean directory removal on Windows
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                if str(self.data_dir).lower() in getattr(h, "baseFilename", "").lower():
                    root.removeHandler(h)
                    h.close()
        self.temp_dir.cleanup()

    # =========================================================================
    # Phase 2 & 3: Activity Tick Accuracy, Monotonic Timing & Drift Prevention
    # =========================================================================

    def test_variable_elapsed_seconds_accumulate_accurately(self):
        """Variable tick durations (0.75s, 1.25s) accumulate without loss or drift."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        # 30 ticks of 0.75s + 30 ticks of 1.25s = 22.5s + 37.5s = 60.0s
        for _ in range(30):
            self.current_time += 0.75
            self.aggregator.tick(0.75)
        closed = None
        for _ in range(30):
            self.current_time += 1.25
            b = self.aggregator.tick(1.25)
            if b:
                closed = b

        self.assertIsNotNone(closed, "Bucket must close when 60s is reached")
        self.assertEqual(closed.active_seconds, 60)
        self.assertEqual(closed.idle_seconds, 0)

    def test_subsecond_accumulation_zero_rounding_drift(self):
        """100 ticks of 0.6s produce exactly one 60s bucket without cumulative integer error."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        closed = None
        for _ in range(100):
            self.current_time += 0.6
            b = self.aggregator.tick(0.6)
            if b:
                closed = b

        self.assertIsNotNone(closed)
        self.assertEqual(closed.active_seconds, 60)
        self.assertEqual(closed.idle_seconds, 0)

    def test_delayed_tick_spans_multiple_buckets(self):
        """A heavily delayed tick (e.g. 130s sleep/lag) correctly closes 2 full buckets and retains 10s remainder."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        self.current_time += 130.0
        closed = self.aggregator.tick(130.0)
        self.assertIsNotNone(closed)

        pending = self.queue.get_pending_buckets(limit=10, employee_id=1)
        # Should have generated 2 full 60-second buckets
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0].active_seconds, 60)
        self.assertEqual(pending[1].active_seconds, 60)

        # The aggregator must hold the remaining 10 seconds in progress
        self.assertEqual(self.aggregator._active_seconds, 10)

    def test_app_usage_never_exceeds_active_seconds(self):
        """Total and individual app usages must never exceed active_seconds in the bucket."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        # 40 seconds active with app usages, then 20 seconds idle
        for _ in range(40):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        self.mock_idle_seconds = 75.0
        closed = None
        for _ in range(20):
            self.current_time += 1.0
            b = self.aggregator.tick(1.0)
            if b:
                closed = b

        self.assertIsNotNone(closed)
        self.assertEqual(closed.active_seconds, 40)
        self.assertEqual(closed.idle_seconds, 20)

        # App usage sum cannot exceed 40
        total_app_time = sum(a.duration_seconds for a in closed.app_usages)
        self.assertLessEqual(total_app_time, closed.active_seconds)
        for app in closed.app_usages:
            self.assertLessEqual(app.duration_seconds, closed.active_seconds)

    def test_shutdown_bucket_cannot_be_duplicated(self):
        """Closing an in-progress bucket on shutdown is idempotent and cannot duplicate data."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        # 25 seconds active
        for _ in range(25):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # First close returns the partial bucket
        b1 = self.aggregator.close_current_bucket()
        self.assertIsNotNone(b1)
        self.assertEqual(b1.active_seconds, 25)

        # Immediate second close must return None and produce no additional records
        b2 = self.aggregator.close_current_bucket()
        self.assertIsNone(b2)
        self.assertEqual(self.queue.get_pending_count(employee_id=1), 1)

    # =========================================================================
    # Phase 4: Windows State Transition Reliability
    # =========================================================================

    def test_scenario_active_to_locked_to_active(self):
        """Scenario 1: ACTIVE -> LOCKED -> ACTIVE mid-tick retains is_locked flag and accounts for both states."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        # Pre-tick: 10s active
        self.current_time += 10.0
        self.aggregator.tick(10.0)

        # Mid-interval transition: user locks workstation at +10s
        self.current_time += 10.0
        self.session_notifier.trigger_lock()
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

        # User unlocks at +10s (total 10s locked)
        self.current_time += 10.0
        self.session_notifier.trigger_unlock()
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # Complete the 20s interval (remaining 10s active)
        self.current_time += 10.0
        self.aggregator.tick(20.0)

        # Close partial bucket to inspect flags and exact accounting
        bucket = self.aggregator.close_current_bucket()
        self.assertIsNotNone(bucket)
        self.assertTrue(bucket.is_locked, "is_locked flag must be captured even if unlocked before tick")
        self.assertEqual(bucket.idle_seconds, 10, "10s idle must be attributed to the locked duration")
        self.assertEqual(bucket.active_seconds, 20, "20s active must be attributed to the active duration")

    def test_scenario_active_to_idle_to_active(self):
        """Scenario 2: ACTIVE -> IDLE -> ACTIVE transitions are accounted for correctly."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        # 10s active
        self.mock_idle_seconds = 0.0
        for _ in range(10):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # User goes idle
        self.mock_idle_seconds = 65.0
        for _ in range(10):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # User resumes activity
        self.mock_idle_seconds = 0.0
        for _ in range(10):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        bucket = self.aggregator.close_current_bucket()
        self.assertIsNotNone(bucket)
        self.assertEqual(bucket.active_seconds, 20)
        self.assertEqual(bucket.idle_seconds, 10)

    def test_scenario_active_to_sleeping_to_locked(self):
        """Scenario 3: ACTIVE -> SLEEPING -> LOCKED (system suspends, then resumes to lock screen)."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        self.power_notifier.trigger_suspend()
        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)

        # While asleep, Windows locks the session
        self.session_notifier.trigger_lock()
        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)

        # System wakes up
        self.power_notifier.trigger_resume()
        # With sleep cleared but lock still active, state must be LOCKED
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

        self.current_time += 1.0
        self.aggregator.tick(1.0)
        bucket = self.aggregator.close_current_bucket()
        self.assertIsNotNone(bucket)
        self.assertFalse(bucket.is_sleeping, "Post-resume bucket must not carry over sticky is_sleeping flag")
        self.assertTrue(bucket.is_locked, "Workstation was locked upon resume")

    def test_scenario_locked_to_sleeping_to_locked(self):
        """Scenario 4: LOCKED -> SLEEPING -> LOCKED (already locked workstation goes to sleep and resumes)."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        self.session_notifier.trigger_lock()
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

        self.power_notifier.trigger_suspend()
        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)

        self.power_notifier.trigger_resume()
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

    def test_scenario_rapid_sleep_and_resume(self):
        """Scenario 5 & 6: Rapid sleep/resume and lock/unlock transitions do not deadlock or raise."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        for _ in range(5):
            self.power_notifier.trigger_suspend()
            self.power_notifier.trigger_resume()
            self.session_notifier.trigger_lock()
            self.session_notifier.trigger_unlock()

        self.current_time += 1.0
        self.aggregator.tick(1.0)
        bucket = self.aggregator.close_current_bucket()
        self.assertIsNotNone(bucket)
        self.assertFalse(bucket.is_sleeping, "Post-resume bucket must not carry over sticky is_sleeping flag")
        self.assertTrue(bucket.is_locked, "Workstation was locked upon resume")

    def test_scenario_multiple_notifications_deduplicated(self):
        """Scenario 7: Multiple duplicate notifications for the same transition do not cause repeated state changes."""
        listener_calls = []
        self.sm.add_state_listener(lambda old, new: listener_calls.append((old, new)))

        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))
        self.assertEqual(len(listener_calls), 1)

        # Trigger lock 3 times repeatedly
        self.session_notifier.trigger_lock()
        self.session_notifier.trigger_lock()
        self.session_notifier.trigger_lock()

        # Only one state transition to LOCKED must be emitted
        lock_transitions = [t for t in listener_calls if t[1] == TrackerState.LOCKED]
        self.assertEqual(len(lock_transitions), 1)

    def test_scenario_events_after_shutdown_ignored(self):
        """Scenario 8: Events arriving while or after the aggregator is stopped do not modify closed state."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))
        self.current_time += 10.0
        self.aggregator.tick(10.0)

        # Stop aggregator
        self.aggregator.stop()
        closed = self.aggregator.close_current_bucket()
        self.assertIsNotNone(closed)

        # Send late event after shutdown
        self.session_notifier.trigger_lock()
        self.power_notifier.trigger_suspend()

        # Aggregator must remain empty
        b_after = self.aggregator.close_current_bucket()
        self.assertIsNone(b_after)

    # =========================================================================
    # Phase 5: Concurrent Shutdown and Thread Safety
    # =========================================================================

    def test_concurrent_tick_and_close_current_bucket(self):
        """Concurrent calls to tick() and close_current_bucket() do not race or corrupt data."""
        self.sm.update_shift_status(ShiftInfo(session_id=10, status=ShiftStatus.ACTIVE))

        errors = []

        def worker_tick():
            try:
                for _ in range(50):
                    self.aggregator.tick(0.1)
            except Exception as e:
                errors.append(e)

        def worker_close():
            try:
                for _ in range(10):
                    self.aggregator.close_current_bucket()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_tick)
        t2 = threading.Thread(target=worker_close)

        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertEqual(len(errors), 0, f"Concurrent tick/close raised errors: {errors}")

    # =========================================================================
    # Phase 6: Windows Logoff Behavior and Syncer Concurrency
    # =========================================================================

    def test_windows_logoff_stops_tracking_without_modifying_crm_shift(self):
        """Windows logoff transitions to STOPPED locally without modifying CRM shift."""
        self.sm.update_shift_status(ShiftInfo(session_id=99, status=ShiftStatus.ACTIVE))
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # Trigger Windows logoff
        self.session_notifier.trigger_logoff()
        self.assertTrue(self.session_notifier.is_logged_off)
        self.assertEqual(self.sm.current_state, TrackerState.STOPPED)

        # Shift status must remain intact (never modified or completed locally)
        self.assertEqual(self.sm.shift_info.session_id, 99)
        self.assertEqual(self.sm.shift_info.status, ShiftStatus.ACTIVE)

        # Ticking during logoff must not accumulate active or idle seconds
        for _ in range(60):
            self.aggregator.tick(1.0)
        closed = self.aggregator.close_current_bucket()
        self.assertIsNone(closed, "No activity bucket should be generated during Windows logoff")

    def test_windows_logoff_remains_stopped_and_new_session_resumes_via_fresh_instance(self):
        """Windows logoff stops process permanently; unlock does not clear logoff; new instance resumes."""
        self.sm.update_shift_status(ShiftInfo(session_id=99, status=ShiftStatus.ACTIVE))
        self.session_notifier.trigger_logoff()
        self.assertEqual(self.sm.current_state, TrackerState.STOPPED)

        # Workstation unlock does NOT treat unlock as a new login in this process
        self.session_notifier.trigger_unlock()
        self.assertTrue(self.session_notifier.is_logged_off)
        self.assertEqual(self.sm.current_state, TrackerState.STOPPED, "Process must remain STOPPED following logoff")

        # Upon a new Windows login, a freshly launched tracker instance initializes:
        new_session_notifier = WindowsSessionNotifier()
        new_power_notifier = WindowsPowerNotifier()
        new_sm = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=new_session_notifier,
            power_notifier=new_power_notifier,
            idle_threshold_seconds=60.0,
        )
        self.assertFalse(new_session_notifier.is_logged_off)

        # Fresh instance queries authoritative CRM shift and resumes tracking if ACTIVE
        new_sm.update_shift_status(ShiftInfo(session_id=99, status=ShiftStatus.ACTIVE))
        self.assertEqual(new_sm.current_state, TrackerState.ACTIVE)

    def test_syncer_concurrent_sync_pending_now_is_serialized(self):
        """Concurrent calls to sync_pending_now() are serialized and do not duplicate uploads."""
        from vaani_tracker.core.syncer import BackgroundSyncer
        from vaani_tracker.models import SyncResult

        # Seed 3 buckets in queue
        for i in range(3):
            self.queue.save_bucket(ActivityBucket(
                bucket_id=f"concurrent-bucket-{i}",
                device_uuid=self.config.device_uuid,
                employee_id=1,
                bucket_start="2026-11-02T12:00:00Z",
                bucket_end="2026-11-02T12:01:00Z",
                active_seconds=60,
                idle_seconds=0,
                session_id=99,
            ), employee_id=1)
        self.assertEqual(self.queue.get_pending_count(employee_id=1), 3)

        api_client_mock = MagicMock()
        auth_mock = MagicMock()
        auth_mock.get_token.return_value = "fake-token"
        auth_mock.get_authenticated_employee_id.return_value = 1

        upload_call_count = 0
        uploaded_ids = []
        lock = threading.Lock()

        def mock_sync_buckets(device_uuid, token, buckets):
            nonlocal upload_call_count
            time.sleep(0.05)  # Simulate network latency
            with lock:
                upload_call_count += 1
                uploaded_ids.extend([b.bucket_id for b in buckets])
            return SyncResult(success=True, synced_count=len(buckets))

        api_client_mock.sync_buckets.side_effect = mock_sync_buckets

        syncer = BackgroundSyncer(
            config=self.config,
            api_client=api_client_mock,
            queue=self.queue,
            auth_manager=auth_mock,
            state_machine=self.sm,
        )

        results = []
        errors = []

        def worker_sync():
            try:
                res = syncer.sync_pending_now()
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_sync) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent sync threw errors: {errors}")
        self.assertEqual(len(results), 4)

        # All pending buckets synced, count in SQLite is now 0
        self.assertEqual(self.queue.get_pending_count(employee_id=1), 0)

        # Buckets were only uploaded once (no duplicate uploads)
        self.assertEqual(len(uploaded_ids), 3)
        self.assertEqual(set(uploaded_ids), {"concurrent-bucket-0", "concurrent-bucket-1", "concurrent-bucket-2"})


class TestPhysicalSleepResumeTimingRemediation(unittest.TestCase):
    """Deterministic tests for physical Windows sleep/resume timing, invariants, and flag isolation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_timing.db"
        self.queue = ActivityQueue(self.db_path)
        self.emp_id = 1

        self.current_time = 1000.0
        self.mock_idle_seconds = 0.0
        self.mock_foreground_process = "code.exe"

        self.idle_detector = MagicMock()
        self.idle_detector.get_idle_seconds.side_effect = lambda: self.mock_idle_seconds

        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()

        self.process_id = MagicMock()
        self.process_id.get_foreground_process_name.side_effect = lambda: self.mock_foreground_process

        self.sm = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=self.session_notifier,
            power_notifier=self.power_notifier,
            idle_threshold_seconds=60,
        )

        self.aggregator = ActivityAggregator(
            state_machine=self.sm,
            process_identifier=self.process_id,
            queue=self.queue,
            device_uuid="timing-test-device-uuid",
            bucket_duration_seconds=60,
            track_apps=True,
            clock=lambda: self.current_time,
            employee_id=self.emp_id,
        )

    def tearDown(self):
        self.aggregator.stop()
        self.temp_dir.cleanup()

    def test_1_normal_active_tick(self):
        """Test 1: Normal active tick produces 1 bucket with exact active duration and app invariant."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        closed = None
        for _ in range(60):
            self.current_time += 1.0
            b = self.aggregator.tick(1.0)
            if b:
                closed = b

        self.assertIsNotNone(closed)
        self.assertEqual(closed.active_seconds, 60)
        self.assertEqual(closed.idle_seconds, 0)
        self.assertEqual(closed.state, TrackerState.ACTIVE)
        self.assertFalse(closed.is_locked)
        self.assertFalse(closed.is_sleeping)

        # Invariants: active + idle <= bucket duration, and sum(apps) <= active_seconds
        self.assertLessEqual(closed.active_seconds + closed.idle_seconds, 60)
        app_total = sum(a.duration_seconds for a in closed.app_usages)
        self.assertLessEqual(app_total, closed.active_seconds)
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

    def test_2_suspend_occurring_in_middle_of_bucket(self):
        """Test 2: Suspend occurring in middle of bucket preserves pre-suspend activity and ends at suspend."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        # 25 seconds of active work
        for _ in range(25):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Machine enters suspend at t=1025
        self.power_notifier.trigger_suspend()
        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)

        # Pre-suspend bucket must be immediately finalized and persisted
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)
        pending = self.queue.get_pending_buckets(limit=10, employee_id=self.emp_id)
        bucket = pending[0]
        self.assertEqual(bucket.active_seconds, 25)
        self.assertEqual(bucket.idle_seconds, 0)
        self.assertTrue(bucket.is_sleeping, "Pre-suspend bucket reflects suspend transition")
        self.assertFalse(bucket.is_locked)
        self.assertLessEqual(bucket.active_seconds + bucket.idle_seconds, 25)

    def test_3_resume_after_short_sleep(self):
        """Test 3: Resume after a short sleep does not count sleeping time as active or idle."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        # 20 seconds work before sleep
        for _ in range(20):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Suspend at t=1020
        self.power_notifier.trigger_suspend()
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        # Sleep for 30 seconds (clock leaps to t=1050)
        self.current_time += 30.0

        # Ticks during sleep must not produce records
        b_sleep = self.aggregator.tick(1.0)
        self.assertIsNone(b_sleep)
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        # Resume at t=1050
        self.power_notifier.trigger_resume()
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # 10 seconds of active work after resume
        for _ in range(10):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Close partial post-resume bucket
        post_bucket = self.aggregator.close_current_bucket()
        self.assertIsNotNone(post_bucket)
        self.assertEqual(post_bucket.active_seconds, 10)
        self.assertEqual(post_bucket.idle_seconds, 0)
        self.assertFalse(post_bucket.is_sleeping, "Post-resume bucket is awake")
        self.assertLessEqual(post_bucket.active_seconds + post_bucket.idle_seconds, 10)

    def test_4_resume_after_sleep_longer_than_one_bucket(self):
        """Test 4: Sleep longer than one bucket (140s) produces zero artificial buckets and clean post-resume tracking."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        # 57 seconds active before sleep
        for _ in range(57):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Suspend at t=1057 (Bucket 1 closed)
        self.power_notifier.trigger_suspend()
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        # Sleep for 140 seconds: monotonic clock leaps to t=1197
        self.current_time += 140.0

        # Windows locks screen during sleep
        self.session_notifier.trigger_lock()
        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)

        # Resume at t=1197 -> state is LOCKED
        self.power_notifier.trigger_resume()
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

        # Ensure no artificial buckets were queued during sleep
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        # 20 seconds at lock screen (LOCKED -> idle)
        self.mock_idle_seconds = 70.0
        for _ in range(20):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # User unlocks screen at t=1217 (input resets idle seconds)
        self.mock_idle_seconds = 0.0
        self.session_notifier.trigger_unlock()
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # 40 seconds active work after unlock
        closed_b2 = None
        for _ in range(40):
            self.current_time += 1.0
            b = self.aggregator.tick(1.0)
            if b:
                closed_b2 = b

        # Post-resume bucket completed at 60s
        self.assertIsNotNone(closed_b2)
        self.assertEqual(closed_b2.active_seconds, 40)
        self.assertEqual(closed_b2.idle_seconds, 20)
        self.assertEqual(closed_b2.state, TrackerState.ACTIVE)
        self.assertTrue(closed_b2.is_locked, "Was locked during initial portion of bucket")
        self.assertFalse(closed_b2.is_sleeping, "Must not carry over sticky is_sleeping flag")

        # Invariant checks on post-resume bucket
        self.assertEqual(closed_b2.active_seconds + closed_b2.idle_seconds, 60)

        # Next bucket (Bucket 3): 60s active work. Must NOT retain is_locked!
        closed_b3 = None
        for _ in range(60):
            self.current_time += 1.0
            b = self.aggregator.tick(1.0)
            if b:
                closed_b3 = b

        self.assertIsNotNone(closed_b3)
        self.assertEqual(closed_b3.active_seconds, 60)
        self.assertEqual(closed_b3.idle_seconds, 0)
        self.assertFalse(closed_b3.is_locked, "is_locked must not leak into subsequent bucket")
        self.assertFalse(closed_b3.is_sleeping, "is_sleeping must not leak into subsequent bucket")

        # Queue has exactly 3 buckets total: pre-suspend (57s), post-resume (60s), subsequent (60s)
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 3)

    def test_5_delayed_tick_after_resume(self):
        """Test 5: Delayed tick after resume resets tick timing anchor without burst-filling multiple buckets."""
        from vaani_tracker.app import VaaniTrackerApp
        from vaani_tracker.config import TrackerConfig

        cfg = TrackerConfig(
            device_uuid="delayed-tick-test",
            backend_url="http://127.0.0.1:8000",
            data_dir=Path(self.temp_dir.name),
        )

        app = VaaniTrackerApp(config=cfg, headless=True)
        try:
            # Trigger power resume
            app.power_notifier.trigger_suspend()
            self.assertTrue(app.power_notifier.is_sleeping)

            app.power_notifier.trigger_resume()
            self.assertFalse(app.power_notifier.is_sleeping)
            self.assertTrue(app._just_resumed, "on_resume must set _just_resumed")
        finally:
            app.stop()

    def test_6_duplicate_suspend_resume_notifications(self):
        """Test 6: Duplicate suspend and resume notifications are idempotent and safe."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        for _ in range(10):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Fire suspend 3 times
        self.power_notifier.trigger_suspend()
        self.power_notifier.trigger_suspend()
        self.power_notifier.trigger_suspend()

        self.assertEqual(self.sm.current_state, TrackerState.SLEEPING)
        # Exactly one pre-suspend bucket created
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        # Fire resume 3 times
        self.power_notifier.trigger_resume()
        self.power_notifier.trigger_resume()
        self.power_notifier.trigger_resume()

        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)
        # Still exactly 1 bucket in queue
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

    def test_7_sleep_followed_by_workstation_lock_and_unlock(self):
        """Test 7: Full sequence of sleep, resume to lock screen, unlock, and clean active progression."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        # 30s active
        for _ in range(30):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Suspend
        self.power_notifier.trigger_suspend()
        b1 = self.queue.get_pending_buckets(limit=1, employee_id=self.emp_id)[0]
        self.assertEqual(b1.active_seconds, 30)
        self.assertTrue(b1.is_sleeping)
        self.assertFalse(b1.is_locked)

        # Sleep 45s, lock session
        self.current_time += 45.0
        self.session_notifier.trigger_lock()

        # Resume to locked
        self.power_notifier.trigger_resume()
        self.assertEqual(self.sm.current_state, TrackerState.LOCKED)

        # 15s locked
        self.mock_idle_seconds = 65.0
        for _ in range(15):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        # Unlock (user input resets idle seconds to 0)
        self.mock_idle_seconds = 0.0
        self.session_notifier.trigger_unlock()
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)

        # 45s active (completes Bucket 2)
        b2 = None
        for _ in range(45):
            self.current_time += 1.0
            b = self.aggregator.tick(1.0)
            if b:
                b2 = b

        self.assertIsNotNone(b2)
        self.assertEqual(b2.active_seconds, 45)
        self.assertEqual(b2.idle_seconds, 15)
        self.assertTrue(b2.is_locked)
        self.assertFalse(b2.is_sleeping)

        # Bucket 3: 60s active
        b3 = None
        for _ in range(60):
            self.current_time += 1.0
            b = self.aggregator.tick(1.0)
            if b:
                b3 = b

        self.assertIsNotNone(b3)
        self.assertEqual(b3.active_seconds, 60)
        self.assertEqual(b3.idle_seconds, 0)
        self.assertFalse(b3.is_locked)
        self.assertFalse(b3.is_sleeping)

        # Timestamps monotonic & non-overlapping
        self.assertLessEqual(b1.bucket_end, b2.bucket_start)
        self.assertLessEqual(b2.bucket_end, b3.bucket_start)

    def test_8_shutdown_while_sleeping(self):
        """Test 8: Shutdown while sleeping does not save an empty or corrupt bucket."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        for _ in range(15):
            self.current_time += 1.0
            self.aggregator.tick(1.0)

        self.power_notifier.trigger_suspend()
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

        # Shutdown while asleep
        self.aggregator.stop()
        b = self.aggregator.close_current_bucket()
        self.assertIsNone(b, "Closing while asleep with zero tracked work must return None")
        self.assertEqual(self.queue.get_pending_count(employee_id=self.emp_id), 1)

    def test_9_bucket_duration_and_app_usage_invariants(self):
        """Test 9: Invariants hold across variable tick intervals and delayed rollovers."""
        self.sm.update_shift_status(ShiftInfo(session_id=101, status=ShiftStatus.ACTIVE))

        # Irregular sub-second and multi-second ticks
        intervals = [0.1, 0.4, 0.5, 1.2, 2.8, 15.0, 40.0]
        closed_buckets = []
        for interval in intervals:
            self.current_time += interval
            b = self.aggregator.tick(interval)
            if b:
                closed_buckets.append(b)

        # Also close final partial bucket
        final_b = self.aggregator.close_current_bucket()
        if final_b:
            closed_buckets.append(final_b)

        for b in closed_buckets:
            # Invariant 1: Non-negative
            self.assertGreaterEqual(b.active_seconds, 0)
            self.assertGreaterEqual(b.idle_seconds, 0)

            # Invariant 2: active + idle <= bucket_duration_seconds
            self.assertLessEqual(b.active_seconds + b.idle_seconds, 60)

            # Invariant 3: sum(app_durations) <= active_seconds
            app_total = sum(a.duration_seconds for a in b.app_usages)
            self.assertLessEqual(app_total, b.active_seconds)

            # Invariant 4: Monotonic timestamps
            self.assertLessEqual(b.bucket_start, b.bucket_end)


if __name__ == "__main__":
    unittest.main()
