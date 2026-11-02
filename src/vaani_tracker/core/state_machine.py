"""Tracking state machine enforcing Vaani business and attendance rules."""

import logging
from typing import Callable, List, Optional

from ..constants import TrackerState, ShiftStatus
from ..models import ShiftInfo
from ..windows.idle import WindowsIdleDetector
from ..windows.session import WindowsSessionNotifier
from ..windows.power import WindowsPowerNotifier

logger = logging.getLogger(__name__)


class TrackingStateMachine:
    """Manages tracking state transitions based on authoritative backend shift status and Windows hardware events."""

    def __init__(
        self,
        idle_detector: WindowsIdleDetector,
        session_notifier: WindowsSessionNotifier,
        power_notifier: WindowsPowerNotifier,
        idle_threshold_seconds: float = 60.0,
    ):
        self.idle_detector = idle_detector
        self.session_notifier = session_notifier
        self.power_notifier = power_notifier
        self.idle_threshold_seconds = idle_threshold_seconds

        self._shift_info: ShiftInfo = ShiftInfo(status=ShiftStatus.NONE)
        self._current_state: TrackerState = TrackerState.STOPPED
        self._is_connected: bool = True
        self._state_listeners: List[Callable[[TrackerState, TrackerState], None]] = []

        # Wire hardware listeners to immediately re-evaluate state
        self.session_notifier.on_lock(self.evaluate_state)
        self.session_notifier.on_unlock(self.evaluate_state)
        self.power_notifier.on_suspend(self.evaluate_state)
        self.power_notifier.on_resume(self.evaluate_state)

    @property
    def current_state(self) -> TrackerState:
        return self._current_state

    @property
    def shift_info(self) -> ShiftInfo:
        return self._shift_info

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def add_state_listener(self, callback: Callable[[TrackerState, TrackerState], None]) -> None:
        """Subscribes a callback to state change notifications (old_state, new_state)."""
        self._state_listeners.append(callback)

    def set_connected(self, connected: bool) -> None:
        """Sets network connectivity status."""
        if self._is_connected != connected:
            self._is_connected = connected
            self.evaluate_state()

    def update_shift_status(self, shift_info: ShiftInfo) -> None:
        """Updates authoritative shift status from backend and triggers re-evaluation."""
        old_shift = self._shift_info
        self._shift_info = shift_info

        if old_shift.session_id != shift_info.session_id or old_shift.status != shift_info.status:
            logger.info(
                f"Shift status updated: session_id={shift_info.session_id}, "
                f"status={shift_info.status.value}, break={shift_info.current_break_name}"
            )

        self.evaluate_state()

    def evaluate_state(self) -> TrackerState:
        """Evaluates current environment inputs and performs state transition."""
        new_state = self._determine_next_state()

        if new_state != self._current_state:
            old_state = self._current_state
            self._current_state = new_state
            logger.info(f"State transition: {old_state.value} -> {new_state.value}")

            for listener in self._state_listeners:
                try:
                    listener(old_state, new_state)
                except Exception as e:
                    logger.error(f"Error in state listener: {e}", exc_info=True)

        return self._current_state

    def _determine_next_state(self) -> TrackerState:
        """Core rule engine determining tracking state."""
        # Rule 1: Tracking starts ONLY when an authenticated employee has an eligible active Vaani shift
        if not self._shift_info.session_id:
            return TrackerState.STOPPED

        # Rule 2: End Shift stops the tracking session
        if self._shift_info.status in (ShiftStatus.COMPLETED, ShiftStatus.NONE):
            return TrackerState.STOPPED

        # Rule 3: Start Break pauses work tracking
        if self._shift_info.status == ShiftStatus.PAUSED:
            return TrackerState.PAUSED

        # If shift is not ACTIVE, do not track
        if self._shift_info.status != ShiftStatus.ACTIVE:
            return TrackerState.STOPPED

        # Rule 4: Windows sleep indicates SLEEPING
        if self.power_notifier.is_sleeping:
            return TrackerState.SLEEPING

        # Rule 5: Windows lock indicates LOCKED
        if self.session_notifier.is_locked:
            return TrackerState.LOCKED

        # Rule 6: 60 seconds without input indicates IDLE; mouse or keyboard input indicates ACTIVE
        idle_secs = self.idle_detector.get_idle_seconds()
        if idle_secs >= self.idle_threshold_seconds:
            return TrackerState.IDLE

        return TrackerState.ACTIVE

