"""End-to-End integration test for Desktop Tracker with local Mock Vaani Backend."""

import json
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_server import MockVaaniHandler, SERVER_STATE
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, ShiftStatus
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.models import DeviceInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier
from vaani_tracker.windows.apps import WindowsProcessIdentifier
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.aggregator import ActivityAggregator
from vaani_tracker.core.syncer import BackgroundSyncer


TEST_PORT = 8189


class TestEndToEndFlow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Start mock server in background thread
        cls.server = HTTPServer(("127.0.0.1", TEST_PORT), MockVaaniHandler)
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
            SERVER_STATE.session_id = 500
            SERVER_STATE.shift_status = "ACTIVE"
            SERVER_STATE.ingested_buckets.clear()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.backend_url = f"http://127.0.0.1:{TEST_PORT}"

        self.config = TrackerConfig(
            backend_url=self.backend_url,
            data_dir=self.data_dir,
            device_uuid="dev-e2e-001",
            idle_threshold_seconds=60,
            sync_interval_seconds=1,
            heartbeat_interval_seconds=1,
        )
        self.queue = ActivityQueue(self.config.db_path)
        self.auth = DeviceAuthManager(self.config, self.queue)
        self.api_client = VaaniApiClient(self.backend_url)

        self.mock_idle_sec = 0.0
        self.mock_proc = "code.exe"
        self.idle_detector = WindowsIdleDetector(idle_provider=lambda: self.mock_idle_sec)
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.proc_id = WindowsProcessIdentifier(provider=lambda: self.mock_proc)

        self.sm = TrackingStateMachine(
            self.idle_detector,
            self.session_notifier,
            self.power_notifier,
            idle_threshold_seconds=60,
        )

        self.aggregator = ActivityAggregator(
            state_machine=self.sm,
            process_identifier=self.proc_id,
            queue=self.queue,
            device_uuid=self.config.device_uuid,
            bucket_duration_seconds=60,
            track_apps=True,
        )

        self.syncer = BackgroundSyncer(
            config=self.config,
            api_client=self.api_client,
            queue=self.queue,
            auth_manager=self.auth,
            state_machine=self.sm,
        )

    def tearDown(self):
        self.syncer.stop()
        self.temp_dir.cleanup()

    def test_complete_lifecycle(self):
        # 1. Device Registration
        dev_info = DeviceInfo(
            device_uuid=self.config.device_uuid,
            machine_name="DEV-WIN11-PILOT",
            os_version="Windows 11 Pro",
            client_version="1.0.0",
        )
        success, reg_resp = self.api_client.register_device(dev_info, pairing_code="VN-999")
        self.assertTrue(success)
        token = reg_resp["auth_token"]
        self.auth.set_token(token)
        self.auth.bind_employee(reg_resp["employee_id"], reg_resp["employee_name"])

        # 2. Heartbeat fetches active shift
        heartbeat_ok = self.syncer.heartbeat_now()
        self.assertTrue(heartbeat_ok)
        self.assertEqual(self.sm.current_state, TrackerState.ACTIVE)
        self.assertEqual(self.sm.shift_info.session_id, 500)

        # 3. Simulate 60 seconds of active work on code.exe
        for _ in range(60):
            self.aggregator.tick(1.0)

        # Queue should now have 1 pending bucket
        self.assertEqual(self.queue.get_pending_count(), 1)

        # 4. Sync bucket to backend
        sync_res = self.syncer.sync_pending_now()
        self.assertTrue(sync_res.success)
        self.assertEqual(self.queue.get_pending_count(), 0)

        # 5. Verify server state
        with SERVER_STATE.lock:
            self.assertEqual(len(SERVER_STATE.ingested_buckets), 1)
            ingested = SERVER_STATE.ingested_buckets[0]
            self.assertEqual(ingested["session_id"], 500)
            self.assertEqual(ingested["active_seconds"], 60)
            self.assertEqual(ingested["app_usages"][0]["process_name"], "code.exe")
            self.assertEqual(ingested["app_usages"][0]["duration_seconds"], 60)

        # 6. Simulate Shift Break from server
        req = urllib.request.Request(
            f"{self.backend_url}/mock/shift/break",
            data=json.dumps({"reason": "Lunch Break"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.getcode(), 200)

        self.syncer.heartbeat_now()
        self.assertEqual(self.sm.current_state, TrackerState.PAUSED)
        self.assertEqual(self.sm.shift_info.current_break_name, "Lunch Break")

        # 7. Simulate Shift End from server
        req = urllib.request.Request(
            f"{self.backend_url}/mock/shift/end",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.getcode(), 200)

        self.syncer.heartbeat_now()
        self.assertEqual(self.sm.current_state, TrackerState.STOPPED)


if __name__ == "__main__":
    unittest.main()

