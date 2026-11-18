"""Modern desktop dashboard interface for Vaani Desktop Tracker."""

from datetime import datetime, timezone
import logging
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
from PIL import Image, ImageTk

from ..config import TrackerConfig
from ..constants import TrackerState, ShiftStatus, SyncStatus, APP_VERSION
from ..core.state_machine import TrackingStateMachine
from ..core.syncer import BackgroundSyncer
from ..storage.queue import ActivityQueue
from ..client.auth import DeviceAuthManager
from .icons import get_asset_path, STATE_COLORS
from .summary import format_duration

logger = logging.getLogger(__name__)

# Color Palette (Dark Slate Theme)
BG_DARK = "#0f172a"          # Slate 900
BG_CARD = "#1e293b"          # Slate 800
BG_CARD_HOVER = "#334155"    # Slate 700
BG_SIDEBAR = "#090d16"       # Navy Dark
TEXT_MAIN = "#f8fafc"        # Slate 50
TEXT_MUTED = "#94a3b8"       # Slate 400
TEXT_DARK = "#64748b"        # Slate 500
BORDER_COLOR = "#334155"     # Slate 700
ACCENT = "#38bdf8"           # Sky 400
ACCENT_HOVER = "#0284c7"     # Sky 600
COLOR_SUCCESS = "#10b981"    # Emerald 500
COLOR_WARNING = "#f59e0b"    # Amber 500
COLOR_DANGER = "#ef4444"     # Red 500
COLOR_PURPLE = "#8b5cf6"     # Purple 500
COLOR_BLUE = "#3b82f6"       # Blue 500


def is_run_on_startup_enabled() -> bool:
    """Checks if VaaniTracker is configured in Windows user startup registry."""
    if os.name != "nt":
        return False
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "VaaniTracker")
            return True
    except Exception:
        return False


def set_run_on_startup(enabled: bool) -> bool:
    """Enables or disables automatic startup in Windows user registry."""
    if os.name != "nt":
        return False
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                target_exe = sys.executable
                if not getattr(sys, "frozen", False):
                    # In dev mode, point to python executable running main
                    target_exe = f'"{sys.executable}" -m vaani_tracker'
                else:
                    target_exe = f'"{target_exe}"'
                winreg.SetValueEx(key, "VaaniTracker", 0, winreg.REG_SZ, target_exe)
            else:
                try:
                    winreg.DeleteValue(key, "VaaniTracker")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        logger.error(f"Failed to update Windows startup registry: {e}")
        return False


