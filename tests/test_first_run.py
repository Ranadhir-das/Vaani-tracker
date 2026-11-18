"""Unit, integration, and security regression tests for first-run wizard and device pairing logic.

Covers:
1. Existing pairing-code registration remains functional.
2. Username/password success, if supported.
3. Invalid username/password.
4. Ineligible or inactive employee.
5. Network timeout and server errors.
6. Missing or malformed device token.
7. Employee ID mismatch or non-positive.
8. DPAPI storage and token retrieval.
9. Password never written to disk, logs, or SQLite.
10. Failed registration leaves the tracker unbound.
11. Existing bound employee cannot be silently replaced.
12. First-run UI validation and switching between authentication methods.
"""

import json
import platform
import sqlite3
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tkinter as tk

from vaani_tracker.config import TrackerConfig
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.models import DeviceInfo
from vaani_tracker.ui.first_run import FirstRunWizard


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ==============================================================================
# EXISTING BASELINE TESTS
# ==============================================================================

def test_unauthenticated_device_state(temp_dir):
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    cfg.auth_token = None
    cfg.registered_employee_id = None
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    assert not auth.is_authenticated()
    emp = auth.get_bound_employee()
    assert emp["employee_id"] is None


def test_pairing_workflow_success(temp_dir):
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    mock_client = MagicMock()
    mock_client.register_device.return_value = (
        True,
        {
            "status": "APPROVED",
            "auth_token": "vdt_mock_token_778899",
            "employee_id": 55,
            "employee_name": "Priya Patel",
            "employee_email": "priya@vaani.internal",
        }
    )

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name=platform.node(),
        os_version="Windows 11",
        client_version="1.0.0",
    )

    success, resp = mock_client.register_device(device_info, pairing_code="VN-7788")
    assert success is True

    token = resp.get("auth_token")
    emp_id = resp.get("employee_id")
    emp_name = resp.get("employee_name")
    emp_email = resp.get("employee_email")

    auth.set_token(token)
    auth.bind_employee(emp_id, emp_name, emp_email)

    assert auth.is_authenticated()
    assert auth.get_token() == "vdt_mock_token_778899"
    bound = auth.get_bound_employee()
    assert bound["employee_id"] == 55
    assert bound["employee_name"] == "Priya Patel"
    assert bound["employee_email"] == "priya@vaani.internal"


def test_pairing_workflow_failure(temp_dir):
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    mock_client = MagicMock()
    mock_client.register_device.return_value = (
        False,
        {"error": "INVALID_CODE", "message": "The pairing code has expired or is invalid."}
    )

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name=platform.node(),
        os_version="Windows 11",
        client_version="1.0.0",
    )

    success, resp = mock_client.register_device(device_info, pairing_code="INVALID")
    assert success is False
    assert resp["error"] == "INVALID_CODE"
    assert not auth.is_authenticated()


def test_unpair_and_clear_credentials(temp_dir):
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    auth.set_token("test-token")
    auth.bind_employee(10, "Test User", "test@test.com")
    assert auth.is_authenticated()

    auth.clear_credentials()
    assert not auth.is_authenticated()
    assert auth.get_token() is None
    bound = auth.get_bound_employee()
    assert bound["employee_id"] is None


# ==============================================================================
# PHASE 3: 12 SPECIFIED SECURITY AND REGRESSION TESTS
# ==============================================================================

