"""Unit and regression tests for DashboardViewModel and UI data presentation."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, ShiftStatus, SyncStatus
from vaani_tracker.models import ShiftInfo, ActivityBucket, AppUsage
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.ui.dashboard import DashboardViewModel, is_run_on_startup_enabled


class MockNotifier:
    def __init__(self, is_active=False):
        self.is_active = is_active
        self.is_locked = is_active
        self.is_sleeping = is_active

    def start(self): return True
    def stop(self, timeout=2.0): return True


class MockStateMachine:
    def __init__(self, state=TrackerState.ACTIVE, shift=None):
        self.current_state = state
        self.shift_info = shift or ShiftInfo(
            session_id=101,
            status=ShiftStatus.ACTIVE,
            net_seconds=3600,
            break_seconds=600,
            current_break_name="Lunch",
        )
        self.session_notifier = MockNotifier(is_active=False)
        self.power_notifier = MockNotifier(is_active=False)

    def add_state_listener(self, cb): pass


class MockSyncer:
    def __init__(self):
        self.sync_called = False

    def sync_pending_now(self):
        self.sync_called = True


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def sample_setup(temp_dir):
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    cfg.device_uuid = "test-device-uuid-1234"
    cfg.backend_url = "http://127.0.0.1:8085"
    cfg.registered_employee_id = 42
    cfg.registered_employee_name = "Rahul Sharma"
    cfg.auth_token = "vdt_mock_token_abcdef123456"

    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    auth.bind_employee(42, "Rahul Sharma", "rahul@vaani.internal")
    auth.set_token("vdt_mock_token_abcdef123456")

    sm = MockStateMachine()
    syncer = MockSyncer()

    vm = DashboardViewModel(
        config=cfg,
        state_machine=sm,
        syncer=syncer,
        queue=queue,
        auth_manager=auth,
    )
    return cfg, queue, auth, sm, syncer, vm


def test_mask_token():
    assert DashboardViewModel.mask_token(None) == "None"
    assert DashboardViewModel.mask_token("") == "None"
    assert DashboardViewModel.mask_token("short") == "***"
    token = "vdt_mock_token_12345678"
    masked = DashboardViewModel.mask_token(token)
    assert masked.startswith("vdt_mo")
    assert masked.endswith("***")
    assert "12345678" not in masked


def test_get_header_data(sample_setup):
    cfg, queue, auth, sm, syncer, vm = sample_setup

    data = vm.get_header_data()
    assert data["employee_name"] == "Rahul Sharma"
    assert data["employee_id"] == 42
    assert data["employee_email"] == "rahul@vaani.internal"
    assert data["state"] == TrackerState.ACTIVE
    assert data["state_label"] == "ACTIVE"
    assert data["is_synced"] is True
    assert "All synced" in data["sync_status_text"]


def test_kpi_cards_calculation(sample_setup):
    cfg, queue, auth, sm, syncer, vm = sample_setup

    # Insert test bucket into queue
    now_iso = datetime.now(timezone.utc).isoformat()
    bucket = ActivityBucket(
        bucket_id="kpi-bucket-1",
        device_uuid=cfg.device_uuid,
        employee_id=42,
        session_id=101,
        bucket_start=now_iso,
        bucket_end=now_iso,
        active_seconds=45,
        idle_seconds=15,
        state=TrackerState.ACTIVE,
        app_usages=[AppUsage(process_name="code.exe", duration_seconds=45)],
    )
    queue.save_bucket(bucket, employee_id=42)

    cards = vm.get_kpi_cards()
    assert len(cards) == 6

    titles = [c["title"] for c in cards]
    assert "Active Work Time" in titles
    assert "Idle Time" in titles
    assert "Break Time" in titles
    assert "Current State" in titles
    assert "Pending Sync" in titles
    assert "Workstation Status" in titles

    active_card = next(c for c in cards if c["title"] == "Active Work Time")
    assert "45s" in active_card["value"]

    idle_card = next(c for c in cards if c["title"] == "Idle Time")
    assert "15s" in idle_card["value"]


def test_get_timeline_intervals(sample_setup):
    cfg, queue, auth, sm, syncer, vm = sample_setup

    now_iso = datetime.now(timezone.utc).isoformat()
    bucket = ActivityBucket(
        bucket_id="timeline-bucket-1",
        device_uuid=cfg.device_uuid,
        employee_id=42,
        session_id=101,
        bucket_start="2026-11-03T10:00:00+00:00",
        bucket_end="2026-11-03T10:01:00+00:00",
        active_seconds=50,
        idle_seconds=10,
        state=TrackerState.ACTIVE,
        sync_status=SyncStatus.PENDING,
        app_usages=[AppUsage(process_name="Adobe Premiere Pro.exe", duration_seconds=50)],
    )
    queue.save_bucket(bucket, employee_id=42)

    intervals = vm.get_timeline_intervals(limit=10)
    assert len(intervals) >= 1
    item = intervals[0]
    assert item["state"] == "ACTIVE"
    assert item["active_seconds"] == 50
    assert item["idle_seconds"] == 10
    assert item["top_app"] == "Adobe Premiere Pro.exe"
    assert item["sync_status"] == "PENDING"


def test_get_app_usage_aggregation(sample_setup):
    cfg, queue, auth, sm, syncer, vm = sample_setup

    now_iso = datetime.now(timezone.utc).isoformat()
    bucket1 = ActivityBucket(
        bucket_id="app-b1",
        device_uuid=cfg.device_uuid,
        employee_id=42,
        session_id=101,
        bucket_start=now_iso,
        bucket_end=now_iso,
        active_seconds=60,
        idle_seconds=0,
        state=TrackerState.ACTIVE,
        app_usages=[
            AppUsage(process_name="code.exe", duration_seconds=40),
            AppUsage(process_name="chrome.exe", duration_seconds=20),
        ],
    )
    queue.save_bucket(bucket1, employee_id=42)

    apps = vm.get_app_usage()
    assert len(apps) == 2
    # Sorted by duration descending
    assert apps[0][0] == "code.exe"
    assert apps[0][3] == 40
    assert apps[1][0] == "chrome.exe"
    assert apps[1][3] == 20


def test_get_device_account_info(sample_setup):
    cfg, queue, auth, sm, syncer, vm = sample_setup

    info = vm.get_device_account()
    assert info["device_uuid"] == "test-device-uuid-1234"
    assert info["employee_name"] == "Rahul Sharma"
    assert info["employee_id"] == 42
    assert info["is_authenticated"] is True
    assert "vdt_mock_token_abcdef123456" not in info["auth_token_masked"]
    assert "***" in info["auth_token_masked"]


def test_get_diagnostics_and_logs(sample_setup, temp_dir):
    cfg, queue, auth, sm, syncer, vm = sample_setup

    # Write a test log file
    with open(cfg.log_path, "w", encoding="utf-8") as f:
        f.write("[2026-11-03 12:00:00] [INFO] [app]: Tracker starting\n")
        f.write("[2026-11-03 12:00:01] [INFO] [auth]: Token set: vdt_secret_token_99999\n")

    diag = vm.get_diagnostics()
    assert diag["total_buckets"] >= 0
    assert diag["pending_buckets"] >= 0

    logs = vm.get_recent_logs()
    assert "Tracker starting" in logs
    # Verify token masking in logs
    assert "vdt_secret_token_99999" not in logs
    assert "vdt_***" in logs

