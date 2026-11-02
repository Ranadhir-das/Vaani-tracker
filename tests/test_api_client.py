"""Tests for VaaniApiClient network requests, error handling, and idempotency."""

import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import ShiftStatus, TrackerState
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.models import DeviceInfo, ActivityBucket


class TestVaaniApiClient(unittest.TestCase):

    def setUp(self):
        self.client = VaaniApiClient("http://127.0.0.1:8000")

    @patch("urllib.request.urlopen")
    def test_register_device_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 201
        mock_resp.read.return_value = json.dumps({
            "status": "APPROVED",
            "auth_token": "token_abc123",
            "device_id": "dev-1",
            "employee_id": 10,
            "employee_name": "Test User",
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        dev = DeviceInfo(device_uuid="dev-1", machine_name="PC-1", os_version="Win11", client_version="1.0.0")
        success, data = self.client.register_device(dev, pairing_code="VN-1234")

        self.assertTrue(success)
        self.assertEqual(data["auth_token"], "token_abc123")
        self.assertEqual(data["employee_id"], 10)

    @patch("urllib.request.urlopen")
    def test_fetch_status_active_shift(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({
            "is_approved": True,
            "session": {
                "id": 101,
                "status": "ACTIVE",
                "started_at": "2026-11-02T09:00:00Z",
                "net_seconds": 1800,
                "break_seconds": 300,
            },
            "tracking_policy": {
                "idle_threshold_seconds": 60,
                "track_apps": True,
            },
            "server_time": "2026-11-02T10:00:00Z",
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        online, shift, policy = self.client.fetch_status("dev-1", "token_123")

        self.assertTrue(online)
        self.assertEqual(shift.session_id, 101)
        self.assertEqual(shift.status, ShiftStatus.ACTIVE)
        self.assertEqual(shift.net_seconds, 1800)
        self.assertEqual(policy["idle_threshold_seconds"], 60)

    @patch("urllib.request.urlopen")
    def test_sync_buckets_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({
            "status": "SUCCESS",
            "server_time": "2026-11-02T10:01:00Z",
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        bucket = ActivityBucket(bucket_id="b-1", device_uuid="dev-1", active_seconds=60)
        res = self.client.sync_buckets("dev-1", "token_123", [bucket])

        self.assertTrue(res.success)
        self.assertEqual(res.synced_count, 1)
        self.assertFalse(res.is_duplicate)

    @patch("urllib.request.urlopen")
    def test_sync_buckets_409_conflict_treated_as_idempotent_success(self, mock_urlopen):
        import urllib.error
        # HTTP 409 Conflict
        err = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/v1/desktop-tracker/sync/",
            code=409,
            msg="Conflict: Duplicate batch",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"error": "DUPLICATE_BATCH"}')
        )
        mock_urlopen.side_effect = err

        bucket = ActivityBucket(bucket_id="b-dup", device_uuid="dev-1", active_seconds=60)
        res = self.client.sync_buckets("dev-1", "token_123", [bucket])

        self.assertTrue(res.success)
        self.assertTrue(res.is_duplicate)
        self.assertEqual(res.synced_count, 1)


if __name__ == "__main__":
    unittest.main()