def test_1_pairing_code_registration_functional(temp_dir):
    """1. Existing pairing-code registration remains fully functional."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-01",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    mock_resp = {
        "status": "APPROVED",
        "auth_token": "vdt_pairing_token_abc_12345",
        "device_id": cfg.device_uuid,
        "employee_id": 101,
        "employee_name": "Aarav Sharma",
        "employee_email": "aarav@vaani.internal",
    }

    with patch.object(api, "_make_request", return_value=(201, mock_resp)) as mock_req:
        ok, resp = api.register_device(device_info, pairing_code="VN-9428")
        assert ok is True
        mock_req.assert_called_once()
        _, kwargs = mock_req.call_args
        assert kwargs["data"]["pairing_code"] == "VN-9428"
        assert "username" not in kwargs["data"]
        assert "password" not in kwargs["data"]

        bind_ok = auth.register_and_bind(
            token=resp["auth_token"],
            employee_id=resp["employee_id"],
            employee_name=resp["employee_name"],
            employee_email=resp["employee_email"],
        )
        assert bind_ok is True
        assert auth.is_authenticated() is True
        assert auth.get_authenticated_employee_id() == 101
        assert auth.get_token() == "vdt_pairing_token_abc_12345"
        assert queue.get_meta("auth_state") == "BOUND"


def test_2_username_password_registration_success(temp_dir):
    """2. Username/password registration succeeds and creates identical binding."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-02",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    mock_resp = {
        "status": "APPROVED",
        "auth_token": "vdt_creds_token_xyz_67890",
        "device_id": cfg.device_uuid,
        "employee_id": 102,
        "employee_name": "Priya Nair",
        "employee_email": "priya@vaani.internal",
    }

    with patch.object(api, "_make_request", return_value=(201, mock_resp)) as mock_req:
        ok, resp = api.register_device(device_info, username="priya.n", password="ValidPassword123!")
        assert ok is True
        mock_req.assert_called_once()
        _, kwargs = mock_req.call_args
        assert kwargs["data"]["username"] == "priya.n"
        assert kwargs["data"]["password"] == "ValidPassword123!"
        assert "pairing_code" not in kwargs["data"]

        bind_ok = auth.register_and_bind(
            token=resp["auth_token"],
            employee_id=resp["employee_id"],
            employee_name=resp["employee_name"],
            employee_email=resp["employee_email"],
        )
        assert bind_ok is True
        assert auth.is_authenticated() is True
        assert auth.get_authenticated_employee_id() == 102
        assert auth.get_token() == "vdt_creds_token_xyz_67890"
        assert queue.get_meta("auth_state") == "BOUND"


def test_3_invalid_username_password_fails_closed(temp_dir):
    """3. Invalid username or password returns failure and leaves tracker unbound."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-03",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    err_resp = {"error": "INVALID_CREDENTIALS", "message": "Invalid username or password."}
    with patch.object(api, "_make_request", return_value=(401, err_resp)):
        ok, resp = api.register_device(device_info, username="bad_user", password="bad_password")
        assert ok is False
        assert resp["error"] == "INVALID_CREDENTIALS"

    assert auth.is_authenticated() is False
    assert auth.get_authenticated_employee_id() is None
    assert auth.get_token() is None
    assert queue.get_meta("auth_state") is None


def test_4_ineligible_or_inactive_employee(temp_dir):
    """4. Ineligible or inactive employee is rejected (HTTP 403) and leaves tracker unbound."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-04",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    err_resp = {"error": "INELIGIBLE_EMPLOYEE", "message": "Employee is not eligible for desktop PC tracking."}
    with patch.object(api, "_make_request", return_value=(403, err_resp)):
        ok, resp = api.register_device(device_info, username="ineligible.emp", password="password")
        assert ok is False
        assert resp["error"] == "INELIGIBLE_EMPLOYEE"

    assert auth.is_authenticated() is False
    assert auth.get_authenticated_employee_id() is None
    assert queue.get_meta("auth_state") is None


def test_5_network_timeout_and_server_errors(temp_dir):
    """5. Network timeouts, connection errors, and server 500 errors fail closed."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-05",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    # 5a: HTTP 500 Server Error
    with patch.object(api, "_make_request", return_value=(500, {"error": "SERVER_ERROR", "message": "Internal error"})):
        ok, resp = api.register_device(device_info, username="user", password="pwd")
        assert ok is False
        assert auth.is_authenticated() is False

    # 5b: Network URLError (connection refused/timeout)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection timed out")):
        ok, resp = api.register_device(device_info, username="user", password="pwd")
        assert ok is False
        assert resp.get("error") == "NETWORK_UNREACHABLE"
        assert auth.is_authenticated() is False
        assert auth.get_authenticated_employee_id() is None


def test_6_missing_or_malformed_device_token(temp_dir):
    """6. Missing or malformed device token rejects registration atomically."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    for invalid_token in [None, "", "   ", 12345]:
        ok = auth.register_and_bind(
            token=invalid_token,
            employee_id=106,
            employee_name="Malformed Token Test",
        )
        assert ok is False
        assert auth.is_authenticated() is False
        assert auth.get_authenticated_employee_id() is None
        assert auth.get_token() is None


