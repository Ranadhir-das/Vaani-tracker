"""Tests for data models serialization, deserialization, and API payloads."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.constants import TrackerState, ShiftStatus, SyncStatus
from vaani_tracker.models import DeviceInfo, ShiftInfo, AppUsage, ActivityBucket, SyncResult


class TestModels(unittest.TestCase):

    def test_device_info_dict(self):
        d = DeviceInfo(
            device_uuid="uuid-1",
            machine_name="PC-DEV",
            os_version="Windows 11",
            client_version="1.0.0",
            employee_id=5,
            employee_name="John Doe",
        )
        as_dict = d.to_dict()
        self.assertEqual(as_dict["device_uuid"], "uuid-1")
        self.assertEqual(as_dict["employee_name"], "John Doe")

    def test_shift_info_eligibility(self):
        none_shift = ShiftInfo(status=ShiftStatus.NONE)
        self.assertFalse(none_shift.is_tracking_eligible)

        completed_shift = ShiftInfo(session_id=1, status=ShiftStatus.COMPLETED)
        self.assertFalse(completed_shift.is_tracking_eligible)

        paused_shift = ShiftInfo(session_id=1, status=ShiftStatus.PAUSED)
        self.assertFalse(paused_shift.is_tracking_eligible)

        active_shift = ShiftInfo(session_id=1, status=ShiftStatus.ACTIVE)
        self.assertTrue(active_shift.is_tracking_eligible)

    def test_activity_bucket_api_payload(self):
        b = ActivityBucket(
            bucket_id="b-uuid-123",
            device_uuid="dev-uuid-456",
            session_id=10,
            active_seconds=45,
            idle_seconds=15,
            state=TrackerState.ACTIVE,
            app_usages=[AppUsage(process_name="code.exe", duration_seconds=45)],
        )
        payload = b.to_api_payload()
        self.assertEqual(payload["bucket_id"], "b-uuid-123")
        self.assertEqual(payload["session_id"], 10)
        self.assertEqual(payload["active_seconds"], 45)
        self.assertEqual(payload["idle_seconds"], 15)
        self.assertEqual(payload["state"], "ACTIVE")
        self.assertEqual(payload["app_usages"][0]["process_name"], "code.exe")
        self.assertEqual(payload["app_usages"][0]["duration_seconds"], 45)
        # Verify internal fields are NOT in the API payload
        self.assertNotIn("sync_status", payload)
        self.assertNotIn("retry_count", payload)


if __name__ == "__main__":
    unittest.main()

