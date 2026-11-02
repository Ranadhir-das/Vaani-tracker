"""Configuration management for Vaani Desktop Tracker."""

import json
import os
import platform
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any

from .constants import (
    DEFAULT_APP_DIR_NAME,
    DEFAULT_DB_FILE_NAME,
    DEFAULT_CONFIG_FILE_NAME,
    DEFAULT_LOG_FILE_NAME,
    DEFAULT_IDLE_THRESHOLD_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_SYNC_INTERVAL_SECONDS,
)


def get_default_data_dir() -> Path:
    """Returns the default directory for application data and local storage.

    On Windows: %LOCALAPPDATA%\\VaaniTracker (e.g., C:\\Users\\...\\AppData\\Local\\VaaniTracker)
    On Linux/macOS: ~/.vaani_tracker
    """
    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base_dir = Path(local_app_data)
        else:
            base_dir = Path.home() / "AppData" / "Local"
    else:
        base_dir = Path.home()

    target = base_dir / DEFAULT_APP_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


@dataclass
class TrackerConfig:
    """Configuration settings for the desktop tracker instance."""
    backend_url: str = "http://127.0.0.1:8000"
    idle_threshold_seconds: int = DEFAULT_IDLE_THRESHOLD_SECONDS
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    track_apps: bool = True
    log_level: str = "INFO"
    data_dir: Path = field(default_factory=get_default_data_dir)
    device_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    auth_token: Optional[str] = None
    registered_employee_id: Optional[int] = None
    registered_employee_name: Optional[str] = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / DEFAULT_DB_FILE_NAME

    @property
    def config_path(self) -> Path:
        return self.data_dir / DEFAULT_CONFIG_FILE_NAME

    @property
    def log_path(self) -> Path:
        return self.data_dir / DEFAULT_LOG_FILE_NAME

    @property
    def is_registered(self) -> bool:
        return bool(self.auth_token and self.device_uuid)

    @classmethod
    def load(cls, custom_data_dir: Optional[Path] = None, config_file: Optional[Path] = None) -> "TrackerConfig":
        """Loads configuration from environment variables and persisted config.json."""
        data_dir = custom_data_dir or get_default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)

        config_path = config_file or (data_dir / DEFAULT_CONFIG_FILE_NAME)
        file_data: Dict[str, Any] = {}

        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
            except Exception:
                pass

        # Environment variable overrides
        backend_url = os.environ.get("VAANI_BACKEND_URL", file_data.get("backend_url", "http://127.0.0.1:8000"))
        idle_threshold = int(os.environ.get("VAANI_IDLE_THRESHOLD", file_data.get("idle_threshold_seconds", DEFAULT_IDLE_THRESHOLD_SECONDS)))
        heartbeat_interval = int(os.environ.get("VAANI_HEARTBEAT_INTERVAL", file_data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)))
        sync_interval = int(os.environ.get("VAANI_SYNC_INTERVAL", file_data.get("sync_interval_seconds", DEFAULT_SYNC_INTERVAL_SECONDS)))
        track_apps = str(os.environ.get("VAANI_TRACK_APPS", file_data.get("track_apps", True))).lower() in ("true", "1", "yes")
        log_level = os.environ.get("VAANI_LOG_LEVEL", file_data.get("log_level", "INFO")).upper()
        device_uuid = file_data.get("device_uuid") or str(uuid.uuid4())
        auth_token = file_data.get("auth_token")
        employee_id = file_data.get("registered_employee_id")
        employee_name = file_data.get("registered_employee_name")

        instance = cls(
            backend_url=backend_url.rstrip("/"),
            idle_threshold_seconds=idle_threshold,
            heartbeat_interval_seconds=heartbeat_interval,
            sync_interval_seconds=sync_interval,
            track_apps=track_apps,
            log_level=log_level,
            data_dir=data_dir,
            device_uuid=device_uuid,
            auth_token=auth_token,
            registered_employee_id=employee_id,
            registered_employee_name=employee_name,
        )

        # Save to persist newly generated device_uuid if not already saved
        if not config_path.exists() or "device_uuid" not in file_data:
            instance.save()

        return instance

    def save(self) -> None:
        """Persists current configuration to disk."""
        data = {
            "backend_url": self.backend_url,
            "idle_threshold_seconds": self.idle_threshold_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "sync_interval_seconds": self.sync_interval_seconds,
            "track_apps": self.track_apps,
            "log_level": self.log_level,
            "device_uuid": self.device_uuid,
            "auth_token": self.auth_token,
            "registered_employee_id": self.registered_employee_id,
            "registered_employee_name": self.registered_employee_name,
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