def test_7_employee_id_mismatch_or_non_positive(temp_dir):
    """7. Non-positive, missing, or non-numeric employee ID fails closed."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    for invalid_id in [None, 0, -1, "not-numeric", ""]:
        ok = auth.register_and_bind(
            token="vdt_valid_token_777",
            employee_id=invalid_id,
            employee_name="Bad ID Test",
        )
        assert ok is False
        assert auth.is_authenticated() is False
        assert auth.get_authenticated_employee_id() is None


def test_8_dpapi_storage_and_token_retrieval(temp_dir):
    """8. Device token is encrypted via DPAPI on disk and decrypted on retrieval."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    raw_token = "vdt_dpapi_secret_token_888999"
    ok = auth.register_and_bind(
        token=raw_token,
        employee_id=108,
        employee_name="DPAPI User",
        employee_email="dpapi@vaani.internal",
    )
    assert ok is True

    # 1. Directly inspect SQLite database file
    conn = sqlite3.connect(str(cfg.db_path))
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM meta WHERE key = 'auth_token'")
        stored_token_val = cursor.fetchone()[0]
    finally:
        conn.close()

    assert stored_token_val.startswith("dpapi:"), "Stored token must be prefixed with 'dpapi:'"
    assert raw_token not in stored_token_val, "Raw token must never appear unencrypted in SQLite"

    # 2. Verify auth manager decrypts and retrieves raw token
    assert auth.get_token() == raw_token

    # 3. Verify a separate fresh auth manager instance reads and decrypts it
    auth_fresh = DeviceAuthManager(cfg, queue)
    assert auth_fresh.get_token() == raw_token
    assert auth_fresh.get_authenticated_employee_id() == 108


def test_9_password_never_written_to_disk_or_db(temp_dir):
    """9. Password is never persisted in config.json, SQLite, or log files."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-09",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    sentinel_password = "ULTRA_CONFIDENTIAL_PASSWORD_NEVER_PERSIST_998877"

    mock_resp = {
        "status": "APPROVED",
        "auth_token": "vdt_token_for_leak_check",
        "device_id": cfg.device_uuid,
        "employee_id": 109,
        "employee_name": "Leak Check Employee",
    }

    with patch.object(api, "_make_request", return_value=(201, mock_resp)):
        ok, resp = api.register_device(device_info, username="test_leak", password=sentinel_password)
        assert ok is True
        auth.register_and_bind(
            token=resp["auth_token"],
            employee_id=resp["employee_id"],
            employee_name=resp["employee_name"],
        )

    # Search all files in temp directory
    for path in temp_dir.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert sentinel_password.encode("utf-8") not in content, f"Password leaked in file: {path}"

    # Also assert TrackerConfig has no password attribute
    assert not hasattr(cfg, "password")


def test_10_failed_registration_leaves_tracker_unbound(temp_dir):
    """10. Failed registration leaves tracker unbound with zero residual authentication state."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    device_info = DeviceInfo(
        device_uuid=cfg.device_uuid,
        machine_name="WORKSTATION-10",
        os_version="Windows 11 Pro",
        client_version="1.0.0",
    )

    with patch.object(api, "_make_request", return_value=(401, {"error": "INVALID_CREDENTIALS"})):
        ok, resp = api.register_device(device_info, username="fail_user", password="bad")
        assert ok is False

    assert auth.is_authenticated() is False
    assert auth.get_authenticated_employee_id() is None
    assert auth.get_token() is None
    assert queue.get_meta("auth_state") is None
    assert cfg.auth_token is None
    assert cfg.registered_employee_id is None


