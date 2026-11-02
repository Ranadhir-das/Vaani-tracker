"""Constants and enumeration definitions for Vaani Desktop Tracker."""

from enum import Enum


class TrackerState(str, Enum):
    """Activity tracking states.

    ACTIVE: Keyboard/mouse activity within idle threshold during an active shift.
    IDLE: No user input for >= idle threshold (60s) during an active shift.
    LOCKED: Windows workstation is locked (WTS_SESSION_LOCK).
    SLEEPING: System is in sleep/suspend power state.
    PAUSED: Work shift is paused on break.
    DISCONNECTED: Backend API is unreachable / offline queueing mode.
    STOPPED: No active shift, shift ended, or employee logged out.
    """
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    LOCKED = "LOCKED"
    SLEEPING = "SLEEPING"
    PAUSED = "PAUSED"
    DISCONNECTED = "DISCONNECTED"
    STOPPED = "STOPPED"


class ShiftStatus(str, Enum):
    """Authoritative shift status from Vaani Django CRM."""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    NONE = "NONE"


class SyncStatus(str, Enum):
    """Storage queue synchronization status."""
    PENDING = "PENDING"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


# Operational defaults
DEFAULT_IDLE_THRESHOLD_SECONDS = 60
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_SYNC_INTERVAL_SECONDS = 60
DEFAULT_BUCKET_DURATION_SECONDS = 60
MAX_RETRY_COUNT = 10
MAX_OFFLINE_DAYS = 7

# Storage database configuration
DEFAULT_APP_DIR_NAME = "VaaniTracker"
DEFAULT_DB_FILE_NAME = "events.db"
DEFAULT_CONFIG_FILE_NAME = "config.json"
DEFAULT_LOG_FILE_NAME = "tracker.log"

# Win32 Messages & Notification Codes
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1
WM_WTSSESSION_CHANGE = 0x02B1
WM_POWERBROADCAST = 0x0218

# WTS Session notification event codes
WTS_CONSOLE_CONNECT = 0x1
WTS_CONSOLE_DISCONNECT = 0x2
WTS_REMOTE_CONNECT = 0x3
WTS_REMOTE_DISCONNECT = 0x4
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
WTS_SESSION_REMOTE_CONTROL = 0x9

# Power Broadcast event codes
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007

