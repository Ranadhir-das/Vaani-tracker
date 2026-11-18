"""Windows system tray icon and menu interface."""

import logging
import threading
from typing import Optional, Callable, Any, List, Dict, Tuple

try:
    import pystray
    from pystray import MenuItem as item
    PYSTRAY_AVAILABLE = True
except ImportError:
    pystray = None
    item = None
    PYSTRAY_AVAILABLE = False

from ..config import TrackerConfig
from ..constants import TrackerState, ShiftStatus
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
        on_open_dashboard: Optional[Callable[[], None]] = None,
        auth_manager: Optional[Any] = None,
    ):
        self.config = config
        self.state_machine = state_machine
        self.syncer = syncer
        self.queue = queue
        self.on_exit = on_exit
        self.on_open_dashboard = on_open_dashboard
        self.auth_manager = auth_manager

        self._icon: Optional["pystray.Icon"] = None
        self._thread: Optional[threading.Thread] = None
        self._exit_requested = threading.Event()
        self._stopped = threading.Event()
        self._meta_lock = threading.Lock()
        self._cached_break_reasons: Optional[List[Dict[str, Any]]] = None

        # Subscribe to state transitions to update tray appearance
        self.state_machine.add_state_listener(self._on_state_changed)

    def get_break_reasons(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetches and caches active break reasons from the CRM shift metadata endpoint.

        Never returns hardcoded fallback IDs. Returns an empty list if metadata
        cannot be retrieved.
        """
        with self._meta_lock:
            if self._cached_break_reasons is not None and not refresh:
                return [dict(b) for b in self._cached_break_reasons]

        token = self.auth_manager.get_token() if self.auth_manager else None
        if not token or not self.syncer or not hasattr(self.syncer, "api_client"):
            with self._meta_lock:
                return [dict(b) for b in (self._cached_break_reasons or [])]

        try:
            ok, _, breaks, _ = self.syncer.api_client.fetch_shift_meta(self.config.device_uuid, token)
            if ok and isinstance(breaks, list):
                valid_breaks: List[Dict[str, Any]] = []
                for b in breaks:
                    if isinstance(b, dict) and b.get("id") is not None and b.get("name"):
                        valid_breaks.append({
                            "id": int(b["id"]),
                            "name": str(b["name"]),
                            "code": str(b.get("code") or ""),
                            "requires_note": bool(b.get("requires_note", False)),
                            "warning_threshold_minutes": int(b.get("warning_threshold_minutes") or 0),
                        })
                with self._meta_lock:
                    self._cached_break_reasons = valid_breaks
                    return [dict(b) for b in valid_breaks]
        except Exception as e:
            logger.debug(f"Could not fetch shift metadata for tray menu: {e}")

        with self._meta_lock:
            return [dict(b) for b in (self._cached_break_reasons or [])]

    def get_break_menu_descriptors(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Builds dynamic break menu descriptors from CRM metadata.

        If metadata is unavailable, returns a single safe 'Take Break...' descriptor
        that opens the dashboard rather than submitting hardcoded break IDs.
        """
        breaks = self.get_break_reasons(refresh=refresh)
        if not breaks:
            return [{
                "id": None,
                "name": "Break",
                "requires_note": False,
                "label": "☕ Take Break...",
                "action": "open_dashboard",
            }]

        descriptors: List[Dict[str, Any]] = []
        for brk in breaks:
            b_id = int(brk["id"])
            b_name = str(brk["name"])
            req_note = bool(brk.get("requires_note", False))
            base_label = f"☕ Take {b_name}" if "break" in b_name.lower() else f"☕ Take {b_name} Break"
            label = f"{base_label}..." if req_note else base_label
            descriptors.append({
                "id": b_id,
                "name": b_name,
                "requires_note": req_note,
                "label": label,
                "action": "open_dashboard_note" if req_note else "pause_break",
            })
        return descriptors

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
        if self.auth_manager:
            auth_id = self.auth_manager.get_authenticated_employee_id()
            if auth_id is not None:
                bound = self.auth_manager.get_bound_employee()
                emp_name = bound.get("employee_name") or self.config.registered_employee_name or "Employee"
            else:
                emp_name = "Not Paired"
        else:
            emp_name = self.config.registered_employee_name or "Employee"

        menu_items = [
            item(f"👤 {emp_name}", lambda: None, enabled=False),
            item(f"⚡ {status_text}", lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]

        # Shift quick action items
        shift = self.state_machine.shift_info
        if shift.status == ShiftStatus.ACTIVE:
            for desc in self.get_break_menu_descriptors():
                if desc["id"] is not None:
                    menu_items.append(
                        item(
                            desc["label"],
                            lambda *_, d=dict(desc): self._on_tray_pause_break(
                                d["id"],
                                requires_note=d["requires_note"],
                                break_name=d["name"],
                            ),
                        )
                    )
                else:
                    menu_items.append(item(desc["label"], self._on_open_dashboard_click))
            menu_items.append(item("⏹ End Shift...", self._on_open_dashboard_click))
            menu_items.append(pystray.Menu.SEPARATOR)
        elif shift.status == ShiftStatus.PAUSED:
            menu_items.append(item("▶ Resume Shift", self._on_tray_resume_shift))
            menu_items.append(item("⏹ End Shift...", self._on_open_dashboard_click))
            menu_items.append(pystray.Menu.SEPARATOR)
        elif shift.status in (ShiftStatus.NOT_STARTED, ShiftStatus.NONE):
            menu_items.append(item("▶ Start Shift...", self._on_open_dashboard_click))
            menu_items.append(pystray.Menu.SEPARATOR)

        if self.on_open_dashboard:
            menu_items.append(item("📊 Open Dashboard", self._on_open_dashboard_click, default=True))

        menu_items.extend([
            item("🔄 Sync Now", self._on_sync_now),
            item("🌐 View Web Summary", self._on_view_summary),
            pystray.Menu.SEPARATOR,
            item("❌ Exit Tracker", self._on_quit),
        ])

        return pystray.Menu(*menu_items)

    def _on_open_dashboard_click(self) -> None:
        """Handles user clicking 'Open Dashboard' or double clicking tray icon."""
        if self._exit_requested.is_set() or self._stopped.is_set():
            return
        if self.on_open_dashboard:
            try:
                self.on_open_dashboard()
            except Exception as e:
                logger.error(f"Error opening dashboard from tray: {e}", exc_info=True)

    def _on_state_changed(self, old_state: TrackerState, new_state: TrackerState) -> None:
        """Updates the tray icon image and tooltip on state transition."""
        if self._stopped.is_set() or not self._icon:
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
        if self._exit_requested.is_set() or self._stopped.is_set():
            logger.debug("Manual sync ignored; tracker is shutting down.")
            return
        logger.info("Manual sync triggered from system tray.")
        threading.Thread(target=self.syncer.sync_pending_now, daemon=True, name="ManualSyncWorker").start()

    def _execute_tray_pause_break(
        self,
        break_id: Optional[int],
        requires_note: bool = False,
        break_name: str = "",
    ) -> Tuple[bool, str]:
        """Validates break metadata and pauses shift or opens dashboard when a note is required."""
        if self._exit_requested.is_set() or self._stopped.is_set():
            return False, "Tracker is shutting down."

        if break_id is None:
            logger.info("Break metadata unavailable in tray; opening dashboard for break selection.")
            self._on_open_dashboard_click()
            return False, "Break metadata unavailable; opened dashboard."

        reasons = self.get_break_reasons()
        matched = next((b for b in reasons if int(b.get("id", -1)) == int(break_id)), None)
        if matched is None:
            # Refresh metadata once in case admin updated break reasons
            reasons = self.get_break_reasons(refresh=True)
            matched = next((b for b in reasons if int(b.get("id", -1)) == int(break_id)), None)

        if matched is None:
            logger.warning(f"Tray break action aborted: break_reason_id={break_id} is not in CRM shift metadata.")
            self._on_open_dashboard_click()
            return False, "Selected break reason is not available in CRM metadata."

        if requires_note or bool(matched.get("requires_note", False)):
            logger.info(
                f"Break reason '{matched.get('name') or break_name}' requires a note; opening dashboard for note entry."
            )
            self._on_open_dashboard_click()
            return False, "Break reason requires a note; opened dashboard."

        token = self.auth_manager.get_token() if self.auth_manager else None
        if not token or not self.syncer or not hasattr(self.syncer, "api_client"):
            return False, "Workstation not authenticated."

        try:
            ok, s, extra = self.syncer.api_client.pause_shift(
                self.config.device_uuid,
                token,
                break_reason_id=int(break_id),
            )
            if ok:
                self.state_machine.update_shift_status(s)
                return True, f"On break: {s.current_break_name or matched.get('name') or break_name}"

            err_code = str(extra.get("error") or "")
            msg = str(extra.get("message") or "Could not start break.")
            if err_code in ("NOTE_REQUIRED", "INVALID_BREAK_REASON"):
                self.get_break_reasons(refresh=True)
                self._on_open_dashboard_click()
            logger.warning(f"Could not pause shift from tray ({err_code}): {msg}")
            return False, msg
        except Exception as e:
            logger.error(f"Error pausing shift from tray: {e}")
            return False, "Unexpected error while starting break."

    def _on_tray_pause_break(
        self,
        break_id: Optional[int],
        requires_note: bool = False,
        break_name: str = "",
        run_async: bool = True,
    ) -> Optional[Tuple[bool, str]]:
        """Handles user clicking Take Break from system tray."""
        if self._exit_requested.is_set() or self._stopped.is_set():
            return False, "Tracker is shutting down."
        if not run_async:
            return self._execute_tray_pause_break(break_id, requires_note=requires_note, break_name=break_name)

        threading.Thread(
            target=self._execute_tray_pause_break,
            args=(break_id, requires_note, break_name),
            daemon=True,
            name="TrayPauseShift",
        ).start()
        return None

    def _on_tray_resume_shift(self) -> None:
        """Handles user clicking Resume Shift from system tray."""
        if self._exit_requested.is_set() or self._stopped.is_set():
            return
        token = self.auth_manager.get_token() if self.auth_manager else None
        if not token or not self.syncer or not hasattr(self.syncer, "api_client"):
            return
        def _worker():
            try:
                ok, s, _ = self.syncer.api_client.resume_shift(self.config.device_uuid, token)
                if ok:
                    self.state_machine.update_shift_status(s)
            except Exception as e:
                logger.error(f"Error resuming shift from tray: {e}")
        threading.Thread(target=_worker, daemon=True, name="TrayResumeShift").start()

    def _on_view_summary(self) -> None:
        """Handles user clicking 'View Activity Summary'."""
        if self._exit_requested.is_set() or self._stopped.is_set():
            logger.debug("View summary ignored; tracker is shutting down.")
            return
        try:
            show_activity_summary(self.config, self.state_machine, self.queue, auth_manager=self.auth_manager)
        except Exception as e:
            logger.error(f"Failed to display activity summary: {e}", exc_info=True)

    def _on_quit(self) -> None:
        """Handles user clicking 'Exit Tracker'."""
        if self._exit_requested.is_set():
            logger.debug("Exit already requested from system tray; ignoring duplicate.")
            return
        self._exit_requested.set()
        logger.info("Exit requested from system tray.")
        if self.on_exit:
            try:
                self.on_exit()
            except Exception as e:
                logger.error(f"Error during on_exit callback: {e}", exc_info=True)

    def run(self) -> None:
        """Starts the tray application (blocking or in background thread)."""
        if not PYSTRAY_AVAILABLE:
            logger.warning("pystray not available; tray icon disabled.")
            return

        if self._stopped.is_set() or self._exit_requested.is_set():
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
        try:
            self._icon.run()
        except Exception as e:
            logger.error(f"Error in system tray run loop: {e}", exc_info=True)
        finally:
            self._stopped.set()

    def run_detached(self) -> None:
        """Runs the tray icon inside a background thread."""
        self._thread = threading.Thread(target=self.run, daemon=True, name="VaaniTrayThread")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """Stops and removes the system tray icon."""
        self._stopped.set()
        if self._icon:
            try:
                self._icon.stop()
            except Exception as e:
                logger.debug(f"Error stopping pystray icon: {e}")
            self._icon = None

        terminated = True
        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.error(f"VaaniTrayThread failed to terminate within {timeout}s.")
                terminated = False
            else:
                self._thread = None

        return terminated

