"""Data models and type definitions for Vaani Desktop Tracker."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import uuid

from .constants import TrackerState, ShiftStatus, SyncStatus


def current_utc_iso() -> str:
    """Returns current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeviceInfo:
    """Hardware device identity and binding information."""
    device_uuid: str
    machine_name: str
    os_version: str
    client_version: str
    auth_token: Optional[str] = None
    employee_id: Optional[int] = None
    employee_name: Optional[str] = None
    employee_email: Optional[str] = None
    is_approved: bool = False
    registered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ShiftInfo:
    """Authoritative attendance shift information from Vaani backend."""
    session_id: Optional[int] = None
    status: ShiftStatus = ShiftStatus.NONE
    started_at: Optional[str] = None
    net_seconds: int = 0
    break_seconds: int = 0
    current_break_name: Optional[str] = None
    server_time: Optional[str] = None
    last_checked_at: str = field(default_factory=current_utc_iso)

    @property
    def is_active(self) -> bool:
        return self.status == ShiftStatus.ACTIVE and self.session_id is not None

    @property
    def is_paused(self) -> bool:
        return self.status == ShiftStatus.PAUSED and self.session_id is not None

    @property
    def is_tracking_eligible(self) -> bool:
        """Tracking is only eligible when an authenticated shift is active."""
        return self.is_active

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['status'] = self.status.value
        return d


@dataclass
class AppUsage:
    """Transparent foreground application usage duration within a time bucket.

    Only the executable basename (e.g. 'code.exe') is captured.
    Window titles, document contents, and web URLs are NEVER collected.
    """
    process_name: str
    duration_seconds: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ActivityBucket:
    """Aggregated 60-second activity record.

    Represents a single time-bucket slice of employee activity during an active shift.
    Identified by a globally unique UUIDv4 for backend idempotency.
    """
    bucket_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    device_uuid: str = ""
    session_id: Optional[int] = None
    bucket_start: str = field(default_factory=current_utc_iso)
    bucket_end: str = field(default_factory=current_utc_iso)
    active_seconds: int = 0
    idle_seconds: int = 0
    state: TrackerState = TrackerState.STOPPED
    is_locked: bool = False
    is_sleeping: bool = False
    is_paused: bool = False
    app_usages: List[AppUsage] = field(default_factory=list)
    sync_status: SyncStatus = SyncStatus.PENDING
    retry_count: int = 0
    created_at: str = field(default_factory=current_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bucket_id": self.bucket_id,
            "device_uuid": self.device_uuid,
            "session_id": self.session_id,
            "bucket_start": self.bucket_start,
            "bucket_end": self.bucket_end,
            "active_seconds": self.active_seconds,
            "idle_seconds": self.idle_seconds,
            "state": self.state.value if isinstance(self.state, TrackerState) else str(self.state),
            "is_locked": self.is_locked,
            "is_sleeping": self.is_sleeping,
            "is_paused": self.is_paused,
            "app_usages": [app.to_dict() for app in self.app_usages],
            "sync_status": self.sync_status.value if isinstance(self.sync_status, SyncStatus) else str(self.sync_status),
            "retry_count": self.retry_count,
            "created_at": self.created_at,
        }

    def to_api_payload(self) -> Dict[str, Any]:
        """Payload format sent to backend HTTPS sync endpoint."""
        return {
            "bucket_id": self.bucket_id,
            "session_id": self.session_id,
            "bucket_start": self.bucket_start,
            "bucket_end": self.bucket_end,
            "active_seconds": self.active_seconds,
            "idle_seconds": self.idle_seconds,
            "state": self.state.value if isinstance(self.state, TrackerState) else str(self.state),
            "is_locked": self.is_locked,
            "is_sleeping": self.is_sleeping,
            "app_usages": [app.to_dict() for app in self.app_usages],
        }


@dataclass
class SyncResult:
    """Outcome of an HTTPS synchronization attempt."""
    success: bool
    synced_count: int = 0
    failed_count: int = 0
    server_time: Optional[str] = None
    error_message: Optional[str] = None
    is_revoked: bool = False
    is_duplicate: bool = False