class DashboardViewModel:
    """Pure logic view model computing dashboard representations for UI rendering and tests."""

    def __init__(
        self,
        config: TrackerConfig,
        state_machine: TrackingStateMachine,
        syncer: BackgroundSyncer,
        queue: ActivityQueue,
        auth_manager: DeviceAuthManager,
    ):
        self.config = config
        self.state_machine = state_machine
        self.syncer = syncer
        self.queue = queue
        self.auth_manager = auth_manager

    @staticmethod
    def mask_token(token: Optional[str]) -> str:
        """Masks auth tokens to protect credentials in UI and diagnostics."""
        if not token:
            return "None"
        if len(token) <= 8:
            return "***"
        return f"{token[:6]}...***"

    def get_header_data(self) -> Dict[str, Any]:
        """Returns header summary data: employee info, current state, and sync badge."""
        auth_id = self.auth_manager.get_authenticated_employee_id()
        if auth_id:
            emp = self.auth_manager.get_bound_employee()
            emp_name = emp.get("employee_name") or "Employee"
            emp_id = auth_id
            emp_email = emp.get("employee_email") or "—"
            pending_count = self.queue.get_pending_count(employee_id=auth_id)
            is_synced = (pending_count == 0)
            sync_text = "All synced" if is_synced else f"{pending_count} pending sync"
        else:
            emp_name = "Not Paired"
            emp_id = "—"
            emp_email = "—"
            pending_count = 0
            is_synced = True
            sync_text = "Not paired"

        state = self.state_machine.current_state
        state_color_tuple = STATE_COLORS.get(state, (148, 163, 184))
        state_hex = f"#{state_color_tuple[0]:02x}{state_color_tuple[1]:02x}{state_color_tuple[2]:02x}"

        shift = self.state_machine.shift_info
        shift_status = shift.status.value if hasattr(shift.status, "value") else str(shift.status)

        return {
            "employee_name": emp_name,
            "employee_id": emp_id,
            "employee_email": emp_email,
            "device_uuid": self.config.device_uuid,
            "state": state,
            "state_label": state.value,
            "state_color": state_hex,
            "shift_status": shift_status,
            "current_break_name": shift.current_break_name or "",
            "pending_count": pending_count,
            "is_synced": is_synced,
            "sync_status_text": sync_text,
        }

    def get_kpi_cards(self) -> List[Dict[str, Any]]:
        """Computes data for 6 metric KPI cards for the Overview panel."""
        auth_id = self.auth_manager.get_authenticated_employee_id()
        # Calculate local calendar day start boundary converted to UTC
        local_now = datetime.now().astimezone()
        local_today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_iso = local_today_start.astimezone(timezone.utc).isoformat()

        if auth_id:
            stats = self.queue.get_summary_stats(today_start_iso, employee_id=auth_id)
            pending_count = self.queue.get_pending_count(employee_id=auth_id)
            active_secs = stats.get("total_active_seconds", 0)
            idle_secs = stats.get("total_idle_seconds", 0)
        else:
            stats = {}
            pending_count = 0
            active_secs = 0
            idle_secs = 0

        shift = self.state_machine.shift_info
        state = self.state_machine.current_state
        break_secs = shift.break_seconds

        # Format KPI cards
        cards = [
            {
                "title": "Active Work Time",
                "value": format_duration(active_secs),
                "subtext": "Keyboard & mouse active today",
                "color": COLOR_SUCCESS,
                "icon": "⚡",
            },
            {
                "title": "Idle Time",
                "value": format_duration(idle_secs),
                "subtext": "Away from keyboard (>60s)",
                "color": COLOR_WARNING,
                "icon": "⏳",
            },
            {
                "title": "Break Time",
                "value": format_duration(break_secs),
                "subtext": shift.current_break_name or "Official shift breaks",
                "color": COLOR_PURPLE,
                "icon": "☕",
            },
            {
                "title": "Current State",
                "value": state.value,
                "subtext": f"Shift: {shift.status.value}",
                "color": ACCENT,
                "icon": "🎯",
            },
            {
                "title": "Pending Sync",
                "value": str(pending_count),
                "subtext": "Buckets queued locally" if pending_count > 0 else "Up to date with server",
                "color": COLOR_WARNING if pending_count > 0 else COLOR_SUCCESS,
                "icon": "☁",
            },
            {
                "title": "Workstation Status",
                "value": "Locked" if self.state_machine.session_notifier.is_locked else "Unlocked",
                "subtext": "System sleep: Active" if self.state_machine.power_notifier.is_sleeping else "System awake",
                "color": COLOR_BLUE if self.state_machine.session_notifier.is_locked else COLOR_SUCCESS,
                "icon": "🔒",
            },
        ]
        return cards

    def get_timeline_intervals(self, limit: int = 40) -> List[Dict[str, Any]]:
        """Returns recent activity buckets for the Activity Timeline panel."""
        auth_id = self.auth_manager.get_authenticated_employee_id()
        if auth_id:
            buckets = self.queue.get_recent_buckets(limit=limit, employee_id=auth_id)
        else:
            buckets = []
        items = []
        for b in buckets:
            # Parse start time and convert UTC to workstation local timezone
            try:
                dt_start = datetime.fromisoformat(b.bucket_start.replace("Z", "+00:00"))
                if dt_start.tzinfo is None:
                    dt_start = dt_start.replace(tzinfo=timezone.utc)
                start_str = dt_start.astimezone().strftime("%H:%M:%S")
            except Exception:
                start_str = b.bucket_start

            # Parse end time and convert UTC to workstation local timezone
            try:
                dt_end = datetime.fromisoformat(b.bucket_end.replace("Z", "+00:00"))
                if dt_end.tzinfo is None:
                    dt_end = dt_end.replace(tzinfo=timezone.utc)
                end_str = dt_end.astimezone().strftime("%H:%M:%S")
            except Exception:
                end_str = b.bucket_end

            top_app = "—"
            if b.app_usages:
                top_app = max(b.app_usages, key=lambda a: a.duration_seconds).process_name

            items.append({
                "bucket_id": b.bucket_id,
                "time_range": f"{start_str} - {end_str}",
                "state": b.state.value if hasattr(b.state, "value") else str(b.state),
                "active_seconds": b.active_seconds,
                "idle_seconds": b.idle_seconds,
                "active_str": f"{b.active_seconds}s",
                "idle_str": f"{b.idle_seconds}s",
                "top_app": top_app,
                "sync_status": b.sync_status.value if hasattr(b.sync_status, "value") else str(b.sync_status),
            })
        return items

    def get_app_usage(self) -> List[Tuple[str, str, int, int]]:
        """Returns sorted list of (process_name, duration_formatted, percentage, duration_seconds)."""
        auth_id = self.auth_manager.get_authenticated_employee_id()
        if not auth_id:
            return []

        # Calculate local calendar day start boundary converted to UTC
        local_now = datetime.now().astimezone()
        local_today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_iso = local_today_start.astimezone(timezone.utc).isoformat()
        stats = self.queue.get_summary_stats(today_start_iso, employee_id=auth_id)
        total_active = max(1, stats.get("total_active_seconds", 0))

        app_breakdown = stats.get("app_breakdown", {})
        sorted_apps = sorted(app_breakdown.items(), key=lambda x: x[1], reverse=True)

        results = []
        for proc, dur in sorted_apps:
            dur_str = format_duration(dur)
            pct = min(100, int((dur / total_active) * 100))
            results.append((proc, dur_str, pct, dur))
        return results

    def get_device_account(self) -> Dict[str, Any]:
        """Returns complete device, workstation, and employee binding parameters."""
        auth_id = self.auth_manager.get_authenticated_employee_id()
        if auth_id:
            emp = self.auth_manager.get_bound_employee()
            emp_id = auth_id
            emp_name = emp.get("employee_name") or "Employee"
            emp_email = emp.get("employee_email") or "—"
        else:
            emp_id = "—"
            emp_name = "Not Paired"
            emp_email = "—"

        return {
            "machine_name": platform.node(),
            "os_version": f"{platform.system()} {platform.release()} ({platform.version()})",
            "device_uuid": self.config.device_uuid,
            "client_version": APP_VERSION,
            "backend_url": self.config.backend_url,
            "employee_id": emp_id,
            "employee_name": emp_name,
            "employee_email": emp_email,
            "auth_token_masked": self.mask_token(self.auth_manager.get_token()),
            "data_dir": str(self.config.data_dir),
            "db_path": str(self.config.db_path),
            "log_path": str(self.config.log_path),
            "is_authenticated": self.auth_manager.is_authenticated(),
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        """Compiles technical diagnostic information for debugging."""
        db_size_bytes = 0
        if self.config.db_path.exists():
            try:
                db_size_bytes = self.config.db_path.stat().st_size
            except Exception:
                pass

        auth_id = self.auth_manager.get_authenticated_employee_id()
        quarantine = self.queue.get_quarantine_diagnostics()
        diag_counts = self.queue.get_device_diagnostic_counts()
        total_buckets = diag_counts.get("total", 0)

        if auth_id:
            pending_buckets = self.queue.get_pending_count(employee_id=auth_id)
        else:
            pending_buckets = 0

        return {
            "database_size_kb": round(db_size_bytes / 1024, 2),
            "total_buckets": total_buckets,
            "pending_buckets": pending_buckets,
            "synced_buckets": diag_counts.get(SyncStatus.SYNCED.value, 0),
            "orphaned_buckets": quarantine.get("orphaned_count", 0),
            "dead_letter_buckets": quarantine.get("dead_letter_count", 0),
            "is_locked": self.state_machine.session_notifier.is_locked,
            "is_sleeping": self.state_machine.power_notifier.is_sleeping,
            "current_state": self.state_machine.current_state.value,
            "idle_threshold_seconds": self.config.idle_threshold_seconds,
            "sync_interval_seconds": self.config.sync_interval_seconds,
            "track_apps": self.config.track_apps,
            "log_level": self.config.log_level,
            "run_on_startup": is_run_on_startup_enabled(),
        }

    def get_recent_logs(self, max_lines: int = 150) -> str:
        """Reads recent lines from log file, masking any sensitive tokens."""
        if not self.config.log_path.exists():
            return "No log entries recorded yet."

        try:
            with open(self.config.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                recent = lines[-max_lines:]
                # Mask tokens if present in logs
                clean_lines = []
                for line in recent:
                    if "token" in line.lower() and "vdt_" in line:
                        # Mask token sequence
                        parts = line.split("vdt_")
                        masked_line = parts[0] + "vdt_***"
                        clean_lines.append(masked_line)
                    else:
                        clean_lines.append(line)
                return "".join(clean_lines)
        except Exception as e:
            return f"Error reading log file: {e}"

    def get_shift_metadata(self, refresh: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Fetches and caches shift categories and break reasons from backend."""
        if (
            not refresh
            and hasattr(self, "_cached_categories")
            and hasattr(self, "_cached_break_reasons")
            and (self._cached_categories or self._cached_break_reasons)
        ):
            return list(self._cached_categories), list(self._cached_break_reasons)

        token = self.auth_manager.get_token() if self.auth_manager else None
        if not token or not self.syncer or not hasattr(self.syncer, "api_client"):
            return (
                list(getattr(self, "_cached_categories", [])),
                list(getattr(self, "_cached_break_reasons", [])),
            )
        try:
            res = self.syncer.api_client.fetch_shift_meta(self.config.device_uuid, token)
            if isinstance(res, tuple) and len(res) == 4:
                ok, cats, breaks, _ = res
                if ok and isinstance(cats, list) and isinstance(breaks, list):
                    self._cached_categories = list(cats)
                    self._cached_break_reasons = list(breaks)
                    return list(cats), list(breaks)
        except Exception:
            pass
        return (
            list(getattr(self, "_cached_categories", [])),
            list(getattr(self, "_cached_break_reasons", [])),
        )

    def get_break_reason_by_id(
        self, break_reason_id: int, refresh: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Looks up a break reason dictionary from shift metadata by ID."""
        _, breaks = self.get_shift_metadata(refresh=refresh)
        for b in breaks:
            if isinstance(b, dict) and b.get("id") is not None:
                try:
                    if int(b["id"]) == int(break_reason_id):
                        return dict(b)
                except (ValueError, TypeError):
                    continue
        return None

    @staticmethod
    def validate_break_note(note: Optional[str], requires_note: bool = False) -> Tuple[bool, str, str]:
        """Validates a break note against CRM requirements.

        Returns (is_valid, cleaned_note, error_message).
        Never logs the note content.
        """
        raw = "" if note is None else str(note)
        cleaned = raw.strip()
        if requires_note and not cleaned:
            return False, "", "Please enter a short note for this break reason."
        if len(cleaned) > 255:
            return False, "", "Break note cannot exceed 255 characters."
        return True, cleaned, ""

    def start_shift(self, category_id: Optional[int] = None, note: str = "") -> Tuple[bool, str]:
        token = self.auth_manager.get_token()
        if not token:
            return False, "Device is not authenticated."
        if not self.syncer or not hasattr(self.syncer, "api_client"):
            return False, "API client is not available."
        ok, shift, extra = self.syncer.api_client.start_shift(
            device_uuid=self.config.device_uuid,
            token=token,
            category_id=category_id,
            note=note,
        )
        if ok:
            self.state_machine.update_shift_status(shift)
            return True, "Shift started successfully."
        return False, extra.get("message") or extra.get("error") or "Failed to start shift."

    def pause_shift(
        self,
        break_reason_id: int,
        note: str = "",
        enforce_metadata: bool = False,
    ) -> Tuple[bool, str]:
        token = self.auth_manager.get_token()
        if not token:
            return False, "Device is not authenticated."
        if not self.syncer or not hasattr(self.syncer, "api_client"):
            return False, "API client is not available."

        requires_note = False
        cached_breaks = getattr(self, "_cached_break_reasons", None)
        if enforce_metadata or cached_breaks is not None:
            brk_meta = self.get_break_reason_by_id(break_reason_id)
            if brk_meta is not None:
                requires_note = bool(brk_meta.get("requires_note", False))
            elif enforce_metadata:
                # Refresh once before rejecting unknown break_reason_id
                brk_meta = self.get_break_reason_by_id(break_reason_id, refresh=True)
                if brk_meta is None:
                    return False, "Selected break reason is not available in server metadata."
                requires_note = bool(brk_meta.get("requires_note", False))

        valid_note, cleaned_note, err_msg = self.validate_break_note(note, requires_note=requires_note)
        if not valid_note:
            return False, err_msg

        ok, shift, extra = self.syncer.api_client.pause_shift(
            device_uuid=self.config.device_uuid,
            token=token,
            break_reason_id=break_reason_id,
            note=cleaned_note,
        )
        if ok:
            self.state_machine.update_shift_status(shift)
            return True, f"Shift paused on break: {shift.current_break_name or 'Break'}."

        if extra.get("error") in ("NOTE_REQUIRED", "INVALID_BREAK_REASON"):
            self.get_shift_metadata(refresh=True)
        return False, extra.get("message") or extra.get("error") or "Failed to pause shift."

    def resume_shift(self, category_id: Optional[int] = None, note: str = "") -> Tuple[bool, str]:
        token = self.auth_manager.get_token()
        if not token:
            return False, "Device is not authenticated."
        if not self.syncer or not hasattr(self.syncer, "api_client"):
            return False, "API client is not available."
        ok, shift, extra = self.syncer.api_client.resume_shift(
            device_uuid=self.config.device_uuid,
            token=token,
            category_id=category_id,
            note=note,
        )
        if ok:
            self.state_machine.update_shift_status(shift)
            return True, "Shift resumed successfully."
        return False, extra.get("message") or extra.get("error") or "Failed to resume shift."

    def end_shift(self, note: str = "", force_early: bool = False) -> Tuple[bool, str]:
        token = self.auth_manager.get_token()
        if not token:
            return False, "Device is not authenticated."
        if not self.syncer or not hasattr(self.syncer, "api_client"):
            return False, "API client is not available."
        ok, shift, extra = self.syncer.api_client.end_shift(
            device_uuid=self.config.device_uuid,
            token=token,
            note=note,
            force_early=force_early,
        )
        if ok:
            self.state_machine.update_shift_status(shift)
            if hasattr(self.syncer, "trigger_immediate_sync"):
                try:
                    self.syncer.trigger_immediate_sync()
                except Exception:
                    pass
            return True, "Shift ended successfully."
        return False, extra.get("message") or extra.get("error") or "Failed to end shift."


class VaaniDashboard:
    """Tkinter/ttk desktop GUI dashboard application."""

    def __init__(
        self,
        config: TrackerConfig,
        state_machine: TrackingStateMachine,
        syncer: BackgroundSyncer,
        queue: ActivityQueue,
        auth_manager: DeviceAuthManager,
        on_exit: Optional[Callable[[], None]] = None,
        root: Optional[tk.Tk] = None,
        aggregator: Optional[Any] = None,
    ):
        self.config = config
        self.state_machine = state_machine
        self.syncer = syncer
        self.queue = queue
        self.auth_manager = auth_manager
        self.on_exit = on_exit
        self.aggregator = aggregator

        self.vm = DashboardViewModel(
            config=config,
            state_machine=state_machine,
            syncer=syncer,
            queue=queue,
            auth_manager=auth_manager,
        )

        self._is_closed = False
        self._is_closing = False
        self._tick_id: Optional[str] = None
        self._current_tab = "overview"

        # Create or use existing Tk root
        if root is not None:
            self.root = root
            self._owns_root = False
        else:
            self.root = tk.Tk()
            self._owns_root = True

        self._setup_window()
        self._setup_styles()
        self._create_layout()

        # Start periodic UI refresh timer (2.0s)
        try:
            self._tick_id = self.root.after(2000, self._tick_ui)
        except Exception:
            self._tick_id = None

    def _setup_window(self) -> None:
        """Configures root window geometry, title, icons, and close interception."""
        self.root.title("Vaani Desktop Tracker")
        self.root.geometry("1020x680")
        self.root.minsize(880, 580)
        self.root.configure(bg=BG_DARK)

        # Set taskbar/window icon
        ico_path = get_asset_path("vaani.ico")
        if ico_path.exists():
            try:
                self.root.iconbitmap(str(ico_path))
            except Exception:
                pass

        # Handle window close [X] -> minimize to tray
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    def _setup_styles(self) -> None:
        """Sets up ttk widget styling."""
        self.style = ttk.Style(master=self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # Treeview styling
        self.style.configure(
            "Treeview",
            background=BG_CARD,
            foreground=TEXT_MAIN,
            fieldbackground=BG_CARD,
            rowheight=28,
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        self.style.configure(
            "Treeview.Heading",
            background=BG_DARK,
            foreground=TEXT_MUTED,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padding=(6, 6),
        )
        self.style.map(
            "Treeview",
            background=[("selected", ACCENT_HOVER)],
            foreground=[("selected", TEXT_MAIN)],
        )

    def _create_layout(self) -> None:
        """Constructs header, sidebar, and dynamic content pane."""
        # Top Header Bar
        self.header_frame = tk.Frame(self.root, bg=BG_DARK, height=64)
        self.header_frame.pack(side="top", fill="x", padx=16, pady=12)

        self._build_header()

        # Horizontal Separator
        sep = tk.Frame(self.root, bg=BORDER_COLOR, height=1)
        sep.pack(side="top", fill="x")

        # Main Body Frame (Sidebar + Content Pane)
        self.body_frame = tk.Frame(self.root, bg=BG_DARK)
        self.body_frame.pack(side="top", fill="both", expand=True)

        # Left Sidebar (Nav)
        self.sidebar_frame = tk.Frame(self.body_frame, bg=BG_SIDEBAR, width=220)
        self.sidebar_frame.pack(side="left", fill="y")
        self.sidebar_frame.pack_propagate(False)

        self._build_sidebar()

        # Vertical Divider
        v_sep = tk.Frame(self.body_frame, bg=BORDER_COLOR, width=1)
        v_sep.pack(side="left", fill="y")

        # Right Content View Area
        self.content_container = tk.Frame(self.body_frame, bg=BG_DARK)
        self.content_container.pack(side="right", fill="both", expand=True, padx=20, pady=16)

        # Show initial tab
        self.show_view("overview")

    def _build_header(self) -> None:
        """Populates header with branding, live status badge, employee pill, and sync action."""
        # Left: Brand Logo & Title
        brand_frame = tk.Frame(self.header_frame, bg=BG_DARK)
        brand_frame.pack(side="left")

        # Load small icon image if available
        self._header_logo_img = None
        icon_path = get_asset_path("vaani-icon.png")
        if icon_path.exists():
            try:
                pil_img = Image.open(icon_path).resize((36, 36), Image.Resampling.LANCZOS)
                self._header_logo_img = ImageTk.PhotoImage(pil_img, master=self.root)
                lbl_icon = tk.Label(brand_frame, image=self._header_logo_img, bg=BG_DARK)
                lbl_icon.pack(side="left", padx=(0, 10))
            except Exception:
                pass

        title_frame = tk.Frame(brand_frame, bg=BG_DARK)
        title_frame.pack(side="left")

        lbl_title = tk.Label(
            title_frame,
            text="VAANI TRACKER",
            font=("Segoe UI", 13, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        lbl_title.pack(anchor="w")

        lbl_version = tk.Label(
            title_frame,
            text=f"v{APP_VERSION} · Enterprise Desktop Client",
            font=("Segoe UI", 8),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        )
        lbl_version.pack(anchor="w")

        # Right: Live State Pill, Employee Pill, and Sync Action
        actions_frame = tk.Frame(self.header_frame, bg=BG_DARK)
        actions_frame.pack(side="right")

        # Sync Status Badge
        self.lbl_sync_badge = tk.Label(
            actions_frame,
            text="☁ All synced",
            font=("Segoe UI", 9),
            fg=COLOR_SUCCESS,
            bg=BG_CARD,
            padx=10,
            pady=4,
        )
        self.lbl_sync_badge.pack(side="left", padx=6)

        # "Sync Now" button
        self.btn_sync_now = tk.Button(
            actions_frame,
            text="🔄 Sync Now",
            font=("Segoe UI", 9, "bold"),
            bg=BG_CARD,
            fg=ACCENT,
            activebackground=BG_CARD_HOVER,
            activeforeground=TEXT_MAIN,
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._on_sync_now_click,
        )
        self.btn_sync_now.pack(side="left", padx=6)

        # State Pill
        self.state_pill_frame = tk.Frame(actions_frame, bg=BG_CARD, padx=10, pady=4)
        self.state_pill_frame.pack(side="left", padx=6)

        self.state_canvas = tk.Canvas(self.state_pill_frame, width=12, height=12, bg=BG_CARD, highlightthickness=0)
        self.state_canvas.pack(side="left", padx=(0, 6))
        self.state_dot = self.state_canvas.create_oval(1, 1, 11, 11, fill=COLOR_SUCCESS, outline="")

        self.lbl_state_text = tk.Label(
            self.state_pill_frame,
            text="ACTIVE",
            font=("Segoe UI", 9, "bold"),
            fg=TEXT_MAIN,
            bg=BG_CARD,
        )
        self.lbl_state_text.pack(side="left")

        # Employee Pill
        emp_header = self.vm.get_header_data()
        self.lbl_emp_pill = tk.Label(
            actions_frame,
            text=f"👤 {emp_header['employee_name']} (#{emp_header['employee_id']})",
            font=("Segoe UI", 9),
            fg=TEXT_MAIN,
            bg=BG_CARD,
            padx=10,
            pady=4,
        )
        self.lbl_emp_pill.pack(side="left", padx=6)

    def _build_sidebar(self) -> None:
        """Constructs left navigation bar with 8 tabs."""
        lbl_nav = tk.Label(
            self.sidebar_frame,
            text="NAVIGATION",
            font=("Segoe UI", 8, "bold"),
            fg=TEXT_DARK,
            bg=BG_SIDEBAR,
            padx=16,
            pady=12,
        )
        lbl_nav.pack(anchor="w")

        nav_items = [
            ("overview", "📊 Overview"),
            ("timeline", "⏱️ Activity Timeline"),
            ("apps", "💻 App Usage"),
            ("device", "🖥️ Device & Account"),
            ("settings", "⚙️ Settings"),
            ("privacy", "🛡️ Privacy & Rights"),
            ("diagnostics", "🔍 Diagnostics & Logs"),
            ("about", "ℹ️ About Vaani"),
        ]

        self.nav_buttons: Dict[str, tk.Button] = {}
        for tab_id, label in nav_items:
            btn = tk.Button(
                self.sidebar_frame,
                text=label,
                font=("Segoe UI", 9),
                fg=TEXT_MUTED,
                bg=BG_SIDEBAR,
                activebackground=BG_CARD,
                activeforeground=TEXT_MAIN,
                relief="flat",
                bd=0,
                anchor="w",
                padx=16,
                pady=10,
                cursor="hand2",
                command=lambda t=tab_id: self.show_view(t),
            )
            btn.pack(fill="x")
            self.nav_buttons[tab_id] = btn

    def show_view(self, tab_id: str) -> None:
        """Switches active navigation tab and displays the selected view."""
        self._current_tab = tab_id

        # Update button visual active state
        for t, btn in self.nav_buttons.items():
            if t == tab_id:
                btn.configure(bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 9, "bold"))
            else:
                btn.configure(bg=BG_SIDEBAR, fg=TEXT_MUTED, font=("Segoe UI", 9))

        # Clear content container
        for child in self.content_container.winfo_children():
            child.destroy()

        # Render selected view
        if tab_id == "overview":
            self._render_overview_view()
        elif tab_id == "timeline":
            self._render_timeline_view()
        elif tab_id == "apps":
            self._render_apps_view()
        elif tab_id == "device":
            self._render_device_view()
        elif tab_id == "settings":
            self._render_settings_view()
        elif tab_id == "privacy":
            self._render_privacy_view()
        elif tab_id == "diagnostics":
            self._render_diagnostics_view()
        elif tab_id == "about":
            self._render_about_view()

    # --------------------------------------------------------------------------
    # VIEW: OVERVIEW
    # --------------------------------------------------------------------------
    def _render_overview_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Today's Shift Overview",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 14))

        # Shift & Attendance Controls Card
        self._render_shift_controls_card()

        # KPI Cards (3 cols x 2 rows)
        cards_grid = tk.Frame(self.content_container, bg=BG_DARK)
        cards_grid.pack(fill="x", pady=(0, 16))

        cards = self.vm.get_kpi_cards()
        for idx, card in enumerate(cards):
            col = idx % 3
            row = idx // 3

            card_box = tk.Frame(cards_grid, bg=BG_CARD, padx=16, pady=14, relief="flat", highlightbackground=BORDER_COLOR, highlightthickness=1)
            card_box.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
            cards_grid.grid_columnconfigure(col, weight=1)

            # Icon & Title
            top_f = tk.Frame(card_box, bg=BG_CARD)
            top_f.pack(fill="x")

            lbl_icon = tk.Label(top_f, text=card["icon"], font=("Segoe UI", 12), fg=card["color"], bg=BG_CARD)
            lbl_icon.pack(side="left")

            lbl_t = tk.Label(top_f, text=card["title"], font=("Segoe UI", 9, "bold"), fg=TEXT_MUTED, bg=BG_CARD)
            lbl_t.pack(side="left", padx=6)

            # Big Value
            lbl_val = tk.Label(card_box, text=card["value"], font=("Segoe UI", 18, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
            lbl_val.pack(anchor="w", pady=(6, 2))

            # Subtext
            lbl_sub = tk.Label(card_box, text=card["subtext"], font=("Segoe UI", 8), fg=TEXT_DARK, bg=BG_CARD)
            lbl_sub.pack(anchor="w")

        # Employee Privacy Assurance Banner
        banner = tk.Frame(self.content_container, bg=BG_CARD, padx=16, pady=12, highlightbackground=BORDER_COLOR, highlightthickness=1)
        banner.pack(fill="x", pady=(8, 16))

        b_icon = tk.Label(banner, text="🔒", font=("Segoe UI", 14), bg=BG_CARD)
        b_icon.pack(side="left", padx=(0, 10))

        b_text = tk.Label(
            banner,
            text="Employee Privacy Protected: Vaani Tracker logs only active work duration and foreground process names.\n"
                 "No keystrokes, screenshots, clipboard contents, or browser URLs are ever recorded.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_CARD,
            justify="left",
        )
        b_text.pack(side="left")

        # Quick Snapshot of Recent Intervals
        snap_lbl = tk.Label(self.content_container, text="Recent Activity Snapshots (Last 5 Minutes)", font=("Segoe UI", 11, "bold"), fg=TEXT_MAIN, bg=BG_DARK)
        snap_lbl.pack(anchor="w", pady=(8, 6))

        recent_intervals = self.vm.get_timeline_intervals(limit=5)
        if not recent_intervals:
            lbl_empty = tk.Label(self.content_container, text="No activity buckets recorded yet today.", font=("Segoe UI", 9), fg=TEXT_DARK, bg=BG_DARK)
            lbl_empty.pack(anchor="w", pady=4)
        else:
            for item in recent_intervals:
                row_f = tk.Frame(self.content_container, bg=BG_CARD, padx=12, pady=6)
                row_f.pack(fill="x", pady=2)

                lbl_time = tk.Label(row_f, text=item["time_range"], font=("Segoe UI", 9, "bold"), fg=ACCENT, bg=BG_CARD, width=18, anchor="w")
                lbl_time.pack(side="left")

                badge_col = COLOR_SUCCESS if item["state"] == "ACTIVE" else COLOR_WARNING
                lbl_st = tk.Label(row_f, text=item["state"], font=("Segoe UI", 8, "bold"), fg=badge_col, bg=BG_DARK, padx=6, pady=1)
                lbl_st.pack(side="left", padx=8)

                lbl_dur = tk.Label(row_f, text=f"Active: {item['active_str']} · Idle: {item['idle_str']}", font=("Segoe UI", 9), fg=TEXT_MUTED, bg=BG_CARD)
                lbl_dur.pack(side="left", padx=8)

                lbl_app = tk.Label(row_f, text=f"App: {item['top_app']}", font=("Segoe UI", 9), fg=TEXT_MAIN, bg=BG_CARD)
                lbl_app.pack(side="right")

    def _safe_get_shift_metadata(self, refresh: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Safely retrieves shift metadata even when view-model is mocked or offline."""
        try:
            try:
                res = self.vm.get_shift_metadata(refresh=refresh)
            except TypeError:
                res = self.vm.get_shift_metadata()
            if isinstance(res, tuple) and len(res) == 2:
                cats, breaks = res
                if isinstance(cats, list) and isinstance(breaks, list):
                    return cats, breaks
        except Exception:
            pass
        return [], []

    def _render_shift_controls_card(self) -> None:
        """Renders interactive Shift Controls according to authoritative CRM shift status."""
        shift = self.state_machine.shift_info
        status = shift.status

        card = tk.Frame(
            self.content_container,
            bg=BG_CARD,
            padx=16,
            pady=14,
            relief="flat",
            highlightbackground=BORDER_COLOR,
            highlightthickness=1,
        )
        card.pack(fill="x", pady=(0, 16))

        # Top row: Section title & status pill
        top_row = tk.Frame(card, bg=BG_CARD)
        top_row.pack(fill="x", pady=(0, 10))

        lbl_section = tk.Label(
            top_row,
            text="⏱ Shift & Attendance Controls",
            font=("Segoe UI", 11, "bold"),
            fg=TEXT_MAIN,
            bg=BG_CARD,
        )
        lbl_section.pack(side="left")

        # Status badge color and text
        if status == ShiftStatus.ACTIVE:
            status_text = "● ACTIVE WORK SESSION"
            status_bg = "#064E3B"
            status_fg = "#34D399"
        elif status == ShiftStatus.PAUSED:
            brk = shift.current_break_name or "Break"
            status_text = f"⏸ ON BREAK ({brk.upper()})"
            status_bg = "#4C1D95"
            status_fg = "#C084FC"
        elif status == ShiftStatus.COMPLETED:
            status_text = "✓ SHIFT COMPLETED TODAY"
            status_bg = "#1E3A8A"
            status_fg = "#93C5FD"
        else:
            status_text = "○ SHIFT NOT STARTED"
            status_bg = "#374151"
            status_fg = "#9CA3AF"

        lbl_status_pill = tk.Label(
            top_row,
            text=status_text,
            font=("Segoe UI", 9, "bold"),
            fg=status_fg,
            bg=status_bg,
            padx=10,
            pady=3,
        )
        lbl_status_pill.pack(side="right")

        # Controls & Action row
        action_row = tk.Frame(card, bg=BG_CARD)
        action_row.pack(fill="x", pady=(4, 0))

        if status == ShiftStatus.ACTIVE:
            cat_display = shift.current_category_name or "General Work"
            lbl_desc = tk.Label(
                action_row,
                text=f"Current Category: {cat_display}  ·  Net Work: {format_duration(shift.net_seconds)}",
                font=("Segoe UI", 9),
                fg=TEXT_MUTED,
                bg=BG_CARD,
            )
            lbl_desc.pack(side="left")

            btn_frame = tk.Frame(action_row, bg=BG_CARD)
            btn_frame.pack(side="right")

            # Break reason selector (dynamic from CRM shift metadata; never uses hardcoded fallback IDs)
            cats, breaks = self._safe_get_shift_metadata()
            valid_breaks = [
                b for b in breaks
                if isinstance(b, dict) and b.get("name") and b.get("id") is not None
            ]
            self._breaks_meta_by_name: Dict[str, Dict[str, Any]] = {
                str(b["name"]): dict(b) for b in valid_breaks
            }
            self._breaks_map: Dict[str, int] = {
                str(b["name"]): int(b["id"]) for b in valid_breaks
            }
            if valid_breaks:
                break_names = [str(b["name"]) for b in valid_breaks]
                default_brk = break_names[0]
            else:
                break_names = ["Unavailable (Sync Metadata)"]
                default_brk = break_names[0]
            self._var_selected_break = tk.StringVar(master=self.root, value=default_brk)

            self.combo_break = ttk.Combobox(
                btn_frame,
                textvariable=self._var_selected_break,
                values=break_names,
                state="readonly",
                width=16,
            )
            self.combo_break.pack(side="left", padx=(0, 8))

            self.btn_take_break = tk.Button(
                btn_frame,
                text="☕ Take Break",
                font=("Segoe UI", 9, "bold"),
                bg="#6D28D9",
                fg="#FFFFFF",
                activebackground="#7C3AED",
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=12,
                pady=5,
                cursor="hand2",
                command=self._on_take_break_click,
            )
            self.btn_take_break.pack(side="left", padx=(0, 8))

            self.btn_end_shift = tk.Button(
                btn_frame,
                text="⏹ End Shift",
                font=("Segoe UI", 9, "bold"),
                bg="#DC2626",
                fg="#FFFFFF",
                activebackground="#EF4444",
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=12,
                pady=5,
                cursor="hand2",
                command=self._on_end_shift_click,
            )
            self.btn_end_shift.pack(side="left")

        elif status == ShiftStatus.PAUSED:
            brk_display = shift.current_break_name or "Break"
            lbl_desc = tk.Label(
                action_row,
                text=f"Shift is paused on {brk_display}.  ·  Elapsed Break: {format_duration(shift.break_seconds)}",
                font=("Segoe UI", 9),
                fg=TEXT_MUTED,
                bg=BG_CARD,
            )
            lbl_desc.pack(side="left")

            btn_frame = tk.Frame(action_row, bg=BG_CARD)
            btn_frame.pack(side="right")

            self.btn_resume_shift = tk.Button(
                btn_frame,
                text="▶ Resume Shift",
                font=("Segoe UI", 9, "bold"),
                bg="#059669",
                fg="#FFFFFF",
                activebackground="#10B981",
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=14,
                pady=5,
                cursor="hand2",
                command=self._on_resume_shift_click,
            )
            self.btn_resume_shift.pack(side="left", padx=(0, 8))

            self.btn_end_shift = tk.Button(
                btn_frame,
                text="⏹ End Shift",
                font=("Segoe UI", 9, "bold"),
                bg="#DC2626",
                fg="#FFFFFF",
                activebackground="#EF4444",
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=12,
                pady=5,
                cursor="hand2",
                command=self._on_end_shift_click,
            )
            self.btn_end_shift.pack(side="left")

        elif status == ShiftStatus.COMPLETED:
            lbl_desc = tk.Label(
                action_row,
                text=f"Completed today's shift.  ·  Total Net Work: {format_duration(shift.net_seconds)}  ·  Breaks: {format_duration(shift.break_seconds)}",
                font=("Segoe UI", 9),
                fg=TEXT_MUTED,
                bg=BG_CARD,
            )
            lbl_desc.pack(side="left")

            btn_frame = tk.Frame(action_row, bg=BG_CARD)
            btn_frame.pack(side="right")

            self.btn_start_new = tk.Button(
                btn_frame,
                text="▶ Start New Session",
                font=("Segoe UI", 9, "bold"),
                bg=ACCENT,
                fg="#FFFFFF",
                activebackground=ACCENT_HOVER,
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=14,
                pady=5,
                cursor="hand2",
                command=self._on_start_shift_click,
            )
            self.btn_start_new.pack(side="left")

        else:
            # Shift NOT started / None
            lbl_desc = tk.Label(
                action_row,
                text="Select work category and click Start Shift to begin attendance tracking:",
                font=("Segoe UI", 9),
                fg=TEXT_MUTED,
                bg=BG_CARD,
            )
            lbl_desc.pack(side="left")

            btn_frame = tk.Frame(action_row, bg=BG_CARD)
            btn_frame.pack(side="right")

            cats, breaks = self._safe_get_shift_metadata()
            cat_names = [c["name"] for c in cats] if cats else ["Calling / Inbound", "Back Office / Ops"]
            self._var_selected_cat = tk.StringVar(master=self.root, value=cat_names[0] if cat_names else "Calling / Inbound")
            self._cats_map = {c["name"]: c["id"] for c in cats} if cats else {"Calling / Inbound": 1, "Back Office / Ops": 2}

            combo_cat = ttk.Combobox(
                btn_frame,
                textvariable=self._var_selected_cat,
                values=cat_names,
                state="readonly",
                width=18,
            )
            combo_cat.pack(side="left", padx=(0, 10))

            self.btn_start_shift = tk.Button(
                btn_frame,
                text="▶ Start Shift",
                font=("Segoe UI", 9, "bold"),
                bg=ACCENT,
                fg="#FFFFFF",
                activebackground=ACCENT_HOVER,
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=16,
                pady=5,
                cursor="hand2",
                command=self._on_start_shift_click,
            )
            self.btn_start_shift.pack(side="left")

    def _on_start_shift_click(self) -> None:
        cat_id = None
        if hasattr(self, "_var_selected_cat") and hasattr(self, "_cats_map"):
            cat_id = self._cats_map.get(self._var_selected_cat.get())

        def _worker():
            ok, msg = self.vm.start_shift(category_id=cat_id)
            def _ui_done():
                if ok:
                    self._update_header_data()
                    self.show_view("overview")
                else:
                    messagebox.showerror("Shift Start Failed", msg, parent=self.root)
            self.root.after(0, _ui_done)

        threading.Thread(target=_worker, daemon=True, name="ShiftStartWorker").start()

    def _prompt_break_note_dialog(self, break_name: str) -> Optional[str]:
        """Opens a modal dialog prompting for a required break note (max 255 chars).

        Returns the validated note string, or None if the employee cancels.
        Never logs the note content.
        """
        result: Dict[str, Optional[str]] = {"note": None}

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Break Note Required — {break_name}")
        dlg.geometry("440x230")
        dlg.resizable(False, False)
        dlg.configure(bg=BG_CARD)
        dlg.transient(self.root)
        dlg.grab_set()

        container = tk.Frame(dlg, bg=BG_CARD, padx=20, pady=16)
        container.pack(fill="both", expand=True)

        lbl_title = tk.Label(
            container,
            text=f"Note Required for '{break_name}'",
            font=("Segoe UI", 11, "bold"),
            fg=TEXT_MAIN,
            bg=BG_CARD,
        )
        lbl_title.pack(anchor="w", pady=(0, 6))

        lbl_hint = tk.Label(
            container,
            text="Please enter a short note for this break reason (1–255 characters):",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_CARD,
        )
        lbl_hint.pack(anchor="w", pady=(0, 8))

        note_var = tk.StringVar(master=dlg, value="")
        entry = tk.Entry(
            container,
            textvariable=note_var,
            font=("Segoe UI", 10),
            bg=BG_DARK,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat",
            highlightbackground=BORDER_COLOR,
            highlightthickness=1,
        )
        entry.pack(fill="x", ipady=5, pady=(0, 6))
        entry.focus_set()

        lbl_err = tk.Label(
            container,
            text="",
            font=("Segoe UI", 8, "bold"),
            fg=COLOR_DANGER,
            bg=BG_CARD,
        )
        lbl_err.pack(anchor="w", pady=(0, 8))

        btn_row = tk.Frame(container, bg=BG_CARD)
        btn_row.pack(fill="x", side="bottom")

        def _on_cancel(*_args):
            result["note"] = None
            try:
                dlg.destroy()
            except Exception:
                pass

        def _on_submit(*_args):
            valid, cleaned, err_msg = DashboardViewModel.validate_break_note(
                note_var.get(),
                requires_note=True,
            )
            if not valid:
                lbl_err.configure(text=err_msg)
                return
            result["note"] = cleaned
            try:
                dlg.destroy()
            except Exception:
                pass

        btn_cancel = tk.Button(
            btn_row,
            text="Cancel",
            font=("Segoe UI", 9),
            bg=BG_DARK,
            fg=TEXT_MUTED,
            relief="flat",
            bd=0,
            padx=14,
            pady=5,
            cursor="hand2",
            command=_on_cancel,
        )
        btn_cancel.pack(side="right", padx=(8, 0))

        btn_ok = tk.Button(
            btn_row,
            text="☕ Start Break",
            font=("Segoe UI", 9, "bold"),
            bg="#6D28D9",
            fg="#FFFFFF",
            activebackground="#7C3AED",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=14,
            pady=5,
            cursor="hand2",
            command=_on_submit,
        )
        btn_ok.pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", _on_cancel)
        dlg.bind("<Return>", _on_submit)
        dlg.bind("<Escape>", _on_cancel)

        self.root.wait_window(dlg)
        return result["note"]

    def _on_take_break_click(self) -> None:
        sel_var = getattr(self, "_var_selected_break", None)
        sel_name = sel_var.get() if sel_var else ""
        breaks_map: Dict[str, int] = getattr(self, "_breaks_map", {}) or {}
        breaks_meta: Dict[str, Dict[str, Any]] = getattr(self, "_breaks_meta_by_name", {}) or {}

        if not breaks_map or sel_name not in breaks_map:
            # Attempt a live metadata refresh before failing; never submit a guessed break ID
            _, fresh_breaks = self._safe_get_shift_metadata(refresh=True)
            valid_fresh = [
                b for b in fresh_breaks
                if isinstance(b, dict) and b.get("name") and b.get("id") is not None
            ]
            breaks_meta = {str(b["name"]): dict(b) for b in valid_fresh}
            breaks_map = {str(b["name"]): int(b["id"]) for b in valid_fresh}
            self._breaks_meta_by_name = breaks_meta
            self._breaks_map = breaks_map

            if not breaks_map:
                messagebox.showerror(
                    "Break Reasons Unavailable",
                    "Break reasons could not be loaded from the server. Please check your connection and try again.",
                    parent=self.root,
                )
                return

            if sel_name not in breaks_map:
                first_name = next(iter(breaks_map.keys()))
                if hasattr(self, "combo_break"):
                    try:
                        self.combo_break.configure(values=list(breaks_map.keys()))
                    except Exception:
                        pass
                if sel_var:
                    sel_var.set(first_name)
                sel_name = first_name

        brk_id = breaks_map[sel_name]
        brk_info = breaks_meta.get(sel_name, {})
        requires_note = bool(brk_info.get("requires_note", False))

        note_val = ""
        if requires_note:
            prompted = self._prompt_break_note_dialog(sel_name)
            if prompted is None:
                # Employee cancelled note entry; do not start break
                return
            note_val = prompted

        def _worker():
            ok, msg = self.vm.pause_shift(break_reason_id=brk_id, note=note_val)
            def _ui_done():
                if ok:
                    self._update_header_data()
                    self.show_view("overview")
                else:
                    messagebox.showerror("Break Failed", msg, parent=self.root)
            self.root.after(0, _ui_done)

        threading.Thread(target=_worker, daemon=True, name="ShiftPauseWorker").start()

    def _on_resume_shift_click(self) -> None:
        def _worker():
            ok, msg = self.vm.resume_shift()
            def _ui_done():
                if ok:
                    self._update_header_data()
                    self.show_view("overview")
                else:
                    messagebox.showerror("Resume Shift Failed", msg, parent=self.root)
            self.root.after(0, _ui_done)

        threading.Thread(target=_worker, daemon=True, name="ShiftResumeWorker").start()

    def _on_end_shift_click(self) -> None:
        if not messagebox.askyesno("Confirm End Shift", "Are you sure you want to clock out and end your shift for today?", parent=self.root):
            return

        def _worker():
            ok, msg = self.vm.end_shift()
            def _ui_done():
                if ok:
                    self._update_header_data()
                    self.show_view("overview")
                else:
                    messagebox.showerror("End Shift Failed", msg, parent=self.root)
            self.root.after(0, _ui_done)

        threading.Thread(target=_worker, daemon=True, name="ShiftEndWorker").start()

    # --------------------------------------------------------------------------
    # VIEW: ACTIVITY TIMELINE
    # --------------------------------------------------------------------------
    def _render_timeline_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Activity Timeline",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 4))

        sub = tk.Label(
            self.content_container,
            text="Chronological breakdown of 60-second activity buckets recorded during your shift.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        )
        sub.pack(anchor="w", pady=(0, 12))

        # Table / Treeview Container
        table_frame = tk.Frame(self.content_container, bg=BG_CARD)
        table_frame.pack(fill="both", expand=True)

        columns = ("time", "state", "active", "idle", "app", "sync")
        self.timeline_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=14)

        self.timeline_tree.heading("time", text="Time Window")
        self.timeline_tree.heading("state", text="State")
        self.timeline_tree.heading("active", text="Active")
        self.timeline_tree.heading("idle", text="Idle")
        self.timeline_tree.heading("app", text="Primary Process")
        self.timeline_tree.heading("sync", text="Sync Status")

        self.timeline_tree.column("time", width=140, anchor="w")
        self.timeline_tree.column("state", width=90, anchor="center")
        self.timeline_tree.column("active", width=70, anchor="e")
        self.timeline_tree.column("idle", width=70, anchor="e")
        self.timeline_tree.column("app", width=180, anchor="w")
        self.timeline_tree.column("sync", width=90, anchor="center")

        # Scrollbar
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.timeline_tree.yview)
        self.timeline_tree.configure(yscrollcommand=scrollbar.set)

        self.timeline_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Populate rows
        intervals = self.vm.get_timeline_intervals(limit=60)
        for item in intervals:
            self.timeline_tree.insert("", "end", values=(
                item["time_range"],
                item["state"],
                item["active_str"],
                item["idle_str"],
                item["top_app"],
                item["sync_status"],
            ))

    # --------------------------------------------------------------------------
    # VIEW: APPLICATION USAGE
    # --------------------------------------------------------------------------
    def _render_apps_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Application Usage Breakdown",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 4))

        sub = tk.Label(
            self.content_container,
            text="Aggregated active time by process name for today's work shift.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        )
        sub.pack(anchor="w", pady=(0, 12))

        # Privacy Pill Notice
        notice_f = tk.Frame(self.content_container, bg=BG_CARD, padx=12, pady=8, highlightbackground=BORDER_COLOR, highlightthickness=1)
        notice_f.pack(fill="x", pady=(0, 16))

        lbl_notice = tk.Label(
            notice_f,
            text="💡 Transparency Notice: Only executable file names (.exe) are recorded. Window titles, browser URLs, files, and screen contents are NEVER captured.",
            font=("Segoe UI", 8),
            fg=ACCENT,
            bg=BG_CARD,
        )
        lbl_notice.pack(anchor="w")

        # Scrollable container for app progress rows
        apps = self.vm.get_app_usage()
        if not apps:
            lbl_empty = tk.Label(
                self.content_container,
                text="No application usage recorded for today yet.",
                font=("Segoe UI", 10),
                fg=TEXT_MUTED,
                bg=BG_DARK,
            )
            lbl_empty.pack(pady=40)
            return

        scroll_canvas = tk.Canvas(self.content_container, bg=BG_DARK, highlightthickness=0)
        scroll_bar = ttk.Scrollbar(self.content_container, orient="vertical", command=scroll_canvas.yview)
        scroll_content = tk.Frame(scroll_canvas, bg=BG_DARK)

        scroll_content.bind(
            "<Configure>",
            lambda e: scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))
        )
        scroll_canvas.create_window((0, 0), window=scroll_content, anchor="nw", width=740)
        scroll_canvas.configure(yscrollcommand=scroll_bar.set)

        scroll_canvas.pack(side="left", fill="both", expand=True)
        scroll_bar.pack(side="right", fill="y")

        for proc_name, dur_str, pct, _ in apps:
            row = tk.Frame(scroll_content, bg=BG_CARD, padx=16, pady=10, highlightbackground=BORDER_COLOR, highlightthickness=1)
            row.pack(fill="x", pady=4)

            top = tk.Frame(row, bg=BG_CARD)
            top.pack(fill="x")

            lbl_name = tk.Label(top, text=proc_name, font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
            lbl_name.pack(side="left")

            lbl_dur = tk.Label(top, text=f"{dur_str} ({pct}%)", font=("Segoe UI", 9), fg=ACCENT, bg=BG_CARD)
            lbl_dur.pack(side="right")

            # Visual progress bar
            pb_canvas = tk.Canvas(row, height=6, bg=BORDER_COLOR, highlightthickness=0)
            pb_canvas.pack(fill="x", pady=(6, 0))
            bar_width = max(2, int(700 * (pct / 100)))
            pb_canvas.create_rectangle(0, 0, bar_width, 6, fill=ACCENT, outline="")

    # --------------------------------------------------------------------------
    # VIEW: DEVICE & ACCOUNT
    # --------------------------------------------------------------------------
    def _render_device_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Device & Employee Account",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 16))

        info = self.vm.get_device_account()

        card = tk.Frame(self.content_container, bg=BG_CARD, padx=20, pady=16, highlightbackground=BORDER_COLOR, highlightthickness=1)
        card.pack(fill="x", pady=(0, 16))

        fields = [
            ("Workstation Hostname", info["machine_name"]),
            ("Operating System", info["os_version"]),
            ("Device UUID", info["device_uuid"]),
            ("Client Version", f"v{info['client_version']}"),
            ("Bound Employee Name", info["employee_name"]),
            ("Bound Employee ID", f"#{info['employee_id']}"),
            ("Bound Employee Email", info["employee_email"]),
            ("Device Token (Masked)", info["auth_token_masked"]),
            ("Target Backend URL", info["backend_url"]),
            ("Local Database", info["db_path"]),
            ("Log File", info["log_path"]),
        ]

        for idx, (label, val) in enumerate(fields):
            row = tk.Frame(card, bg=BG_CARD)
            row.pack(fill="x", pady=4)

            lbl_k = tk.Label(row, text=label, font=("Segoe UI", 9), fg=TEXT_MUTED, bg=BG_CARD, width=22, anchor="w")
            lbl_k.pack(side="left")

            lbl_v = tk.Label(row, text=str(val), font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD, anchor="w")
            lbl_v.pack(side="left", fill="x", expand=True)

        # Action Buttons
        btn_frame = tk.Frame(self.content_container, bg=BG_DARK)
        btn_frame.pack(fill="x", pady=8)

        btn_open_folder = tk.Button(
            btn_frame,
            text="📂 Open Data Folder",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=TEXT_MAIN,
            activebackground=BG_CARD_HOVER,
            activeforeground=TEXT_MAIN,
            relief="flat",
            padx=14,
            pady=8,
            cursor="hand2",
            command=self._on_open_data_folder,
        )
        btn_open_folder.pack(side="left", padx=(0, 10))

        btn_unpair = tk.Button(
            btn_frame,
            text="⚠️ Unpair / Re-register Device",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=COLOR_DANGER,
            activebackground=BG_CARD_HOVER,
            activeforeground=COLOR_DANGER,
            relief="flat",
            padx=14,
            pady=8,
            cursor="hand2",
            command=self._on_unpair_click,
        )
        btn_unpair.pack(side="left")

    def _on_open_data_folder(self) -> None:
        """Opens %LOCALAPPDATA%\\VaaniTracker in Windows Explorer."""
        data_dir = self.config.data_dir
        if data_dir.exists():
            if os.name == "nt":
                os.startfile(str(data_dir))
            else:
                subprocess.Popen(["xdg-open", str(data_dir)])

    def _on_unpair_click(self) -> None:
        """Prompts confirmation to revoke device credentials and unbind employee."""
        confirm = messagebox.askyesno(
            "Confirm Device Unpair",
            "Are you sure you want to unpair this workstation?\n\n"
            "This will clear local device credentials and halt activity tracking until re-registered.",
            parent=self.root,
        )
        if confirm:
            self.auth_manager.clear_credentials()
            messagebox.showinfo("Device Unpaired", "Device unpairing complete. Please re-register or pair device.", parent=self.root)
            self._update_header_data()
            self.show_view("device")

    # --------------------------------------------------------------------------
    # VIEW: SETTINGS
    # --------------------------------------------------------------------------
    def _render_settings_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Tracker Settings",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 16))

        card = tk.Frame(self.content_container, bg=BG_CARD, padx=20, pady=16, highlightbackground=BORDER_COLOR, highlightthickness=1)
        card.pack(fill="x", pady=(0, 16))

        # Backend URL
        lbl_url = tk.Label(card, text="Vaani CRM Backend URL", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_url.pack(anchor="w", pady=(0, 4))
        self.entry_url = tk.Entry(card, font=("Segoe UI", 9), bg=BG_DARK, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", bd=4)
        self.entry_url.insert(0, self.config.backend_url)
        self.entry_url.pack(fill="x", pady=(0, 12))

        # Idle Timeout
        lbl_idle = tk.Label(card, text="Idle Timeout Threshold (Seconds)", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_idle.pack(anchor="w", pady=(0, 4))
        self.entry_idle = tk.Entry(card, font=("Segoe UI", 9), bg=BG_DARK, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", bd=4)
        self.entry_idle.insert(0, str(self.config.idle_threshold_seconds))
        self.entry_idle.pack(fill="x", pady=(0, 12))

        # Sync Interval
        lbl_sync = tk.Label(card, text="Background Sync Interval (Seconds)", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_sync.pack(anchor="w", pady=(0, 4))
        self.entry_sync = tk.Entry(card, font=("Segoe UI", 9), bg=BG_DARK, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", bd=4)
        self.entry_sync.insert(0, str(self.config.sync_interval_seconds))
        self.entry_sync.pack(fill="x", pady=(0, 12))

        # Track Apps Checkbox
        self.var_track_apps = tk.BooleanVar(master=self.root, value=self.config.track_apps)
        chk_apps = tk.Checkbutton(
            card,
            text="Enable foreground application name tracking (.exe name only)",
            variable=self.var_track_apps,
            font=("Segoe UI", 9),
            fg=TEXT_MAIN,
            bg=BG_CARD,
            activebackground=BG_CARD,
            activeforeground=TEXT_MAIN,
            selectcolor=BG_DARK,
        )
        chk_apps.pack(anchor="w", pady=(0, 8))

        # Windows Startup Checkbox
        self.var_startup = tk.BooleanVar(master=self.root, value=is_run_on_startup_enabled())
        chk_startup = tk.Checkbutton(
            card,
            text="Launch Vaani Tracker automatically when Windows starts",
            variable=self.var_startup,
            font=("Segoe UI", 9),
            fg=TEXT_MAIN,
            bg=BG_CARD,
            activebackground=BG_CARD,
            activeforeground=TEXT_MAIN,
            selectcolor=BG_DARK,
        )
        chk_startup.pack(anchor="w", pady=(0, 16))

        # Save Button
        btn_save = tk.Button(
            card,
            text="💾 Save Settings",
            font=("Segoe UI", 9, "bold"),
            bg=ACCENT,
            fg="#000000",
            activebackground=ACCENT_HOVER,
            activeforeground="#000000",
            relief="flat",
            padx=16,
            pady=8,
            cursor="hand2",
            command=self._on_save_settings_click,
        )
        btn_save.pack(anchor="w")

    def _on_save_settings_click(self) -> None:
        """Validates and saves configuration changes to disk and runtime."""
        url = self.entry_url.get().strip()
        try:
            idle_thresh = int(self.entry_idle.get().strip())
            sync_int = int(self.entry_sync.get().strip())
            if idle_thresh < 10 or sync_int < 5:
                raise ValueError("Thresholds too small")
        except ValueError:
            messagebox.showerror("Invalid Input", "Idle threshold must be ≥10s and sync interval ≥5s.", parent=self.root)
            return

        self.config.backend_url = url.rstrip("/")
        self.config.idle_threshold_seconds = idle_thresh
        self.config.sync_interval_seconds = sync_int
        self.config.track_apps = self.var_track_apps.get()
        self.config.save()

        # Propagate settings dynamically to running in-memory components
        if self.syncer and hasattr(self.syncer, "api_client") and self.syncer.api_client:
            self.syncer.api_client.backend_url = self.config.backend_url
        if self.state_machine:
            self.state_machine.idle_threshold_seconds = idle_thresh
        if hasattr(self, "aggregator") and self.aggregator:
            self.aggregator.track_apps = self.config.track_apps
        if self.syncer:
            self.syncer.trigger_immediate_sync()

        # Update Windows startup registry
        set_run_on_startup(self.var_startup.get())

        messagebox.showinfo("Settings Saved", "Settings successfully updated and applied to active services.", parent=self.root)

    # --------------------------------------------------------------------------
    # VIEW: PRIVACY & RIGHTS
    # --------------------------------------------------------------------------
    def _render_privacy_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Employee Privacy & Transparency Matrix",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 4))

        sub = tk.Label(
            self.content_container,
            text="Vaani Desktop Tracker strictly adheres to workplace privacy principles. Review what is recorded vs. what is never collected.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        )
        sub.pack(anchor="w", pady=(0, 16))

        cols_f = tk.Frame(self.content_container, bg=BG_DARK)
        cols_f.pack(fill="x", pady=(0, 16))

        # Left Column: What IS Recorded
        col_yes = tk.Frame(cols_f, bg=BG_CARD, padx=16, pady=14, highlightbackground=BORDER_COLOR, highlightthickness=1)
        col_yes.pack(side="left", fill="both", expand=True, padx=(0, 8))

        lbl_yes_title = tk.Label(col_yes, text="✅ WHAT IS RECORDED", font=("Segoe UI", 10, "bold"), fg=COLOR_SUCCESS, bg=BG_CARD)
        lbl_yes_title.pack(anchor="w", pady=(0, 8))

        tracked_items = [
            "Active keyboard / mouse presence seconds",
            "Idle duration (absence of input for >60s)",
            "Windows Workstation Lock & Unlock events",
            "System Sleep, Suspend & Resume transitions",
            "Foreground executable name (e.g. 'code.exe')",
            "Authoritative shift start, break & end times",
        ]
        for item in tracked_items:
            row = tk.Label(col_yes, text=f"•  {item}", font=("Segoe UI", 9), fg=TEXT_MAIN, bg=BG_CARD, justify="left", wraplength=320)
            row.pack(anchor="w", pady=3)

        # Right Column: What is NEVER Recorded
        col_no = tk.Frame(cols_f, bg=BG_CARD, padx=16, pady=14, highlightbackground=BORDER_COLOR, highlightthickness=1)
        col_no.pack(side="right", fill="both", expand=True, padx=(8, 0))

        lbl_no_title = tk.Label(col_no, text="🚫 WHAT IS NEVER RECORDED", font=("Segoe UI", 10, "bold"), fg=COLOR_DANGER, bg=BG_CARD)
        lbl_no_title.pack(anchor="w", pady=(0, 8))

        never_tracked_items = [
            "NO keystrokes or typed text logging",
            "NO screenshots or desktop captures",
            "NO screen recording or video captures",
            "NO clipboard inspection or copied text",
            "NO web browser URLs or history tracking",
            "NO document, file, or email content reading",
            "NO webcam or microphone access",
        ]
        for item in never_tracked_items:
            row = tk.Label(col_no, text=f"•  {item}", font=("Segoe UI", 9), fg=TEXT_MAIN, bg=BG_CARD, justify="left", wraplength=320)
            row.pack(anchor="w", pady=3)

        # Employee Rights Box
        rights_box = tk.Frame(self.content_container, bg=BG_CARD, padx=16, pady=12, highlightbackground=BORDER_COLOR, highlightthickness=1)
        rights_box.pack(fill="x", pady=8)

        lbl_rights = tk.Label(
            rights_box,
            text="Employee Rights Guarantee: You can review your complete activity history at any time using the "
                 "Activity Timeline and Summary tools. The tracking agent halts automatically outside scheduled work shifts.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_CARD,
            wraplength=700,
            justify="left",
        )
        lbl_rights.pack(anchor="w")

    # --------------------------------------------------------------------------
    # VIEW: DIAGNOSTICS & LOGS
    # --------------------------------------------------------------------------
    def _render_diagnostics_view(self) -> None:
        header = tk.Label(
            self.content_container,
            text="Diagnostics & Runtime Logs",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_MAIN,
            bg=BG_DARK,
        )
        header.pack(anchor="w", pady=(0, 12))

        # Diagnostic Stats Bar
        diag = self.vm.get_diagnostics()
        stats_f = tk.Frame(self.content_container, bg=BG_CARD, padx=14, pady=10, highlightbackground=BORDER_COLOR, highlightthickness=1)
        stats_f.pack(fill="x", pady=(0, 10))

        items = [
            f"Database: {diag['database_size_kb']} KB",
            f"Total Buckets: {diag['total_buckets']}",
            f"Queued: {diag['pending_buckets']}",
            f"Synced: {diag['synced_buckets']}",
            f"Lock: {'Yes' if diag['is_locked'] else 'No'}",
            f"Sleep: {'Yes' if diag['is_sleeping'] else 'No'}",
        ]
        for it in items:
            lbl = tk.Label(stats_f, text=it, font=("Segoe UI", 8, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
            lbl.pack(side="left", padx=8)

        # Scrolled Text for Logs
        self.txt_logs = ScrolledText(
            self.content_container,
            height=13,
            bg=BG_SIDEBAR,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat",
            bd=4,
            font=("Consolas", 9),
        )
        self.txt_logs.pack(fill="both", expand=True, pady=(0, 10))

        logs_content = self.vm.get_recent_logs()
        self.txt_logs.insert("1.0", logs_content)
        self.txt_logs.see("end")

        # Action Buttons
        btn_bar = tk.Frame(self.content_container, bg=BG_DARK)
        btn_bar.pack(fill="x")

        btn_refresh = tk.Button(
            btn_bar,
            text="🔄 Refresh Logs",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=TEXT_MAIN,
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._on_refresh_logs,
        )
        btn_refresh.pack(side="left", padx=(0, 8))

        btn_copy = tk.Button(
            btn_bar,
            text="📋 Copy Diagnostic Info",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=ACCENT,
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._on_copy_diagnostics,
        )
        btn_copy.pack(side="left")

    def _on_refresh_logs(self) -> None:
        """Refreshes log text display."""
        if hasattr(self, "txt_logs") and self.txt_logs.winfo_exists():
            self.txt_logs.delete("1.0", "end")
            self.txt_logs.insert("1.0", self.vm.get_recent_logs())
            self.txt_logs.see("end")

    def _on_copy_diagnostics(self) -> None:
        """Copies masked diagnostic report to clipboard."""
        diag = self.vm.get_diagnostics()
        dev = self.vm.get_device_account()
        report = (
            f"=== VAANI TRACKER DIAGNOSTIC REPORT ===\n"
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
            f"Client Version: {dev['client_version']}\n"
            f"Machine: {dev['machine_name']}\n"
            f"OS: {dev['os_version']}\n"
            f"Device UUID: {dev['device_uuid']}\n"
            f"Employee: {dev['employee_name']} (#{dev['employee_id']})\n"
            f"Backend URL: {dev['backend_url']}\n"
            f"Database Size: {diag['database_size_kb']} KB\n"
            f"Total Buckets: {diag['total_buckets']}\n"
            f"Pending Sync: {diag['pending_buckets']}\n"
            f"State: {diag['current_state']}\n"
            f"Locked: {diag['is_locked']}, Sleeping: {diag['is_sleeping']}\n"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(report)
        messagebox.showinfo("Copied", "Diagnostic report copied to clipboard.", parent=self.root)

    # --------------------------------------------------------------------------
    # VIEW: ABOUT
    # --------------------------------------------------------------------------
    def _render_about_view(self) -> None:
        about_card = tk.Frame(self.content_container, bg=BG_CARD, padx=24, pady=20, highlightbackground=BORDER_COLOR, highlightthickness=1)
        about_card.pack(fill="both", expand=True)

        # App Logo
        icon_path = get_asset_path("vaani-logo.png")
        if icon_path.exists():
            try:
                pil_logo = Image.open(icon_path).resize((180, 120), Image.Resampling.LANCZOS)
                self._about_logo_img = ImageTk.PhotoImage(pil_logo, master=self.root)
                lbl_logo = tk.Label(about_card, image=self._about_logo_img, bg=BG_CARD)
                lbl_logo.pack(anchor="center", pady=(10, 16))
            except Exception:
                pass

        lbl_app = tk.Label(about_card, text="Vaani Desktop Activity Tracker", font=("Segoe UI", 16, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_app.pack(anchor="center")

        lbl_v = tk.Label(about_card, text=f"Version {APP_VERSION} (Production Release)", font=("Segoe UI", 10), fg=ACCENT, bg=BG_CARD)
        lbl_v.pack(anchor="center", pady=(2, 12))

        lbl_desc = tk.Label(
            about_card,
            text="High-reliability Windows desktop activity tracker designed for enterprise telecalling teams and remote video editors.\n"
                 "Engineered with transparent work duration auditing, offline SQLite WAL queuing, and native Win32 hardware event monitoring.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_CARD,
            justify="center",
            wraplength=600,
        )
        lbl_desc.pack(anchor="center", pady=(0, 16))

        lbl_copy = tk.Label(about_card, text="© 2026 Vaani Telecalling CRM · All Rights Reserved", font=("Segoe UI", 8), fg=TEXT_DARK, bg=BG_CARD)
        lbl_copy.pack(anchor="center", pady=(0, 12))

        btn_site = tk.Button(
            about_card,
            text="🌐 Visit Vaani Website",
            font=("Segoe UI", 9, "bold"),
            bg=ACCENT,
            fg="#000000",
            activebackground=ACCENT_HOVER,
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
            command=self._on_visit_website,
        )
        btn_site.pack(anchor="center")

    def _on_visit_website(self) -> None:
        import webbrowser
        webbrowser.open("https://vaaniapp.co.in")

    # --------------------------------------------------------------------------
    # CONTROLLER & EVENT METHODS
    # --------------------------------------------------------------------------
    def _on_sync_now_click(self) -> None:
        """Triggers manual background sync and refreshes status badge."""
        self.btn_sync_now.configure(text="⏳ Syncing...", state="disabled")

        def _worker():
            try:
                self.syncer.sync_pending_now()
            finally:
                if not self._is_closed:
                    self.root.after(0, self._on_sync_finished)

        threading.Thread(target=_worker, daemon=True, name="DashboardManualSync").start()

    def _on_sync_finished(self) -> None:
        """Called when manual sync completes."""
        self.btn_sync_now.configure(text="🔄 Sync Now", state="normal")
        self._update_header_data()
        if self._current_tab in ("overview", "timeline", "diagnostics"):
            self.show_view(self._current_tab)

    def _update_header_data(self) -> None:
        """Updates header badge indicators."""
        header = self.vm.get_header_data()

        # Update sync badge
        if header["is_synced"]:
            self.lbl_sync_badge.configure(text="☁ All synced", fg=COLOR_SUCCESS)
        else:
            self.lbl_sync_badge.configure(text=f"☁ {header['pending_count']} pending", fg=COLOR_WARNING)

        # Update state pill
        self.lbl_state_text.configure(text=header["state_label"])
        self.state_canvas.itemconfig(self.state_dot, fill=header["state_color"])

        # Update employee pill
        self.lbl_emp_pill.configure(text=f"👤 {header['employee_name']} (#{header['employee_id']})")

    def _tick_ui(self) -> None:
        """Periodic UI update every 2 seconds."""
        if self._is_closed or self._is_closing:
            return

        try:
            self._update_header_data()
        except Exception:
            pass

        if not self._is_closed and not self._is_closing and hasattr(self, "root") and self.root:
            try:
                self._tick_id = self.root.after(2000, self._tick_ui)
            except Exception:
                self._tick_id = None

    def hide_to_tray(self) -> None:
        """Minimizes dashboard window to system tray instead of destroying."""
        if self._is_closed or self._is_closing:
            return
        if hasattr(self, "root") and self.root:
            try:
                self.root.withdraw()
            except Exception:
                pass

    def show(self) -> None:
        """Restores and brings dashboard window to foreground."""
        if self._is_closed or self._is_closing:
            return
        if hasattr(self, "root") and self.root:
            try:
                self.root.deiconify()
                try:
                    self.root.state("normal")
                except Exception:
                    pass
                self.root.lift()
                self.root.focus_force()
                self._update_header_data()
                self.show_view(self._current_tab)
            except Exception as e:
                logger.debug(f"Error restoring dashboard: {e}")

    def request_close(self) -> None:
        """Thread-safe request to close dashboard on the Tk-owning thread."""
        if self._is_closing or self._is_closed:
            return
        self._is_closing = True
        if hasattr(self, "root") and self.root:
            try:
                self.root.after(0, self._destroy_on_main_thread)
            except Exception as e:
                logger.debug(f"Error scheduling Tk destroy, executing direct fallback: {e}")
                self._destroy_on_main_thread()

    def _destroy_on_main_thread(self) -> None:
        """Destroys UI and unblocks mainloop on the Tk-owning thread only."""
        if self._is_closed:
            return
        self._is_closed = True
        self._is_closing = True

        if hasattr(self, "_tick_id") and self._tick_id and hasattr(self, "root") and self.root:
            try:
                self.root.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None

        if hasattr(self, "root") and self.root:
            try:
                self.root.quit()
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass

    def close(self) -> None:
        """Halts dashboard and cleans up resources."""
        self.request_close()

