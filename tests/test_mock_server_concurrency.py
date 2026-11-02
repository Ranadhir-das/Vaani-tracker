"""Tests for ThreadingHTTPServer concurrency and thread-safe lock management in mock_server."""

import json
import socket
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_server import VaaniMockThreadingServer, MockVaaniHandler, SERVER_STATE


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestMockServerConcurrency(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.server = VaaniMockThreadingServer(("127.0.0.1", cls.port), MockVaaniHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        with SERVER_STATE.lock:
            SERVER_STATE.devices.clear()
            SERVER_STATE.valid_tokens.clear()
            SERVER_STATE.session_id = 999
            SERVER_STATE.shift_status = "ACTIVE"
            SERVER_STATE.ingested_buckets.clear()

    def test_concurrent_status_sync_and_inspect_requests(self):
        # 1. Register a test device first
        reg_payload = json.dumps({
            "device_uuid": "concurrent-dev-uuid-1",
            "machine_name": "CONCURRENT-PC",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/desktop-tracker/register/",
            data=reg_payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 201)
            reg_data = json.loads(resp.read().decode("utf-8"))
            token = reg_data["auth_token"]

        self.assertTrue(token)

        # 2. Fire 30 concurrent requests (10 status, 10 sync, 10 inspect) across 10 worker threads
        def send_status(idx):
            r = urllib.request.Request(
                f"{self.base_url}/api/v1/desktop-tracker/status/",
                headers={"X-Device-Token": token},
            )
            with urllib.request.urlopen(r, timeout=5) as res:
                return res.status, json.loads(res.read().decode("utf-8"))

        def send_sync(idx):
            payload = json.dumps({
                "buckets": [
                    {
                        "bucket_id": f"bucket-concurrent-{idx}",
                        "active_seconds": 60,
                        "idle_seconds": 0,
                        "state": "ACTIVE",
                    }
                ]
            }).encode("utf-8")
            r = urllib.request.Request(
                f"{self.base_url}/api/v1/desktop-tracker/sync/",
                data=payload,
                headers={"Content-Type": "application/json", "X-Device-Token": token},
            )
            with urllib.request.urlopen(r, timeout=5) as res:
                return res.status, json.loads(res.read().decode("utf-8"))

        def send_inspect(idx):
            r = urllib.request.Request(f"{self.base_url}/mock/inspect")
            with urllib.request.urlopen(r, timeout=5) as res:
                return res.status, json.loads(res.read().decode("utf-8"))

        tasks = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            for i in range(10):
                tasks.append(executor.submit(send_status, i))
                tasks.append(executor.submit(send_sync, i))
                tasks.append(executor.submit(send_inspect, i))

        results = [t.result(timeout=5) for t in tasks]
        self.assertEqual(len(results), 30)
        for code, body in results:
            self.assertEqual(code, 200)

        # Verify all 10 sync buckets were safely recorded without lock corruption
        with SERVER_STATE.lock:
            self.assertEqual(len(SERVER_STATE.ingested_buckets), 10)
            recorded_ids = {b["bucket_id"] for b in SERVER_STATE.ingested_buckets}
            expected_ids = {f"bucket-concurrent-{i}" for i in range(10)}
            self.assertEqual(recorded_ids, expected_ids)


if __name__ == "__main__":
    unittest.main()

