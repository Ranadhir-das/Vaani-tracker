"""Windows system tray icon and menu interface."""

import logging
import threading
from typing import Optional, Callable

try:
    import pystray
    from pystray import MenuItem as item
    PYSTRAY_AVAILABLE = True
except ImportError:
    pystray = None
    item = None
    PYSTRAY_AVAILABLE = False

from ..config import TrackerConfig
from ..constants import TrackerState
from ..core.state_machine import TrackingStateMachine
from ..core.syncer import BackgroundSyncer
from ..storage.queue import ActivityQueue
from .icons import get_status_icon_image
from .summary import show_activity_summary, format_duration

logger = logging.getLogger(__name__)


class VaaniTrayApp:
    """System tray integration providing employee visibility and controls."""

    def __init__(
        self,
        config: TrackerConfig,
        state_machine: TrackingStateMachine,
        syncer: BackgroundSyncer,
        queue: ActivityQueue,
        on_exit: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self.state_machine = state_machine
        self.syncer = syncer
        self.queue = queue
        self.on_exit = on_exit

        self._icon: Optional["pystray.Icon"] = None
        self._thread: Optional[threading.Thread] = None

        # Subscribe to state transitions to update tray appearance
        self.state_machine.add_state_listener(self._on_state_changed)

    def _get_status_text(self) -> str:
        """Returns concise status text for menu and tooltip."""
        state = self.state_machine.current_state
        shift = self.state_machine.shift_info

        if state == TrackerState.STOPPED:
            return "Vaani: Shift Not Started"
        elif state == TrackerState.PAUSED:
            reason = f" ({shift.current_break_name})" if shift.current_break_name else ""
            return f"Vaani: On Break{reason}"
        elif state == TrackerState.IDLE:
            return "Vaani: Idle (Paused Input)"
        elif state == TrackerState.LOCKED:
            return "Vaani: Workstation Locked"
        elif state == TrackerState.SLEEPING:
            return "Vaani: System Sleeping"
        elif state == TrackerState.DISCONNECTED:
            return "Vaani: Offline (Queueing)"
        elif state == TrackerState.ACTIVE:
            dur = format_duration(shift.net_seconds)
            return f"Vaani: Active ({dur})"
        return f"Vaani: {state.value}"

    def _create_menu(self) -> Optional["pystray.Menu"]:
        """Builds the right-click tray context menu."""
        if not PYSTRAY_AVAILABLE:
            return None

        status_text = self._get_status_text()
        emp_name = self.config.registered_employee_name or "Employee"

        return pystray.Menu(
            item(f"👤 {emp_name}", lambda: None, enabled=False),
            item(f"⚡ {status_text}", lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("🔄 Sync Now", self._on_sync_now),
            item("📊 View Activity Summary", self._on_view_summary),
            pystray.Menu.SEPARATOR,
            item("❌ Exit Tracker", self._on_quit),
        )

    def _on_state_changed(self, old_state: TrackerState, new_state: TrackerState) -> None:
        """Updates the tray icon image and tooltip on state transition."""
        if not self._icon:
            return

        try:
            self._icon.icon = get_status_icon_image(new_state)
            self._icon.title = self._get_status_text()
            if hasattr(self._icon, "update_menu"):
                self._icon.menu = self._create_menu()
        except Exception as e:
            logger.debug(f"Failed to update tray icon dynamically: {e}")

    def _on_sync_now(self) -> None:
        """Handles user clicking 'Sync Now' from tray."""
        logger.info("Manual sync triggered from system tray.")
        threading.Thread(target=self.syncer.sync_pending_now, daemon=True).start()

    def _on_view_summary(self) -> None:
        """Handles user clicking 'View Activity Summary'."""
        try:
            show_activity_summary(self.config, self.state_machine, self.queue)
        except Exception as e:
            logger.error(f"Failed to display activity summary: {e}", exc_info=True)

    def _on_quit(self) -> None:
        """Handles user clicking 'Exit Tracker'."""
        logger.info("Exit requested from system tray.")
        self.stop()
        if self.on_exit:
            self.on_exit()

    def run(self) -> None:
        """Starts the tray application (blocking or in background thread)."""
        if not PYSTRAY_AVAILABLE:
            logger.warning("pystray not available; tray icon disabled.")
            return

        initial_state = self.state_machine.current_state
        initial_image = get_status_icon_image(initial_state)

        self._icon = pystray.Icon(
            name="VaaniTracker",
            icon=initial_image,
            title=self._get_status_text(),
            menu=self._create_menu(),
        )

        logger.info("System tray icon started.")
        self._icon.run()

    def run_detached(self) -> None:
        """Runs the tray icon inside a background thread."""
        self._thread = threading.Thread(target=self.run, daemon=True, name="VaaniTrayThread")
        self._thread.start()

    def stop(self) -> None:
        """Stops and removes the system tray icon."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