def test_11_existing_bound_employee_cannot_be_silently_replaced(temp_dir):
    """11. An existing bound employee cannot silently be replaced without explicit unpairing."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)

    # Initial employee 201 (Aarav) binds successfully
    ok1 = auth.register_and_bind(
        token="vdt_token_aarav_111",
        employee_id=201,
        employee_name="Aarav Sharma",
    )
    assert ok1 is True
    assert auth.get_authenticated_employee_id() == 201

    # Employee 202 (Suresh) attempts registration on the same bound workstation
    ok2 = auth.register_and_bind(
        token="vdt_token_suresh_222",
        employee_id=202,
        employee_name="Suresh Verma",
    )
    # Must fail closed: cannot silently replace employee 201
    assert ok2 is False
    # Original binding must remain intact
    assert auth.get_authenticated_employee_id() == 201
    assert auth.get_token() == "vdt_token_aarav_111"

    # Explicit unpairing required before another employee can bind
    auth.clear_credentials()
    assert auth.is_authenticated() is False
    assert auth.get_authenticated_employee_id() is None

    # Now employee 202 can bind cleanly
    ok3 = auth.register_and_bind(
        token="vdt_token_suresh_222",
        employee_id=202,
        employee_name="Suresh Verma",
    )
    assert ok3 is True
    assert auth.get_authenticated_employee_id() == 202
    assert auth.get_token() == "vdt_token_suresh_222"


def test_12_first_run_ui_switching_and_validation(temp_dir):
    """12. FirstRunWizard UI validates inputs, enforces HTTPS, and switches auth methods."""
    cfg = TrackerConfig.load(custom_data_dir=temp_dir)
    queue = ActivityQueue(cfg.db_path)
    auth = DeviceAuthManager(cfg, queue)
    api = VaaniApiClient("http://testserver")

    root = tk.Tk()
    root.withdraw()
    try:
        wizard = FirstRunWizard(
            config=cfg,
            auth_manager=auth,
            api_client=api,
            root=root,
        )

        # 1. HTTPS enforcement for production domain in Step 2
        wizard.show_step(2)
        wizard.entry_backend_url.delete(0, tk.END)
        wizard.entry_backend_url.insert(0, "http://vaaniapp.co.in")
        wizard._on_connection_next()
        assert wizard.config.backend_url == "https://vaaniapp.co.in"
        assert wizard.api_client.backend_url == "https://vaaniapp.co.in"
        assert wizard._current_step == 3

        # 2. Initial Step 3 method is PAIRING_CODE
        assert wizard.auth_method == "PAIRING_CODE"
        assert wizard.btn_pair.cget("text") == "Pair Workstation 🔗"

        # 3. Empty pairing code validation
        wizard.entry_code.delete(0, tk.END)
        wizard._on_pair_click()
        assert "pairing code" in wizard.lbl_pair_error.cget("text").lower()

        # 4. Switch to CREDENTIALS method
        wizard._set_auth_method("CREDENTIALS")
        assert wizard.auth_method == "CREDENTIALS"
        assert wizard.lbl_pair_error.cget("text") == ""
        assert wizard.btn_pair.cget("text") == "Sign In & Bind Workstation 🔗"
        assert wizard.lbl_user.cget("text") == "CRM Username"
        assert "not your email" in wizard.lbl_creds_hint.cget("text").lower()

        # 5. Empty username validation
        wizard.entry_username.delete(0, tk.END)
        wizard.entry_password.delete(0, tk.END)
        wizard._on_pair_click()
        assert "crm username" in wizard.lbl_pair_error.cget("text").lower()

        # 6. Empty password validation
        wizard.entry_username.insert(0, "test_employee")
        wizard._on_pair_click()
        assert "password" in wizard.lbl_pair_error.cget("text").lower()

        # 7. Switch back to PAIRING_CODE
        wizard._set_auth_method("PAIRING_CODE")
        assert wizard.auth_method == "PAIRING_CODE"
        assert wizard.lbl_pair_error.cget("text") == ""
        assert wizard.btn_pair.cget("text") == "Pair Workstation 🔗"

    finally:
        root.destroy()
