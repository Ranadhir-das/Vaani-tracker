"""Phase 9 Integration Verification against Mock Vaani Backend."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_server import VaaniMockThreadingServer, MockVaaniHandler, SERVER_STATE
from vaani_tracker.app import VaaniTrackerApp
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, ShiftStatus, SyncStatus
from vaani_tracker.models import ActivityBucket, DeviceInfo, ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue

TEST_INTEGRATION_PORT = 8195


class TestMockServerFullIntegration(unittest.TestCase):
    """Executes the full 10-step integration verification against the local mock backend."""

    @classmethod
    def setUpClass(cls):
        cls.server = VaaniMockThreadingServer(("127.0.0.1", TEST_INTEGRATION_PORT), MockVaaniHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # Reset mock server state
        with SERVER_STATE.lock:
            SERVER_STATE.devices.clear()
            SERVER_STATE.valid_tokens.clear()
            SERVER_STATE.ingested_buckets.clear()
            SERVER_STATE.session_id = 201
            SERVER_STATE.shift_status = "ACTIVE"
            SERVER_STATE.current_break_name = ""

        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.backend_url = f"http://127.0.0.1:{TEST_INTEGRATION_PORT}"

        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="dev-integ-test-999",
            backend_url=self.backend_url,
            sync_interval_seconds=1,
            heartbeat_interval_seconds=1,
            idle_threshold_seconds=60,
        )
        self.app = None
        self.app2 = None

    def tearDown(self):
        if self.app:
            try:
                self.app.stop()
            except Exception:
                pass
            self.app = None
        if self.app2:
            try:
                self.app2.stop()
            except Exception:
                pass
            self.app2 = None
        self.temp_dir.cleanup()

    def test_complete_phase_9_flow(self):
        """Verifies the complete 10-step end-to-end integration checklist."""
        queue = ActivityQueue(self.config.db_path)
        auth = DeviceAuthManager(self.config, queue)
        api_client = VaaniApiClient(self.backend_url)

        # -------------------------------------------------------------
        # Step 1: Device registration
        # -------------------------------------------------------------
        dev_info = DeviceInfo(
            device_uuid=self.config.device_uuid,
            machine_name="TEST-WIN11-INTEG",
            os_version="Windows 11 Pro (10.0.22631)",
            client_version="1.0.0",
        )
        success, reg_resp = api_client.register_device(dev_info, pairing_code="VN-INTEG-01")
        self.assertTrue(success, f"Device registration failed: {reg_resp}")
        token = reg_resp.get("auth_token") or reg_resp.get("token")
        self.assertIsNotNone(token)
        emp_id = reg_resp["employee_id"]
        auth.set_token(token)
        auth.bind_employee(reg_resp["employee_id"], reg_resp["employee_name"])
        self.assertTrue(auth.is_authenticated())

        # -------------------------------------------------------------
        # Step 2: Tracker startup
        # -------------------------------------------------------------
        app = VaaniTrackerApp(config=self.config, headless=True)
        self.app = app
        # Ensure simulated user is active so machine idle does not override test state
        app.idle_detector._custom_provider = lambda: 0.0
        # Start without background tick loop so manual ticks in the test do not race
        started = app.start(start_tick_thread=False)
        self.assertTrue(started, "App start failed")
        self.assertTrue(app._running)

        # Verify initial heartbeat synced active shift
        app.syncer.heartbeat_now()
        self.assertEqual(app.state_machine.current_state, TrackerState.ACTIVE)
        self.assertEqual(app.state_machine.shift_info.session_id, 201)

        # -------------------------------------------------------------
        # Step 3: Activity bucket creation
        # -------------------------------------------------------------
        # Accumulate 60 seconds of active work
        for _ in range(60):
            app.aggregator.tick(1.0)

        # Queue must have 1 bucket created
        self.assertGreaterEqual(queue.get_pending_count(employee_id=emp_id), 1)
        pending = queue.get_pending_buckets(employee_id=emp_id, limit=10)
        bucket1 = pending[0]
        self.assertEqual(bucket1.active_seconds, 60)
        self.assertEqual(bucket1.idle_seconds, 0)
        self.assertEqual(bucket1.session_id, 201)

        # -------------------------------------------------------------
        # Step 4: Automatic synchronization
        # -------------------------------------------------------------
        # Trigger immediate sync worker
        app.syncer.trigger_immediate_sync()
        time.sleep(0.3)
        # Server should have received bucket1
        with SERVER_STATE.lock:
            self.assertEqual(len(SERVER_STATE.ingested_buckets), 1)
            self.assertEqual(SERVER_STATE.ingested_buckets[0]["bucket_id"], bucket1.bucket_id)

        # -------------------------------------------------------------
        # Step 5: Manual synchronization
        # -------------------------------------------------------------
        manual_result = app.syncer.sync_pending_now()
        self.assertTrue(manual_result.success)
        self.assertEqual(queue.get_pending_count(employee_id=emp_id), 0)

        # -------------------------------------------------------------
        # Step 6: Idle transition and recovery
        # -------------------------------------------------------------
        # Simulate idle for 30s
        app.idle_detector._custom_provider = lambda: 75.0
        for _ in range(30):
            app.aggregator.tick(1.0)
        self.assertEqual(app.state_machine.current_state, TrackerState.IDLE)

        # Simulate user resumes active typing for 30s
        app.idle_detector._custom_provider = lambda: 0.0
        for _ in range(30):
            app.aggregator.tick(1.0)
        self.assertEqual(app.state_machine.current_state, TrackerState.ACTIVE)

        # 60s total closed bucket 2 (30s idle, 30s active)
        self.assertGreaterEqual(queue.get_pending_count(employee_id=emp_id), 1)
        bucket2 = queue.get_pending_buckets(employee_id=emp_id, limit=10)[0]
        self.assertEqual(bucket2.idle_seconds, 30)
        self.assertEqual(bucket2.active_seconds, 30)

        # -------------------------------------------------------------
        # Step 7: Workstation lock and unlock
        # -------------------------------------------------------------
        app.session_notifier.trigger_lock()
        self.assertEqual(app.state_machine.current_state, TrackerState.LOCKED)
        for _ in range(15):
            app.aggregator.tick(1.0)

        app.session_notifier.trigger_unlock()
        self.assertEqual(app.state_machine.current_state, TrackerState.ACTIVE)
        for _ in range(15):
            app.aggregator.tick(1.0)

        # -------------------------------------------------------------
        # Step 8: Shutdown and final partial bucket
        # -------------------------------------------------------------
        app.stop()
        self.app = None
        self.assertFalse(app._running)

        # The partial bucket of 30s (15s locked + 15s active) must be saved to SQLite
        pending_after_stop = queue.get_pending_buckets(employee_id=emp_id, limit=10)
        self.assertGreaterEqual(len(pending_after_stop), 2)
        partial_bucket = [b for b in pending_after_stop if b.is_locked][0]
        self.assertEqual(partial_bucket.idle_seconds, 15)
        self.assertEqual(partial_bucket.active_seconds, 15)
        self.assertTrue(partial_bucket.is_locked)

        # -------------------------------------------------------------
        # Step 9: Restart and offline queue recovery
        # -------------------------------------------------------------
        # Start a brand new tracker instance pointing to the same SQLite database
        app2 = VaaniTrackerApp(config=self.config, headless=True)
        self.app2 = app2
        app2.idle_detector._custom_provider = lambda: 0.0
        app2_started = app2.start(start_tick_thread=False)
        self.assertTrue(app2_started)

        # Sync all pending buckets that were queued while offline
        sync_res = app2.syncer.sync_pending_now()
        self.assertTrue(sync_res.success)
        self.assertEqual(queue.get_pending_count(employee_id=emp_id), 0)

        # Server must now have all 3 buckets
        with SERVER_STATE.lock:
            self.assertEqual(len(SERVER_STATE.ingested_buckets), 3)

        # -------------------------------------------------------------
        # Step 10: Duplicate bucket prevention
        # -------------------------------------------------------------
        # Force re-insert bucket1 into queue with PENDING status
        queue.save_bucket(bucket1, employee_id=emp_id)
        self.assertEqual(queue.get_pending_count(employee_id=emp_id), 1)

        # Attempt to sync bucket1 again
        dup_result = app2.syncer.sync_pending_now()
        self.assertTrue(dup_result.success, "409 Conflict must be handled as idempotent success")
        self.assertEqual(queue.get_pending_count(employee_id=emp_id), 0, "Duplicate bucket must be marked synced and cleared")

        # Server bucket count remains 3 (no duplication)
        with SERVER_STATE.lock:
            self.assertEqual(len(SERVER_STATE.ingested_buckets), 3)

        app2.stop()
        self.app2 = None


if __name__ == "__main__":
    unittest.main()

