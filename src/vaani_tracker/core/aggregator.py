"""1-minute bucket activity aggregator engine."""

from datetime import datetime, timezone
import logging
import time
from typing import Optional, Dict

from ..constants import TrackerState, DEFAULT_BUCKET_DURATION_SECONDS
from ..models import ActivityBucket, AppUsage, current_utc_iso
from ..privacy import PrivacyGuard
from ..storage.queue import ActivityQueue
from ..windows.apps import WindowsProcessIdentifier
from .state_machine import TrackingStateMachine

logger = logging.getLogger(__name__)


class ActivityAggregator:
    """Aggregates per-second hardware activity into 60-second time buckets."""

    def __init__(
        self,
        state_machine: TrackingStateMachine,
        process_identifier: WindowsProcessIdentifier,
        queue: ActivityQueue,
        device_uuid: str,
        bucket_duration_seconds: int = DEFAULT_BUCKET_DURATION_SECONDS,
        track_apps: bool = True,
    ):
        self.state_machine = state_machine
        self.process_identifier = process_identifier
        self.queue = queue
        self.device_uuid = device_uuid
        self.bucket_duration_seconds = bucket_duration_seconds
        self.track_apps = track_apps

        # Current bucket in-progress accumulation
        self._bucket_start_utc: str = current_utc_iso()
        self._bucket_start_mono: float = time.monotonic()
        self._active_seconds: int = 0
        self._idle_seconds: int = 0
        self._is_locked: bool = False
        self._is_sleeping: bool = False
        self._is_paused: bool = False
        self._app_durations: Dict[str, int] = {}
        self._current_session_id: Optional[int] = None

    def tick(self, elapsed_seconds: float = 1.0) -> Optional[ActivityBucket]:
        """Processes an activity slice of duration `elapsed_seconds`.

        Returns completed ActivityBucket if a 60-second bucket was closed, otherwise None.
        """
        # Re-evaluate state
        state = self.state_machine.evaluate_state()
        shift = self.state_machine.shift_info

        # If shift changed or ended abruptly, close any open bucket for previous session
        if self._current_session_id is not None and self._current_session_id != shift.session_id:
            logger.info("Shift session boundary detected. Closing in-progress bucket.")
            closed = self.close_current_bucket()
            self._current_session_id = shift.session_id
            return closed

        self._current_session_id = shift.session_id

        # If shift is STOPPED, do not accumulate work metrics
        if state == TrackerState.STOPPED:
            self._reset_bucket_accumulators()
            return None

        # Track flags
        if state == TrackerState.LOCKED:
            self._is_locked = True
        elif state == TrackerState.SLEEPING:
            self._is_sleeping = True
        elif state == TrackerState.PAUSED:
            self._is_paused = True

        # Accumulate active or idle seconds
        rounded_secs = int(round(elapsed_seconds))
        if state == TrackerState.ACTIVE:
            self._active_seconds += rounded_secs
            if self.track_apps:
                proc = self.process_identifier.get_foreground_process_name()
                if proc:
                    clean_proc = PrivacyGuard.sanitize_process_name(proc)
                    self._app_durations[clean_proc] = self._app_durations.get(clean_proc, 0) + rounded_secs
        elif state in (TrackerState.IDLE, TrackerState.LOCKED, TrackerState.SLEEPING):
            self._idle_seconds += rounded_secs

        # Check if bucket duration (60s) has been reached
        now_mono = time.monotonic()
        bucket_elapsed = now_mono - self._bucket_start_mono

        if bucket_elapsed >= self.bucket_duration_seconds or (self._active_seconds + self._idle_seconds) >= self.bucket_duration_seconds:
            return self.close_current_bucket()

        return None

    def close_current_bucket(self) -> Optional[ActivityBucket]:
        """Closes the current 60-second window, persists bucket to SQLite queue, and resets."""
        total_tracked = self._active_seconds + self._idle_seconds
        if total_tracked == 0 and not (self._is_locked or self._is_sleeping or self._is_paused):
            # Empty bucket, no tracking needed
            self._reset_bucket_accumulators()
            return None

        bucket_end_utc = current_utc_iso()
        primary_state = self.state_machine.current_state

        # Assemble app usages (filtered for privacy)
        raw_apps = [
            {"process_name": p, "duration_seconds": d}
            for p, d in self._app_durations.items()
        ]
        clean_apps = PrivacyGuard.filter_app_usages(raw_apps, track_apps=self.track_apps)
        app_usage_objs = [
            AppUsage(process_name=a["process_name"], duration_seconds=a["duration_seconds"])
            for a in clean_apps
        ]

        bucket = ActivityBucket(
            device_uuid=self.device_uuid,
            session_id=self._current_session_id,
            bucket_start=self._bucket_start_utc,
            bucket_end=bucket_end_utc,
            active_seconds=min(self._active_seconds, self.bucket_duration_seconds),
            idle_seconds=min(self._idle_seconds, self.bucket_duration_seconds),
            state=primary_state,
            is_locked=self._is_locked,
            is_sleeping=self._is_sleeping,
            is_paused=self._is_paused,
            app_usages=app_usage_objs,
        )

        # Audit payload against privacy rules
        PrivacyGuard.audit_payload(bucket.to_api_payload())

        # Persist to local SQLite queue
        self.queue.save_bucket(bucket)
        logger.debug(
            f"Saved activity bucket {bucket.bucket_id}: active={bucket.active_seconds}s, "
            f"idle={bucket.idle_seconds}s, apps={len(bucket.app_usages)}"
        )

        self._reset_bucket_accumulators()
        return bucket

    def _reset_bucket_accumulators(self) -> None:
        """Resets in-memory accumulation counters for the next bucket."""
        self._bucket_start_utc = current_utc_iso()
        self._bucket_start_mono = time.monotonic()
        self._active_seconds = 0
        self._idle_seconds = 0
        self._is_locked = False
        self._is_sleeping = False
        self._is_paused = False
        self._app_durations = {}

