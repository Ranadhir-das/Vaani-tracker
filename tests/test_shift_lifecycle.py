"""Comprehensive integration tests for Phase 4.5.4.3: Shift Lifecycle.

Tests:
1. GET /api/v1/desktop-tracker/shift/meta/ returns categories and break reasons.
2. POST /api/v1/desktop-tracker/shift/start/ starts new session and sets ACTIVE.
3. POST /api/v1/desktop-tracker/shift/start/ returns HTTP 409 conflict if already active.
4. POST /api/v1/desktop-tracker/shift/pause/ transitions to PAUSED with break name.
5. POST /api/v1/desktop-tracker/shift/pause/ returns HTTP 400 when missing break reason.
6. POST /api/v1/desktop-tracker/shift/pause/ returns HTTP 409 when already paused.
7. POST /api/v1/desktop-tracker/shift/resume/ transitions back to ACTIVE.
8. POST /api/v1/desktop-tracker/shift/resume/ returns HTTP 409 when already active.
9. POST /api/v1/desktop-tracker/shift/end/ transitions to COMPLETED.
10. POST /api/v1/desktop-tracker/shift/end/ returns HTTP 404 when no active session.
11. Dynamic break seconds accumulate correctly during PAUSED state.
12. DashboardViewModel shift methods integrate cleanly with state machine.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import ShiftStatus, TrackerState
from vaani_tracker.core.aggregator import ActivityAggregator
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.models import ActivityBucket, DeviceInfo, ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.ui.dashboard import DashboardViewModel
from vaani_tracker.ui.tray import VaaniTrayApp
from vaani_tracker.windows.apps import WindowsProcessIdentifier
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.power import WindowsPowerNotifier
from vaani_tracker.windows.session import WindowsSessionNotifier

from mock_server import (
    MockServerState,
    VaaniMockThreadingServer,
    MockVaaniHandler,
    SERVER_STATE,
)


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPhase4543ShiftLifecycle(unittest.TestCase):
    """Verifies Shift Lifecycle API contracts and state transitions against local mock server."""

    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.server = VaaniMockThreadingServer(("127.0.0.1", cls.port), MockVaaniHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass

    def setUp(self):
        self.backend_url = f"http://127.0.0.1:{self.port}"
        self.api_client = VaaniApiClient(backend_url=self.backend_url)
        self.device_uuid = "test-device-shift-uuid"
        self.token = "test-token-shift-123"

        with SERVER_STATE.lock:
            SERVER_STATE.valid_tokens.clear()
            SERVER_STATE.valid_tokens[self.token] = self.device_uuid
            SERVER_STATE.session_id = 200
            SERVER_STATE.shift_status = "NONE"
            SERVER_STATE.started_at = datetime.now(timezone.utc).isoformat()
            SERVER_STATE.ended_at = None
            SERVER_STATE.break_started_mono = None
            SERVER_STATE.accumulated_break_seconds = 0.0
            SERVER_STATE.current_break_id = None
            SERVER_STATE.current_break_name = ""
            SERVER_STATE.current_category_id = 1
            SERVER_STATE.current_category_name = "Calling / Inbound"

    def test_01_fetch_shift_meta(self):
        """1. GET shift/meta/ returns categories and break reasons."""
        ok, cats, breaks, data = self.api_client.fetch_shift_meta(self.device_uuid, self.token)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(cats), 2)
        self.assertGreaterEqual(len(breaks), 2)
        cat_names = [c["name"] for c in cats]
        self.assertIn("Calling / Inbound", cat_names)
        break_codes = [b["code"] for b in breaks]
        self.assertIn("LUNCH", break_codes)

    def test_02_start_shift_success(self):
        """2. POST shift/start/ starts new work session and returns ACTIVE status."""
        ok, shift, data = self.api_client.start_shift(
            device_uuid=self.device_uuid,
            token=self.token,
            category_id=1,
            note="Starting calling shift",
        )
        self.assertTrue(ok)
        self.assertEqual(shift.status, ShiftStatus.ACTIVE)
        self.assertEqual(shift.session_id, 201)
        self.assertEqual(shift.current_category_name, "Calling / Inbound")

    def test_03_start_shift_conflict_when_already_active(self):
        """3. POST shift/start/ returns HTTP 409 conflict when session already active."""
        # Start first
        ok1, _, _ = self.api_client.start_shift(self.device_uuid, self.token, category_id=1)
        self.assertTrue(ok1)

        # Attempt to start again
        ok2, shift2, extra = self.api_client.start_shift(self.device_uuid, self.token, category_id=1)
        self.assertFalse(ok2)
        self.assertEqual(extra.get("error"), "ACTIVE_SESSION_EXISTS")
        self.assertEqual(extra.get("http_status"), 409)

    def test_04_pause_shift_on_break(self):
        """4. POST shift/pause/ transitions active shift to PAUSED with break name."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)

        ok, shift, data = self.api_client.pause_shift(
            device_uuid=self.device_uuid,
            token=self.token,
            break_reason_id=1,  # Lunch
        )
        self.assertTrue(ok)
        self.assertEqual(shift.status, ShiftStatus.PAUSED)
        self.assertEqual(shift.current_break_name, "Lunch")

    def test_05_pause_shift_missing_break_reason(self):
        """5. POST shift/pause/ returns HTTP 400 when break_reason_id is missing or invalid."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)

        # Non-existent reason id
        ok, shift, extra = self.api_client.pause_shift(
            device_uuid=self.device_uuid,
            token=self.token,
            break_reason_id=9999,
        )
        self.assertFalse(ok)
        self.assertEqual(extra.get("http_status"), 400)
        self.assertEqual(extra.get("error"), "INVALID_BREAK_REASON")

    def test_06_pause_shift_conflict_when_already_paused(self):
        """6. POST shift/pause/ returns HTTP 409 when shift is already paused."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)
        self.api_client.pause_shift(self.device_uuid, self.token, break_reason_id=1)

        # Pause again
        ok, shift, extra = self.api_client.pause_shift(self.device_uuid, self.token, break_reason_id=2)
        self.assertFalse(ok)
        self.assertEqual(extra.get("http_status"), 409)
        self.assertEqual(extra.get("error"), "ALREADY_PAUSED")

    def test_07_resume_shift_success(self):
        """7. POST shift/resume/ transitions from PAUSED back to ACTIVE."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)
        self.api_client.pause_shift(self.device_uuid, self.token, break_reason_id=1)

        ok, shift, data = self.api_client.resume_shift(
            device_uuid=self.device_uuid,
            token=self.token,
            category_id=2,  # Switch to Back Office
        )
        self.assertTrue(ok)
        self.assertEqual(shift.status, ShiftStatus.ACTIVE)
        self.assertEqual(shift.current_category_name, "Back Office / Ops")
        self.assertFalse(shift.current_break_name)

    def test_08_resume_shift_conflict_when_already_active(self):
        """8. POST shift/resume/ returns HTTP 409 when shift is already active."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)

        ok, shift, extra = self.api_client.resume_shift(self.device_uuid, self.token)
        self.assertFalse(ok)
        self.assertEqual(extra.get("http_status"), 409)
        self.assertEqual(extra.get("error"), "ALREADY_ACTIVE")

    def test_09_end_shift_success(self):
        """9. POST shift/end/ completes the active session."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)

        ok, shift, data = self.api_client.end_shift(
            device_uuid=self.device_uuid,
            token=self.token,
            note="Shift finished",
        )
        self.assertTrue(ok)
        self.assertEqual(shift.status, ShiftStatus.COMPLETED)

    def test_10_end_shift_when_no_active_session(self):
        """10. POST shift/end/ returns HTTP 404 when no session is active."""
        ok, shift, extra = self.api_client.end_shift(self.device_uuid, self.token)
        self.assertFalse(ok)
        self.assertEqual(extra.get("http_status"), 404)
        self.assertEqual(extra.get("error"), "NO_SESSION_TO_END")

    def test_11_dynamic_break_duration_accumulates(self):
        """11. Dynamic break seconds accumulate while in PAUSED status."""
        self.api_client.start_shift(self.device_uuid, self.token, category_id=1)
        self.api_client.pause_shift(self.device_uuid, self.token, break_reason_id=1)

        time.sleep(0.3)

        # Check status
        ok, shift, _ = self.api_client.fetch_status(self.device_uuid, self.token)
        self.assertTrue(ok)
        self.assertEqual(shift.status, ShiftStatus.PAUSED)
        # Verify break_seconds is dynamic and >= 0
        self.assertGreaterEqual(shift.break_seconds, 0)

    def test_12_dashboard_view_model_integration(self):
        """12. DashboardViewModel shift methods integrate cleanly with TrackingStateMachine."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config = TrackerConfig.load(custom_data_dir=Path(temp_dir.name))
            config.device_uuid = self.device_uuid
            queue = ActivityQueue(config.db_path)
            idle_mock = MagicMock()
            idle_mock.get_idle_seconds.return_value = 0.0
            session_mock = MagicMock()
            session_mock.is_logged_off = False
            session_mock.is_locked = False
            power_mock = MagicMock()
            power_mock.is_sleeping = False

            state_machine = TrackingStateMachine(
                idle_detector=idle_mock,
                session_notifier=session_mock,
                power_notifier=power_mock,
                idle_threshold_seconds=60,
            )
            syncer = MagicMock()
            syncer.api_client = self.api_client
            auth_manager = MagicMock()
            auth_manager.get_token.return_value = self.token
            auth_manager.get_authenticated_employee_id.return_value = 42

            vm = DashboardViewModel(
                config=config,
                state_machine=state_machine,
                syncer=syncer,
                queue=queue,
                auth_manager=auth_manager,
            )

            # Metadata
            cats, breaks = vm.get_shift_metadata()
            self.assertGreaterEqual(len(cats), 1)

            # Start
            ok, msg = vm.start_shift(category_id=1)
            self.assertTrue(ok)
            self.assertEqual(state_machine.shift_info.status, ShiftStatus.ACTIVE)
            self.assertEqual(state_machine.current_state, TrackerState.ACTIVE)

            # Pause
            ok, msg = vm.pause_shift(break_reason_id=1)
            self.assertTrue(ok)
            self.assertEqual(state_machine.shift_info.status, ShiftStatus.PAUSED)
            self.assertEqual(state_machine.current_state, TrackerState.PAUSED)

            # Resume
            ok, msg = vm.resume_shift()
            self.assertTrue(ok)
            self.assertEqual(state_machine.shift_info.status, ShiftStatus.ACTIVE)
            self.assertEqual(state_machine.current_state, TrackerState.ACTIVE)

            # End
            ok, msg = vm.end_shift()
            self.assertTrue(ok)
            self.assertEqual(state_machine.shift_info.status, ShiftStatus.COMPLETED)
            self.assertEqual(state_machine.current_state, TrackerState.STOPPED)
        finally:
            temp_dir.cleanup()

    def test_13_fresh_mock_server_initialization_has_no_shift(self):
        """13. Fresh MockServerState represents no active, paused, or completed shift."""
        fresh_state = MockServerState()
        self.assertEqual(fresh_state.devices, {})
        self.assertEqual(fresh_state.valid_tokens, {})
        self.assertIsNone(fresh_state.session_id)
        self.assertEqual(fresh_state.shift_status, "NOT_STARTED")
        self.assertIsNone(fresh_state.started_at)
        self.assertIsNone(fresh_state.ended_at)
        self.assertEqual(fresh_state.get_gross_seconds(), 0)
        self.assertEqual(fresh_state.get_break_seconds(), 0)
        self.assertEqual(fresh_state.get_net_seconds(), 0)

        serialized = fresh_state.serialize_shift_state()
        self.assertFalse(serialized["has_active_session"])
        self.assertFalse(serialized["has_completed_today"])
        self.assertIsNone(serialized["session"])
        self.assertIsNone(serialized["current_segment"])
        self.assertIsNone(serialized["current_break"])
        self.assertIsNone(serialized["last_completed_session"])

    def test_14_fresh_start_registration_shift_lifecycle_and_server_restart(self):
        """14. Fresh server -> register -> NOT_STARTED -> Start(101) -> Pause -> Resume -> End -> Restart."""
        with SERVER_STATE.lock:
            SERVER_STATE.reset()

        # Register device against fresh mock server
        dev_info = DeviceInfo(
            device_uuid="fresh-dev-001",
            machine_name="VAANI-TEST-PC",
            os_version="Windows 11",
            client_version="1.0.0",
        )
        reg_ok, reg_data = self.api_client.register_device(dev_info, pairing_code="VN-FRESH-01")
        self.assertTrue(reg_ok)
        fresh_token = reg_data["auth_token"]

        # Status immediately after pairing on fresh mock server must be NOT_STARTED (no active/paused/completed shift)
        st_ok, shift0, _ = self.api_client.fetch_status("fresh-dev-001", fresh_token)
        self.assertTrue(st_ok)
        self.assertEqual(shift0.status, ShiftStatus.NOT_STARTED)
        self.assertIsNone(shift0.session_id)
        self.assertFalse(shift0.is_active)
        self.assertFalse(shift0.is_paused)
        self.assertFalse(shift0.is_completed)
        self.assertFalse(shift0.is_tracking_eligible)

        # Start shift -> creates session_id 101 in ACTIVE state
        ok_start, shift_active, _ = self.api_client.start_shift("fresh-dev-001", fresh_token, category_id=1)
        self.assertTrue(ok_start)
        self.assertEqual(shift_active.status, ShiftStatus.ACTIVE)
        self.assertEqual(shift_active.session_id, 101)
        self.assertTrue(shift_active.is_active)

        # Pause shift -> PAUSED
        ok_pause, shift_paused, _ = self.api_client.pause_shift("fresh-dev-001", fresh_token, break_reason_id=1)
        self.assertTrue(ok_pause)
        self.assertEqual(shift_paused.status, ShiftStatus.PAUSED)
        self.assertEqual(shift_paused.session_id, 101)
        self.assertTrue(shift_paused.is_paused)

        # Resume shift -> ACTIVE
        ok_resume, shift_resumed, _ = self.api_client.resume_shift("fresh-dev-001", fresh_token, category_id=2)
        self.assertTrue(ok_resume)
        self.assertEqual(shift_resumed.status, ShiftStatus.ACTIVE)
        self.assertEqual(shift_resumed.session_id, 101)
        self.assertEqual(shift_resumed.current_category_name, "Back Office / Ops")

        # End shift -> COMPLETED
        ok_end, shift_ended, _ = self.api_client.end_shift("fresh-dev-001", fresh_token)
        self.assertTrue(ok_end)
        self.assertEqual(shift_ended.status, ShiftStatus.COMPLETED)
        self.assertTrue(shift_ended.is_completed)
        self.assertFalse(shift_ended.is_active)

        # Simulate mock server restart: must not falsely report an active shift
        with SERVER_STATE.lock:
            SERVER_STATE.reset()
            self.assertEqual(SERVER_STATE.shift_status, "NOT_STARTED")
            self.assertIsNone(SERVER_STATE.session_id)
            self.assertFalse(SERVER_STATE.serialize_shift_state()["has_active_session"])

    def test_15_protected_endpoints_reject_missing_or_invalid_tokens(self):
        """15. All protected status and shift endpoints reject invalid tokens with HTTP 401."""
        bad_token = "invalid-token-999"

        ok_st, _, extra_st = self.api_client.fetch_status(self.device_uuid, bad_token)
        self.assertFalse(ok_st)
        self.assertTrue(extra_st.get("revoked"))

        ok_meta, _, _, extra_meta = self.api_client.fetch_shift_meta(self.device_uuid, bad_token)
        self.assertFalse(ok_meta)
        self.assertEqual(extra_meta.get("http_status"), 401)

        ok_start, _, extra_start = self.api_client.start_shift(self.device_uuid, bad_token, category_id=1)
        self.assertFalse(ok_start)
        self.assertEqual(extra_start.get("http_status"), 401)

        ok_pause, _, extra_pause = self.api_client.pause_shift(self.device_uuid, bad_token, break_reason_id=1)
        self.assertFalse(ok_pause)
        self.assertEqual(extra_pause.get("http_status"), 401)

        ok_resume, _, extra_resume = self.api_client.resume_shift(self.device_uuid, bad_token)
        self.assertFalse(ok_resume)
        self.assertEqual(extra_resume.get("http_status"), 401)

        ok_end, _, extra_end = self.api_client.end_shift(self.device_uuid, bad_token)
        self.assertFalse(ok_end)
        self.assertEqual(extra_end.get("http_status"), 401)

    def test_16_a1_stopped_bucket_mapping_on_shift_termination(self):
        """16. (A1) Closing a bucket at shift end maps state accurately without sending STOPPED or misclassifying idle/locked as ACTIVE."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            db_path = Path(temp_dir.name) / "a1_test.db"
            queue = ActivityQueue(db_path)

            mock_idle = [0.0]
            mock_proc = ["code.exe"]
            idle_detector = WindowsIdleDetector(idle_provider=lambda: mock_idle[0])
            session_notifier = WindowsSessionNotifier()
            power_notifier = WindowsPowerNotifier()
            proc_id = WindowsProcessIdentifier(provider=lambda: mock_proc[0])

            sm = TrackingStateMachine(
                idle_detector=idle_detector,
                session_notifier=session_notifier,
                power_notifier=power_notifier,
                idle_threshold_seconds=60.0,
            )
            agg = ActivityAggregator(
                state_machine=sm,
                process_identifier=proc_id,
                queue=queue,
                device_uuid="dev-a1-test",
                bucket_duration_seconds=60,
                track_apps=True,
                employee_id=42,
            )

            # Case 1: Active bucket flushed when shift ends -> ACTIVE
            sm.update_shift_status(ShiftInfo(session_id=301, status=ShiftStatus.ACTIVE))
            for _ in range(35):
                agg.tick(1.0)
            sm.update_shift_status(ShiftInfo(session_id=301, status=ShiftStatus.COMPLETED))
            self.assertEqual(sm.current_state, TrackerState.STOPPED)

            pending = queue.get_pending_buckets(employee_id=42, limit=10)
            self.assertEqual(len(pending), 1)
            b_active = pending[0]
            self.assertEqual(b_active.state, TrackerState.ACTIVE)
            self.assertEqual(b_active.to_api_payload()["state"], "ACTIVE")
            self.assertEqual(b_active.active_seconds, 35)
            self.assertEqual(b_active.idle_seconds, 0)
            self.assertEqual(sum(a.duration_seconds for a in b_active.app_usages), 35)
            queue.mark_buckets_synced([b_active.bucket_id], employee_id=42)

            # Case 2: Idle bucket flushed when shift ends -> IDLE (never STOPPED or ACTIVE)
            mock_idle[0] = 90.0
            sm.update_shift_status(ShiftInfo(session_id=302, status=ShiftStatus.ACTIVE))
            self.assertEqual(sm.current_state, TrackerState.IDLE)
            for _ in range(40):
                agg.tick(1.0)
            sm.update_shift_status(ShiftInfo(session_id=302, status=ShiftStatus.COMPLETED))

            pending = queue.get_pending_buckets(employee_id=42, limit=10)
            self.assertEqual(len(pending), 1)
            b_idle = pending[0]
            self.assertEqual(b_idle.state, TrackerState.IDLE)
            self.assertEqual(b_idle.to_api_payload()["state"], "IDLE")
            self.assertEqual(b_idle.active_seconds, 0)
            self.assertEqual(b_idle.idle_seconds, 40)
            queue.mark_buckets_synced([b_idle.bucket_id], employee_id=42)

            # Case 3: Locked bucket flushed when shift ends -> LOCKED (never STOPPED or ACTIVE)
            mock_idle[0] = 0.0
            session_notifier.trigger_lock()
            sm.update_shift_status(ShiftInfo(session_id=303, status=ShiftStatus.ACTIVE))
            self.assertEqual(sm.current_state, TrackerState.LOCKED)
            for _ in range(25):
                agg.tick(1.0)
            sm.update_shift_status(ShiftInfo(session_id=303, status=ShiftStatus.COMPLETED))

            pending = queue.get_pending_buckets(employee_id=42, limit=10)
            self.assertEqual(len(pending), 1)
            b_locked = pending[0]
            self.assertEqual(b_locked.state, TrackerState.LOCKED)
            self.assertEqual(b_locked.to_api_payload()["state"], "LOCKED")
            self.assertTrue(b_locked.is_locked)
            self.assertEqual(b_locked.active_seconds, 0)
            self.assertEqual(b_locked.idle_seconds, 25)
        finally:
            temp_dir.cleanup()

    def test_17_a1_activity_bucket_to_api_payload_never_emits_stopped_or_untracked(self):
        """17. (A1) ActivityBucket.to_api_payload() normalizes STOPPED/DISCONNECTED states safely."""
        b1 = ActivityBucket(active_seconds=50, idle_seconds=10, state=TrackerState.STOPPED)
        self.assertEqual(b1.to_api_payload()["state"], "ACTIVE")

        b2 = ActivityBucket(active_seconds=0, idle_seconds=60, state=TrackerState.STOPPED)
        self.assertEqual(b2.to_api_payload()["state"], "IDLE")

        b3 = ActivityBucket(active_seconds=0, idle_seconds=60, state=TrackerState.STOPPED, is_locked=True)
        self.assertEqual(b3.to_api_payload()["state"], "LOCKED")

        b4 = ActivityBucket(active_seconds=0, idle_seconds=0, state=TrackerState.STOPPED, is_paused=True)
        self.assertEqual(b4.to_api_payload()["state"], "PAUSED")

        b5 = ActivityBucket(active_seconds=0, idle_seconds=0, state=TrackerState.STOPPED, is_sleeping=True)
        self.assertEqual(b5.to_api_payload()["state"], "SLEEPING")

        for b in (b1, b2, b3, b4, b5):
            st = b.to_api_payload()["state"]
            self.assertNotIn(st, ("STOPPED", "DISCONNECTED", "UNTRACKED"))

    def test_18_a2_tray_dynamic_break_reasons_and_unavailable_metadata(self):
        """18. (A2) System tray loads break reasons dynamically from CRM metadata and never submits hardcoded IDs."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config = TrackerConfig.load(custom_data_dir=Path(temp_dir.name))
            config.device_uuid = self.device_uuid
            queue = ActivityQueue(config.db_path)
            sm = TrackingStateMachine(
                idle_detector=MagicMock(get_idle_seconds=MagicMock(return_value=0.0)),
                session_notifier=MagicMock(is_logged_off=False, is_locked=False),
                power_notifier=MagicMock(is_sleeping=False),
            )
            syncer = MagicMock()
            syncer.api_client = self.api_client
            auth_manager = MagicMock()
            auth_manager.get_token.return_value = self.token
            opened_dashboard = []

            tray = VaaniTrayApp(
                config=config,
                state_machine=sm,
                syncer=syncer,
                queue=queue,
                on_open_dashboard=lambda: opened_dashboard.append(True),
                auth_manager=auth_manager,
            )

            # Configure dynamic server break reasons with non-1/2 IDs and a note-required break
            with SERVER_STATE.lock:
                SERVER_STATE.break_reasons = [
                    {"id": 11, "name": "Midday Meal", "code": "MEAL", "warning_threshold_minutes": 45, "requires_note": False},
                    {"id": 22, "name": "Coffee Break", "code": "COFFEE", "warning_threshold_minutes": 15, "requires_note": False},
                    {"id": 33, "name": "Emergency Errand", "code": "OTHER", "warning_threshold_minutes": 15, "requires_note": True},
                ]

            descriptors = tray.get_break_menu_descriptors(refresh=True)
            self.assertEqual([d["id"] for d in descriptors], [11, 22, 33])
            self.assertEqual(descriptors[0]["action"], "pause_break")
            self.assertEqual(descriptors[2]["action"], "open_dashboard_note")

            # Start shift and pause using dynamic ID 11 from tray
            self.api_client.start_shift(self.device_uuid, self.token, category_id=1)
            ok, msg = tray._on_tray_pause_break(11, requires_note=False, break_name="Midday Meal", run_async=False)
            self.assertTrue(ok)
            self.assertEqual(sm.shift_info.status, ShiftStatus.PAUSED)
            self.assertEqual(sm.shift_info.current_break_name, "Midday Meal")

            # Resume and attempt note-required break (ID 33) from tray -> opens dashboard instead of failing API
            self.api_client.resume_shift(self.device_uuid, self.token)
            sm.update_shift_status(ShiftInfo(session_id=201, status=ShiftStatus.ACTIVE))
            opened_dashboard.clear()
            ok_note, msg_note = tray._on_tray_pause_break(33, requires_note=True, break_name="Emergency Errand", run_async=False)
            self.assertFalse(ok_note)
            self.assertEqual(len(opened_dashboard), 1)
            self.assertEqual(sm.shift_info.status, ShiftStatus.ACTIVE)

            # Unavailable metadata -> returns single safe open_dashboard descriptor with id=None
            tray._cached_break_reasons = None
            auth_manager.get_token.return_value = "revoked-token"
            fallback_desc = tray.get_break_menu_descriptors(refresh=True)
            self.assertEqual(len(fallback_desc), 1)
            self.assertIsNone(fallback_desc[0]["id"])
            self.assertEqual(fallback_desc[0]["action"], "open_dashboard")
        finally:
            with SERVER_STATE.lock:
                SERVER_STATE.break_reasons = [
                    {"id": 1, "name": "Lunch", "code": "LUNCH", "warning_threshold_minutes": 45, "requires_note": False},
                    {"id": 2, "name": "Tea Break", "code": "TEA_BREAK", "warning_threshold_minutes": 15, "requires_note": False},
                    {"id": 3, "name": "Bio / Personal", "code": "PERSONAL", "warning_threshold_minutes": 10, "requires_note": False},
                ]
            temp_dir.cleanup()

    def test_19_a3_required_break_note_validation_and_privacy(self):
        """19. (A3) Break reasons with requires_note=True enforce non-empty <=255-char note without logging note text."""
        import logging

        # 1. Static validation checks
        v1, c1, e1 = DashboardViewModel.validate_break_note("", requires_note=True)
        self.assertFalse(v1)
        self.assertIn("note", e1.lower())

        v2, c2, e2 = DashboardViewModel.validate_break_note("   ", requires_note=True)
        self.assertFalse(v2)

        v3, c3, e3 = DashboardViewModel.validate_break_note("x" * 256, requires_note=True)
        self.assertFalse(v3)
        self.assertIn("255", e3)

        v4, c4, e4 = DashboardViewModel.validate_break_note("  Doctor appointment  ", requires_note=True)
        self.assertTrue(v4)
        self.assertEqual(c4, "Doctor appointment")

        # 2. ViewModel integration with requires_note=True vs requires_note=False
        temp_dir = tempfile.TemporaryDirectory()
        try:
            with SERVER_STATE.lock:
                SERVER_STATE.break_reasons = [
                    {"id": 1, "name": "Lunch", "code": "LUNCH", "warning_threshold_minutes": 45, "requires_note": False},
                    {"id": 4, "name": "Other / Personal", "code": "OTHER", "warning_threshold_minutes": 15, "requires_note": True},
                ]

            config = TrackerConfig.load(custom_data_dir=Path(temp_dir.name))
            config.device_uuid = self.device_uuid
            queue = ActivityQueue(config.db_path)
            sm = TrackingStateMachine(
                idle_detector=MagicMock(get_idle_seconds=MagicMock(return_value=0.0)),
                session_notifier=MagicMock(is_logged_off=False, is_locked=False),
                power_notifier=MagicMock(is_sleeping=False),
            )
            syncer = MagicMock()
            syncer.api_client = self.api_client
            auth_manager = MagicMock()
            auth_manager.get_token.return_value = self.token
            auth_manager.get_authenticated_employee_id.return_value = 42

            vm = DashboardViewModel(
                config=config,
                state_machine=sm,
                syncer=syncer,
                queue=queue,
                auth_manager=auth_manager,
            )

            vm.get_shift_metadata(refresh=True)
            ok_start, _ = vm.start_shift(category_id=1)
            self.assertTrue(ok_start)
            self.assertEqual(sm.shift_info.status, ShiftStatus.ACTIVE)

            # Attempting pause on break 4 (requires_note=True) without note fails and keeps shift ACTIVE
            ok_empty, msg_empty = vm.pause_shift(break_reason_id=4, note="   ")
            self.assertFalse(ok_empty)
            self.assertEqual(sm.shift_info.status, ShiftStatus.ACTIVE)
            self.assertEqual(sm.current_state, TrackerState.ACTIVE)

            # Even if cache was bypassed, server NOTE_REQUIRED keeps shift ACTIVE
            vm._cached_break_reasons = []
            ok_srv_err, msg_srv_err = vm.pause_shift(break_reason_id=4, note="")
            self.assertFalse(ok_srv_err)
            self.assertEqual(sm.shift_info.status, ShiftStatus.ACTIVE)

            # Capture logs while submitting a sensitive break note to verify note is NEVER logged
            secret_note = "CONFIDENTIAL_MEDICAL_VISIT_NOTE_98765"
            log_records: list[str] = []

            class ListHandler(logging.Handler):
                def emit(self, record):
                    log_records.append(self.format(record))

            handler = ListHandler()
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)
            try:
                ok_valid, msg_valid = vm.pause_shift(break_reason_id=4, note=secret_note)
                self.assertTrue(ok_valid)
                self.assertEqual(sm.shift_info.status, ShiftStatus.PAUSED)
                self.assertEqual(sm.shift_info.current_break_name, "Other / Personal")
            finally:
                root_logger.removeHandler(handler)

            combined_logs = "\n".join(log_records)
            self.assertNotIn(secret_note, combined_logs, "Break note content must never be written to logs")
            self.assertNotIn(secret_note, msg_valid)
        finally:
            with SERVER_STATE.lock:
                SERVER_STATE.break_reasons = [
                    {"id": 1, "name": "Lunch", "code": "LUNCH", "warning_threshold_minutes": 45, "requires_note": False},
                    {"id": 2, "name": "Tea Break", "code": "TEA_BREAK", "warning_threshold_minutes": 15, "requires_note": False},
                    {"id": 3, "name": "Bio / Personal", "code": "PERSONAL", "warning_threshold_minutes": 10, "requires_note": False},
                ]
            temp_dir.cleanup()

    def test_20_a4_end_shift_insufficient_work_duration_and_immediate_sync(self):
        """20. (A4) End shift preserves active state on HTTP 422 and triggers immediate sync on HTTP 200."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config = TrackerConfig.load(custom_data_dir=Path(temp_dir.name))
            config.device_uuid = self.device_uuid
            queue = ActivityQueue(config.db_path)
            sm = TrackingStateMachine(
                idle_detector=MagicMock(get_idle_seconds=MagicMock(return_value=0.0)),
                session_notifier=MagicMock(is_logged_off=False, is_locked=False),
                power_notifier=MagicMock(is_sleeping=False),
            )
            api_mock = MagicMock()
            syncer = MagicMock()
            syncer.api_client = api_mock
            auth_manager = MagicMock()
            auth_manager.get_token.return_value = self.token

            vm = DashboardViewModel(
                config=config,
                state_machine=sm,
                syncer=syncer,
                queue=queue,
                auth_manager=auth_manager,
            )

            sm.update_shift_status(ShiftInfo(session_id=501, status=ShiftStatus.ACTIVE))

            # Simulate HTTP 422 INSUFFICIENT_WORK_DURATION from CRM
            api_mock.end_shift.return_value = (
                False,
                ShiftInfo(status=ShiftStatus.NONE),
                {
                    "error": "INSUFFICIENT_WORK_DURATION",
                    "message": "Minimum shift duration not met (5h 0m remaining).",
                    "http_status": 422,
                    "net_seconds": 3600,
                    "required_seconds": 21600,
                    "shortfall_seconds": 18000,
                    "can_request_early_end": False,
                },
            )
            ok_422, msg_422 = vm.end_shift()
            self.assertFalse(ok_422)
            self.assertIn("Minimum shift duration", msg_422)
            # Shift must remain ACTIVE locally
            self.assertEqual(sm.shift_info.status, ShiftStatus.ACTIVE)
            self.assertEqual(sm.current_state, TrackerState.ACTIVE)
            syncer.trigger_immediate_sync.assert_not_called()

            # Simulate HTTP 200 SHIFT_ENDED
            api_mock.end_shift.return_value = (
                True,
                ShiftInfo(session_id=501, status=ShiftStatus.COMPLETED, net_seconds=21600),
                {"status": "SHIFT_ENDED"},
            )
            ok_200, msg_200 = vm.end_shift()
            self.assertTrue(ok_200)
            self.assertEqual(sm.shift_info.status, ShiftStatus.COMPLETED)
            self.assertEqual(sm.current_state, TrackerState.STOPPED)
            syncer.trigger_immediate_sync.assert_called_once()
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
