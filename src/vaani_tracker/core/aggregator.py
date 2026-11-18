"""1-minute bucket activity aggregator engine with accurate timing and drift compensation."""

from datetime import datetime, timezone, timedelta
import logging
import sqlite3
import threading
import time
from typing import Any, Optional, Dict, List, Tuple, Callable

from ..constants import TrackerState, DEFAULT_BUCKET_DURATION_SECONDS
from ..models import ActivityBucket, AppUsage, current_utc_iso
from ..privacy import PrivacyGuard
from ..storage.queue import ActivityQueue
from ..windows.apps import WindowsProcessIdentifier
from .state_machine import TrackingStateMachine

logger = logging.getLogger(__name__)


def _add_seconds_to_iso(iso_str: str, seconds: float) -> str:
    """Adds floating-point seconds to an ISO-8601 UTC timestamp string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return (dt + timedelta(seconds=seconds)).isoformat()
    except Exception:
        return current_utc_iso()


class ActivityAggregator:
    """Aggregates hardware activity into 60-second time buckets with high timing accuracy."""

    def __init__(
        self,
        state_machine: TrackingStateMachine,
        process_identifier: WindowsProcessIdentifier,
        queue: ActivityQueue,
        device_uuid: str,
        bucket_duration_seconds: int = DEFAULT_BUCKET_DURATION_SECONDS,
        track_apps: bool = True,
        clock: Optional[Callable[[], float]] = None,
        auth_manager: Optional[Any] = None,
        employee_id: Optional[int] = None,
    ):
        self.state_machine = state_machine
        self.process_identifier = process_identifier
        self.queue = queue
        self.device_uuid = device_uuid
        self.bucket_duration_seconds = bucket_duration_seconds
        self.track_apps = track_apps
        self.auth_manager = auth_manager
        self.employee_id = employee_id
        self._clock = clock or time.monotonic

        self._lock = threading.RLock()
        self._is_stopped = False
        self._evaluating_tick_state = False

        # Current bucket in-progress accumulation (floating-point for sub-second precision)
        self._bucket_start_utc: str = current_utc_iso()
        self._bucket_start_mono: float = self._clock()
        self._bucket_elapsed_seconds: float = 0.0
        self._active_seconds_float: float = 0.0
        self._idle_seconds_float: float = 0.0
        self._is_locked: bool = False
        self._is_sleeping: bool = False
        self._is_paused: bool = False
        self._last_tracking_state: Optional[TrackerState] = None
        self._app_durations_float: Dict[str, float] = {}
        self._current_session_id: Optional[int] = None

        # Fractional remainder carry-over to eliminate cumulative rounding drift
        self._active_carryover: float = 0.0
        self._idle_carryover: float = 0.0

        # Rapid state transition tracking between ticks
        self._pending_transitions: List[Tuple[float, TrackerState, TrackerState]] = []

        # Wire state listener to catch transitions between ticks
        self.state_machine.add_state_listener(self._on_state_transition)

    @property
    def _active_seconds(self) -> int:
        """Backward-compatible integer view of active seconds."""
        return int(round(self._active_seconds_float))

    @property
    def _idle_seconds(self) -> int:
        """Backward-compatible integer view of idle seconds."""
        return int(round(self._idle_seconds_float))

    @property
    def _app_durations(self) -> Dict[str, int]:
        """Backward-compatible integer view of app durations."""
        return {p: int(round(d)) for p, d in self._app_durations_float.items()}

    def stop(self, timeout: Optional[float] = 2.0) -> bool:
        """Marks aggregator as stopped to prevent post-shutdown callbacks from modifying state."""
        acquired = self._lock.acquire(timeout=timeout if timeout is not None else -1)
        if not acquired:
            logger.warning(f"Could not acquire aggregator lock within {timeout}s in stop(); forcing stop flag.")
            self._is_stopped = True
            return False
        try:
            if self._is_stopped:
                return True
            self._is_stopped = True
            self._pending_transitions.clear()
            return True
        finally:
            self._lock.release()

    def _on_state_transition(self, old_state: TrackerState, new_state: TrackerState) -> None:
        """Records state transitions that occur between tick intervals."""
        with self._lock:
            if self._is_stopped:
                return

            # Shift start from STOPPED to active tracking state is a boundary, not an in-tick slice
            if old_state == TrackerState.STOPPED:
                self._pending_transitions.clear()
                self._reset_bucket_accumulators()
                return

            # Shift stop: end shift closes any open bucket
            if new_state == TrackerState.STOPPED:
                if old_state not in (TrackerState.STOPPED, TrackerState.DISCONNECTED):
                    self._last_tracking_state = old_state
                    if old_state == TrackerState.LOCKED:
                        self._is_locked = True
                    elif old_state == TrackerState.PAUSED:
                        self._is_paused = True
                self._pending_transitions.clear()
                try:
                    self.close_current_bucket()
                except (sqlite3.Error, OSError, TimeoutError) as exc:
                    logger.error(f"Failed to close bucket on transition to STOPPED: {exc}")
                self._current_session_id = None
                self._active_carryover = 0.0
                self._idle_carryover = 0.0
                self._reset_bucket_accumulators()
                return

            # System entering SLEEP: finalize and persist any pre-suspend activity immediately.
            # Time before suspend is preserved accurately; sleeping time is not counted.
            if new_state == TrackerState.SLEEPING:
                self._is_sleeping = True
                try:
                    self.close_current_bucket()
                except (sqlite3.Error, OSError, TimeoutError) as exc:
                    logger.error(f"Failed to close bucket on transition to SLEEPING: {exc}")
                self._pending_transitions.clear()
                self._reset_bucket_accumulators()
                return

            # System resuming from SLEEP: start a fresh post-resume bucket anchored at resume time.
            # Time after resume is counted only from the actual resume transition.
            if old_state == TrackerState.SLEEPING:
                self._pending_transitions.clear()
                now_mono = self._clock()
                now_utc = current_utc_iso()
                self._reset_bucket_accumulators(start_utc=now_utc, start_mono=now_mono)
                self._is_sleeping = False
                if new_state == TrackerState.LOCKED:
                    self._is_locked = True
                elif new_state == TrackerState.PAUSED:
                    self._is_paused = True
                return

            # Update sticky flags for the current bucket lifetime
            if new_state == TrackerState.LOCKED:
                self._is_locked = True
            elif new_state == TrackerState.PAUSED:
                self._is_paused = True

            # If the transition occurred during tick's own state evaluation at the start of the tick,
            # this transition applies to the whole upcoming tick slice; no mid-tick splitting needed.
            if self._evaluating_tick_state:
                return

            now_mono = self._clock()
            self._pending_transitions.append((now_mono, old_state, new_state))

    def tick(self, elapsed_seconds: float = 1.0) -> Optional[ActivityBucket]:
        """Processes an activity slice of duration `elapsed_seconds`.

        Returns completed ActivityBucket if a 60-second bucket was closed, otherwise None.
        Handles variable execution duration, delayed ticks, and state transitions safely.
        """
        with self._lock:
            if self._is_stopped or elapsed_seconds <= 0.0:
                return None

            # Re-evaluate state
            self._evaluating_tick_state = True
            try:
                state = self.state_machine.evaluate_state()
            finally:
                self._evaluating_tick_state = False

            shift = self.state_machine.shift_info

            # If shift changed or ended abruptly, close any open bucket for previous session
            if self._current_session_id is not None and self._current_session_id != shift.session_id:
                logger.info("Shift session boundary detected. Closing in-progress bucket.")
                try:
                    closed = self.close_current_bucket()
                except (sqlite3.Error, OSError, TimeoutError) as exc:
                    logger.error(f"Failed to close bucket on shift session change: {exc}")
                    closed = None
                self._current_session_id = shift.session_id
                return closed

            self._current_session_id = shift.session_id

            # If shift is STOPPED, do not accumulate work metrics
            if state == TrackerState.STOPPED:
                self._reset_bucket_accumulators()
                self._pending_transitions.clear()
                return None

            # If state is SLEEPING, do not accumulate work metrics.
            # Sleeping time is not active or idle work.
            if state == TrackerState.SLEEPING:
                self._pending_transitions.clear()
                return None

            # Track sticky flags for current state
            if state == TrackerState.LOCKED:
                self._is_locked = True
            elif state == TrackerState.PAUSED:
                self._is_paused = True

            # Distribute elapsed_seconds across states if transitions occurred between ticks
            time_slices = self._resolve_tick_slices(state, elapsed_seconds)

            closed_bucket = None
            now_clock = self._clock()
            cursor_mono = now_clock - elapsed_seconds

            for slice_state, slice_duration in time_slices:
                if slice_duration <= 0.0:
                    continue

                remaining_slice = slice_duration
                while remaining_slice > 0.0:
                    current_tracked = self._active_seconds_float + self._idle_seconds_float
                    capacity = max(0.0, float(self.bucket_duration_seconds) - current_tracked)

                    if capacity <= 0.0:
                        try:
                            closed_bucket = self.close_current_bucket(end_mono=cursor_mono)
                        except (sqlite3.Error, OSError, TimeoutError) as exc:
                            logger.error(f"Failed to persist completed bucket during tick: {exc}")
                            return closed_bucket
                        capacity = float(self.bucket_duration_seconds)

                    delta = min(remaining_slice, capacity)
                    slice_end_mono = cursor_mono + delta
                    self._apply_slice_to_bucket(slice_state, delta)
                    self._bucket_elapsed_seconds += delta
                    remaining_slice -= delta
                    cursor_mono = slice_end_mono

                    # If bucket duration reached, close and save
                    if (self._active_seconds_float + self._idle_seconds_float) >= self.bucket_duration_seconds:
                        try:
                            closed_bucket = self.close_current_bucket(end_mono=slice_end_mono)
                        except (sqlite3.Error, OSError, TimeoutError) as exc:
                            logger.error(f"Failed to persist completed bucket at boundary during tick: {exc}")
                            return closed_bucket

            return closed_bucket

    def _resolve_tick_slices(
        self, current_state: TrackerState, elapsed_seconds: float
    ) -> List[Tuple[TrackerState, float]]:
        """Resolves the time distribution across states for this tick interval."""
        if not self._pending_transitions:
            return [(current_state, elapsed_seconds)]

        transitions = self._pending_transitions
        self._pending_transitions = []

        now = self._clock()
        start = now - elapsed_seconds

        # Check if transitions occurred with measurable time difference
        slices: List[Tuple[TrackerState, float]] = []
        cur_t = start
        cur_st = transitions[0][1]

        for t_time, old_st, new_st in transitions:
            clamped_t = max(start, min(now, t_time))
            dur = clamped_t - cur_t
            if dur > 1e-4:
                slices.append((cur_st, dur))
            cur_t = clamped_t
            cur_st = new_st

        dur = now - cur_t
        if dur > 1e-4:
            slices.append((cur_st, dur))

        total_dur = sum(d for _, d in slices)
        if total_dur > 1e-4:
            # Scale slices so their sum strictly equals elapsed_seconds
            factor = elapsed_seconds / total_dur
            return [(st, d * factor) for st, d in slices]
        else:
            # Transitions occurred instantaneously (e.g. in a fast unit test)
            # Distribute elapsed_seconds across all states visited during this interval
            states_visited = [transitions[0][1]] + [new_st for _, _, new_st in transitions]
            unique_states = [states_visited[0]]
            for s in states_visited[1:]:
                if s != unique_states[-1]:
                    unique_states.append(s)

            piece = elapsed_seconds / len(unique_states)
            return [(st, piece) for st in unique_states]

    def _apply_slice_to_bucket(self, state: TrackerState, duration: float) -> None:
        """Applies a duration slice to the active or idle accumulation counters."""
        if state not in (TrackerState.STOPPED, TrackerState.DISCONNECTED):
            self._last_tracking_state = state
        if state == TrackerState.ACTIVE:
            self._active_seconds_float += duration
            if self.track_apps:
                proc = self.process_identifier.get_foreground_process_name()
                if proc:
                    clean_proc = PrivacyGuard.sanitize_process_name(proc)
                    self._app_durations_float[clean_proc] = (
                        self._app_durations_float.get(clean_proc, 0.0) + duration
                    )
        elif state in (TrackerState.IDLE, TrackerState.LOCKED):
            self._idle_seconds_float += duration
        # Note: TrackerState.SLEEPING, TrackerState.STOPPED, and TrackerState.PAUSED
        # explicitly accumulate 0 active and 0 idle seconds.

    def _resolve_bucket_state(self, active_int: int, idle_int: int) -> TrackerState:
        """Determines an accurate CRM-supported bucket state based on recorded activity.

        Never returns TrackerState.STOPPED or TrackerState.DISCONNECTED, and never
        misclassifies an idle, locked, sleeping, or paused bucket with 0 active seconds
        as ACTIVE.
        """
        if active_int <= 0:
            if self._is_paused or self._last_tracking_state == TrackerState.PAUSED:
                return TrackerState.PAUSED
            if self._is_sleeping or self._last_tracking_state == TrackerState.SLEEPING:
                return TrackerState.SLEEPING
            if self._is_locked or self._last_tracking_state == TrackerState.LOCKED:
                return TrackerState.LOCKED
            return TrackerState.IDLE

        curr_state = self.state_machine.current_state
        if curr_state in (
            TrackerState.ACTIVE,
            TrackerState.IDLE,
            TrackerState.LOCKED,
            TrackerState.SLEEPING,
            TrackerState.PAUSED,
        ):
            return curr_state

        # Transitioned to STOPPED or DISCONNECTED when closing bucket
        if active_int >= idle_int:
            return TrackerState.ACTIVE
        if self._is_locked or self._last_tracking_state == TrackerState.LOCKED:
            return TrackerState.LOCKED
        if self._is_paused or self._last_tracking_state == TrackerState.PAUSED:
            return TrackerState.PAUSED
        if self._is_sleeping or self._last_tracking_state == TrackerState.SLEEPING:
            return TrackerState.SLEEPING
        return TrackerState.IDLE

    def close_current_bucket(
        self,
        end_utc: Optional[str] = None,
        end_mono: Optional[float] = None,
        lock_timeout: Optional[float] = None,
        db_timeout_ms: Optional[int] = None,
    ) -> Optional[ActivityBucket]:
        """Closes the current window, persists bucket to SQLite queue, and resets.

        Thread-safe; honors lock_timeout and db_timeout_ms to ensure bounded execution during shutdown.
        If persistence fails or times out, accumulators are preserved.
        """
        if lock_timeout is not None:
            acquired = self._lock.acquire(timeout=lock_timeout)
            if not acquired:
                raise TimeoutError(f"ActivityAggregator lock acquisition timed out after {lock_timeout}s.")
        else:
            self._lock.acquire()

        try:
            now_mono = end_mono if end_mono is not None else self._clock()
            elapsed_mono = max(0.0, now_mono - self._bucket_start_mono)

            # Incorporate carryover from previous bucket to eliminate cumulative rounding drift
            raw_active = self._active_seconds_float + self._active_carryover
            raw_idle = self._idle_seconds_float + self._idle_carryover
            total_raw = raw_active + raw_idle

            # Never persist an empty bucket with zero tracked work
            if total_raw <= 0.0:
                self._reset_bucket_accumulators(start_utc=end_utc, start_mono=now_mono)
                return None

            active_int = int(round(raw_active))
            idle_int = int(round(raw_idle))

            next_active_carryover = raw_active - active_int
            next_idle_carryover = raw_idle - idle_int

            # Rule 1 & Invariant: active_int + idle_int <= bucket_duration_seconds
            if (active_int + idle_int) > self.bucket_duration_seconds:
                excess = (active_int + idle_int) - self.bucket_duration_seconds
                if idle_int >= excess:
                    idle_int -= excess
                else:
                    active_int -= (excess - idle_int)
                    idle_int = 0

            # Rule 1 & Invariant: active_int + idle_int <= actual bucket duration (with max 1s rounding tolerance)
            # A bucket cannot record more tracked time than its physical elapsed duration or accumulated tick duration.
            elapsed_duration = max(elapsed_mono, self._bucket_elapsed_seconds)
            max_allowed = max(1, int(round(elapsed_duration)) + 1)
            if (active_int + idle_int) > max_allowed:
                excess = (active_int + idle_int) - max_allowed
                if idle_int >= excess:
                    idle_int -= excess
                else:
                    active_int = max(0, active_int - (excess - idle_int))
                    idle_int = 0

            active_int = max(0, min(self.bucket_duration_seconds, active_int))
            idle_int = max(0, min(self.bucket_duration_seconds - active_int, idle_int))

            # If clamped to zero total seconds, do not persist empty record
            if active_int == 0 and idle_int == 0:
                self._reset_bucket_accumulators(start_utc=end_utc, start_mono=now_mono)
                return None

            # Calculate consistent bucket_end_utc
            if end_utc is not None:
                bucket_end_utc = end_utc
            elif (active_int + idle_int) >= self.bucket_duration_seconds:
                bucket_end_utc = _add_seconds_to_iso(self._bucket_start_utc, float(active_int + idle_int))
            else:
                bucket_end_utc = current_utc_iso()

            # Assemble and normalize app usages so sum <= active_int
            app_usage_objs = []
            if self.track_apps and active_int > 0:
                raw_apps = []
                for p, d in self._app_durations_float.items():
                    d_int = int(round(d))
                    if d_int > 0:
                        raw_apps.append({"process_name": p, "duration_seconds": d_int})

                # Scale or clamp app usages if total exceeds active_int
                total_app_secs = sum(a["duration_seconds"] for a in raw_apps)
                if total_app_secs > active_int:
                    scale = active_int / total_app_secs
                    allocated = 0
                    for a in raw_apps:
                        scaled_d = int(round(a["duration_seconds"] * scale))
                        a["duration_seconds"] = scaled_d
                        allocated += scaled_d
                    # Ensure exact fit without exceeding active_int
                    if allocated > active_int and raw_apps:
                        raw_apps[0]["duration_seconds"] -= (allocated - active_int)

                # Final strict invariant check: sum <= active_int
                running_sum = 0
                valid_apps = []
                for a in raw_apps:
                    if a["duration_seconds"] <= 0:
                        continue
                    if running_sum + a["duration_seconds"] > active_int:
                        a["duration_seconds"] = max(0, active_int - running_sum)
                    if a["duration_seconds"] > 0:
                        valid_apps.append(a)
                        running_sum += a["duration_seconds"]

                clean_apps = PrivacyGuard.filter_app_usages(valid_apps, track_apps=self.track_apps)
                app_usage_objs = [
                    AppUsage(process_name=a["process_name"], duration_seconds=a["duration_seconds"])
                    for a in clean_apps if a["duration_seconds"] > 0
                ]

            primary_state = self._resolve_bucket_state(active_int, idle_int)

            # Strict employee identity resolution: prioritize live authenticated binding
            emp_id = None
            if self.auth_manager:
                try:
                    emp_id = self.auth_manager.get_authenticated_employee_id()
                except Exception:
                    emp_id = None

            # Fallback only to explicitly configured constructor ID (e.g. for standalone unit tests)
            if emp_id is None:
                emp_id = self.employee_id

            bucket = ActivityBucket(
                device_uuid=self.device_uuid,
                employee_id=emp_id,
                session_id=self._current_session_id,
                bucket_start=self._bucket_start_utc,
                bucket_end=bucket_end_utc,
                active_seconds=active_int,
                idle_seconds=idle_int,
                state=primary_state,
                is_locked=self._is_locked,
                is_sleeping=self._is_sleeping,
                is_paused=self._is_paused,
                app_usages=app_usage_objs,
            )

            # Audit payload against privacy rules
            PrivacyGuard.audit_payload(bucket.to_api_payload())

            # Persist to local SQLite queue
            self.queue.save_bucket(
                bucket,
                employee_id=emp_id,
                lock_timeout=lock_timeout,
                busy_timeout_ms=db_timeout_ms,
            )
            logger.debug(
                f"Saved activity bucket {bucket.bucket_id}: active={bucket.active_seconds}s, "
                f"idle={bucket.idle_seconds}s, apps={len(bucket.app_usages)}"
            )

            # Commit carryover remainders and reset accumulators only after persistence succeeds
            self._active_carryover = next_active_carryover
            self._idle_carryover = next_idle_carryover
            self._reset_bucket_accumulators(start_utc=bucket_end_utc, start_mono=now_mono)
            return bucket
        finally:
            self._lock.release()

    def _reset_bucket_accumulators(
        self, start_utc: Optional[str] = None, start_mono: Optional[float] = None
    ) -> None:
        """Resets in-memory accumulation counters for the next bucket."""
        self._bucket_start_utc = start_utc or current_utc_iso()
        self._bucket_start_mono = start_mono if start_mono is not None else self._clock()
        self._bucket_elapsed_seconds = 0.0
        self._active_seconds_float = 0.0
        self._idle_seconds_float = 0.0

        # Reset sticky flags according to instantaneous current state (never carry over past transitions)
        curr_state = self.state_machine.current_state if hasattr(self, "state_machine") else TrackerState.STOPPED
        self._is_locked = (curr_state == TrackerState.LOCKED)
        self._is_sleeping = (curr_state == TrackerState.SLEEPING)
        self._is_paused = (curr_state == TrackerState.PAUSED)
        self._last_tracking_state = (
            curr_state if curr_state not in (TrackerState.STOPPED, TrackerState.DISCONNECTED) else None
        )
        self._app_durations_float = {}
