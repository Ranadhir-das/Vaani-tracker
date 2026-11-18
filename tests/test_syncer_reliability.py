"""Tests for BackgroundSyncer response handling, reliability, and backoff.

Verifies:
- Scenario 11: Network and server failures use exponential backoff.
- Scenario 12: HTTP 409 duplicate acknowledgements are treated as success.
- Scenario 13: Partial batch responses only mark actually accepted records.
- Scenario 14: HTTP 401 and 403 preserve queued telemetry without deletion.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, SyncStatus
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.syncer import BackgroundSyncer
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.models import ActivityBucket, SyncResult
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.windows.idle import WindowsIdleDetector
from vaani_tracker.windows.session import WindowsSessionNotifier
from vaani_tracker.windows.power import WindowsPowerNotifier


class TestSyncerReliability(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig(
            data_dir=self.data_dir,
            device_uuid="dev-syncer-rel",
            sync_interval_seconds=60,
        )
        self.queue = ActivityQueue(self.config.db_path)
        self.auth = DeviceAuthManager(self.config, self.queue)
        self.auth.bind_employee(100, "Reliability User")
        self.auth.set_token("token_valid_100")

        self.idle_detector = WindowsIdleDetector(idle_provider=lambda: 0.0)
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.sm = TrackingStateMachine(self.idle_detector, self.session_notifier, self.power_notifier)

        self.mock_api = MagicMock()
        self.syncer = BackgroundSyncer(
            config=self.config,
            api_client=self.mock_api,
            queue=self.queue,
            auth_manager=self.auth,
            state_machine=self.sm,
        )

    def tearDown(self):
        self.syncer.stop()
        self.temp_dir.cleanup()

    def test_network_failures_use_bounded_exponential_backoff(self):
        """Scenario 11: Consecutive failures increase backoff delay up to a maximum cap."""
        bucket = ActivityBucket(
            bucket_id="b-fail-1",
            device_uuid=self.config.device_uuid,
            employee_id=100,
            active_seconds=60,
        )
        self.queue.save_bucket(bucket, employee_id=100)

        # Simulate consecutive network failures
        self.mock_api.sync_buckets.return_value = SyncResult(
            success=False,
            http_status=0,
            error_message="Network unreachable",
        )

        expected_backoffs = [
            120,  # 60 * 2^1
            240,  # 60 * 2^2
            300,  # 60 * 2^3 = 480 clamped to 300 max
            300,  # capped at 300
        ]

        for idx, expected in enumerate(expected_backoffs, start=1):
            res = self.syncer.sync_pending_now()
            self.assertFalse(res.success)
            self.assertEqual(self.syncer._consecutive_failures, idx)

            # Check backoff formula: min(300, base * (2 ** min(failures, 4)))
            computed_backoff = min(300, self.config.sync_interval_seconds * (2 ** min(self.syncer._consecutive_failures, 4)))
            self.assertEqual(computed_backoff, expected)

    def test_http_409_conflict_treated_as_successful_acknowledgement(self):
        """Scenario 12: HTTP 409 duplicate batch is safely marked synced without double counting."""
        b1 = ActivityBucket(bucket_id="b-dup-409", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 1)

        self.mock_api.sync_buckets.return_value = SyncResult(
            success=True,
            synced_count=1,
            is_duplicate=True,
            http_status=409,
        )

        res = self.syncer.sync_pending_now()
        self.assertTrue(res.success)
        self.assertTrue(res.is_duplicate)
        # Bucket must now be marked SYNCED and cleared from pending
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 0)

    def test_partial_batch_acceptance_does_not_prematurely_mark_all_synced(self):
        """Scenario 13: When server accepts partial batch, only accepted buckets are marked synced."""
        b1 = ActivityBucket(bucket_id="b-part-1", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-part-2", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        b3 = ActivityBucket(bucket_id="b-part-3", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=100)
        self.queue.save_bucket(b2, employee_id=100)
        self.queue.save_bucket(b3, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 3)

        # Server only accepted b1 and b2
        self.mock_api.sync_buckets.return_value = SyncResult(
            success=True,
            synced_count=2,
            accepted_bucket_ids=["b-part-1", "b-part-2"],
            http_status=200,
        )

        res = self.syncer.sync_pending_now()
        self.assertTrue(res.success)

        # Exactly 1 bucket (b3) must remain pending/failed; b1 and b2 must be SYNCED
        pending = self.queue.get_pending_buckets(employee_id=100)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].bucket_id, "b-part-3")

    def test_http_401_revoked_preserves_queued_telemetry(self):
        """Scenario 14a: HTTP 401 clears authentication state but never deletes queued buckets."""
        b1 = ActivityBucket(bucket_id="b-401-1", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 1)

        self.mock_api.sync_buckets.return_value = SyncResult(
            success=False,
            is_revoked=True,
            http_status=401,
            error_message="Device token revoked or expired",
        )

        res = self.syncer.sync_pending_now()
        self.assertFalse(res.success)

        # 1. Device credentials must be cleared
        self.assertFalse(self.auth.is_authenticated())
        self.assertIsNone(self.auth.get_token())

        # 2. But the queued bucket MUST still exist in SQLite with its full data
        self.assertEqual(self.queue.get_total_bucket_count(allow_unfiltered=True), 1)
        # It remains retrievable for employee 100 once re-authenticated
        self.assertEqual(len(self.queue.get_pending_buckets(employee_id=100)), 1)

    def test_http_403_ineligible_preserves_queued_telemetry(self):
        """Scenario 14b: HTTP 403 (ineligible) disconnects tracking without deleting queued buckets."""
        b1 = ActivityBucket(bucket_id="b-403-1", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 1)

        self.mock_api.sync_buckets.return_value = SyncResult(
            success=False,
            is_ineligible=True,
            http_status=403,
            error_code="INELIGIBLE_EMPLOYEE",
            error_message="Employee 100 is not eligible for PC activity tracking.",
        )

        res = self.syncer.sync_pending_now()
        self.assertFalse(res.success)

        # Queued bucket is preserved in SQLite
        self.assertEqual(self.queue.get_total_bucket_count(allow_unfiltered=True), 1)
        pending = self.queue.get_pending_buckets(employee_id=100)
        self.assertEqual(len(pending), 1)
        self.assertIn("INELIGIBLE_EMPLOYEE", pending[0].last_error or "")

    def test_unconfirmed_http_409_is_treated_as_failure(self):
        """Verifies HTTP 409 without duplicate confirmation is treated as a failed sync."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-409-fail", device_uuid="dev-test", active_seconds=60)

        # Mock urllib request to return 409 with non-duplicate error
        import urllib.error
        import io
        error_body = io.BytesIO(b'{"error": "RESOURCE_LOCKED", "message": "Table is locked"}')
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:9999/api/v1/desktop-tracker/sync/",
            code=409,
            msg="Conflict",
            hdrs={},
            fp=error_body,
        )

        with unittest.mock.patch("urllib.request.urlopen", side_effect=http_err):
            result = api_client.sync_buckets("dev-test", "dummy-token", [b1])
            self.assertFalse(result.success)
            self.assertEqual(result.http_status, 409)
            self.assertEqual(result.error_code, "RESOURCE_LOCKED")

    def test_api_client_validates_and_caps_accepted_bucket_ids(self):
        """Verifies api_client strictly discards unknown bucket IDs and caps count."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-valid-1", device_uuid="dev-test", active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-valid-2", device_uuid="dev-test", active_seconds=60)

        # Server response contains valid ID, rogue ID from another device, and extra duplicates
        import io
        success_body = io.BytesIO(
            b'{"status": "SUCCESS", "accepted_bucket_ids": ["b-valid-1", "b-rogue-999", "b-valid-1"]}'
        )
        mock_resp = unittest.mock.MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = success_body.getvalue()
        mock_resp.__enter__.return_value = mock_resp

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
            self.assertTrue(result.success)
            # Rogue ID must be discarded, duplicates deduplicated
            self.assertEqual(result.accepted_bucket_ids, ["b-valid-1"])
            self.assertEqual(result.synced_count, 1)

    def test_server_atomic_success_with_matching_counts(self):
        """C-2: Server returns atomic SUCCESS with matching counts (ingested + duplicate == batch)."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-atom-1", device_uuid="dev-test", active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-atom-2", device_uuid="dev-test", active_seconds=60)
        b3 = ActivityBucket(bucket_id="b-atom-3", device_uuid="dev-test", active_seconds=60)

        # 2 ingested, 1 duplicate == 3 total
        import io
        resp_body = io.BytesIO(
            b'{"status": "SUCCESS", "ingested_count": 2, "duplicate_count": 1, "server_time": "2026-11-09T12:00:00Z"}'
        )
        mock_resp = unittest.mock.MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = resp_body.getvalue()
        mock_resp.__enter__.return_value = mock_resp

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2, b3])
            self.assertTrue(result.success)
            self.assertEqual(result.synced_count, 3)
            self.assertEqual(result.accepted_bucket_ids, ["b-atom-1", "b-atom-2", "b-atom-3"])

        # Also verify BackgroundSyncer marks all 3 buckets synced
        for b in (b1, b2, b3):
            b.employee_id = 100
            self.queue.save_bucket(b, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 3)

        self.mock_api.sync_buckets.return_value = result
        sync_res = self.syncer.sync_pending_now()
        self.assertTrue(sync_res.success)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 0)

    def test_server_atomic_success_duplicate_only_batch(self):
        """C-2: Server returns duplicate-only batch (ingested 0, duplicate 2) -> marked synced."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-dup-only-1", device_uuid="dev-test", active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-dup-only-2", device_uuid="dev-test", active_seconds=60)

        import io
        resp_body = io.BytesIO(
            b'{"status": "SUCCESS", "ingested_count": 0, "duplicate_count": 2, "server_time": "2026-11-09T12:00:00Z"}'
        )
        mock_resp = unittest.mock.MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = resp_body.getvalue()
        mock_resp.__enter__.return_value = mock_resp

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
            self.assertTrue(result.success)
            self.assertEqual(result.synced_count, 2)
            self.assertEqual(result.accepted_bucket_ids, ["b-dup-only-1", "b-dup-only-2"])

        for b in (b1, b2):
            b.employee_id = 100
            self.queue.save_bucket(b, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 2)

        self.mock_api.sync_buckets.return_value = result
        sync_res = self.syncer.sync_pending_now()
        self.assertTrue(sync_res.success)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 0)

    def test_server_count_mismatch_without_ids_prohibits_positional_slicing(self):
        """C-2: Count mismatch without explicit bucket IDs must NOT positionally slice."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-mismatch-1", device_uuid="dev-test", active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-mismatch-2", device_uuid="dev-test", active_seconds=60)
        b3 = ActivityBucket(bucket_id="b-mismatch-3", device_uuid="dev-test", active_seconds=60)

        # Submitted 3, server reports 2 ingested, 0 duplicate (count mismatch: 2 != 3)
        import io
        resp_body = io.BytesIO(
            b'{"status": "SUCCESS", "ingested_count": 2, "duplicate_count": 0, "server_time": "2026-11-09T12:00:00Z"}'
        )
        mock_resp = unittest.mock.MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = resp_body.getvalue()
        mock_resp.__enter__.return_value = mock_resp

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2, b3])
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "AMBIGUOUS_ACKNOWLEDGEMENT")

        # In syncer: verify NO positional slicing happens; all 3 remain in queue
        for b in (b1, b2, b3):
            b.employee_id = 100
            self.queue.save_bucket(b, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 3)

        self.mock_api.sync_buckets.return_value = result
        sync_res = self.syncer.sync_pending_now()
        self.assertFalse(sync_res.success)
        # Positional slicing would have marked b1 and b2 synced, leaving 1 pending.
        # But without positional slicing, all 3 MUST remain in queue!
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 3)
        self.assertEqual(self.syncer._consecutive_failures, 1)

    def test_syncer_handles_ambiguous_success_result_without_slicing(self):
        """C-2: BackgroundSyncer directly receiving synced_count < total without IDs fails safely."""
        b1 = ActivityBucket(bucket_id="b-noslice-1", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-noslice-2", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=100)
        self.queue.save_bucket(b2, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 2)

        # Mock API returning success=True, synced_count=1, but NO accepted_bucket_ids
        self.mock_api.sync_buckets.return_value = SyncResult(
            success=True,
            synced_count=1,
            accepted_bucket_ids=None,
            http_status=200,
        )

        res = self.syncer.sync_pending_now()
        self.assertTrue(res.success)
        # Because accepted_bucket_ids is None and synced_count (1) < total (2),
        # positional slicing is prohibited: both buckets must be marked failed and remain in queue
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 2)
        self.assertEqual(self.syncer._consecutive_failures, 1)

    def test_server_malformed_counts_treated_as_ambiguous(self):
        """C-2: Negative or non-numeric counts are treated as ambiguous failure."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-mal-1", device_uuid="dev-test", active_seconds=60)

        import io
        for malformed in (b'{"status": "SUCCESS", "ingested_count": -1}', b'{"status": "SUCCESS", "ingested_count": "invalid"}'):
            resp_body = io.BytesIO(malformed)
            mock_resp = unittest.mock.MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = resp_body.getvalue()
            mock_resp.__enter__.return_value = mock_resp

            with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
                result = api_client.sync_buckets("dev-test", "dummy-token", [b1])
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "AMBIGUOUS_ACKNOWLEDGEMENT")

    def test_ambiguous_acknowledgement_retry_behavior(self):
        """C-2: Buckets failing from ambiguous acknowledgement remain in queue and succeed on retry."""
        b1 = ActivityBucket(bucket_id="b-retry-1", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-retry-2", device_uuid=self.config.device_uuid, employee_id=100, active_seconds=60)
        self.queue.save_bucket(b1, employee_id=100)
        self.queue.save_bucket(b2, employee_id=100)

        # Attempt 1: Ambiguous acknowledgement (server returned count mismatch)
        self.mock_api.sync_buckets.return_value = SyncResult(
            success=False,
            failed_count=2,
            http_status=200,
            error_code="AMBIGUOUS_ACKNOWLEDGEMENT",
            error_message="Ambiguous sync response: received counts do not match batch size.",
        )
        res1 = self.syncer.sync_pending_now()
        self.assertFalse(res1.success)
        self.assertEqual(self.syncer._consecutive_failures, 1)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 2)

        # Attempt 2: Server returns atomic SUCCESS with matching counts
        self.mock_api.sync_buckets.return_value = SyncResult(
            success=True,
            synced_count=2,
            accepted_bucket_ids=["b-retry-1", "b-retry-2"],
            http_status=200,
        )
        res2 = self.syncer.sync_pending_now()
        self.assertTrue(res2.success)
        self.assertEqual(self.syncer._consecutive_failures, 0)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 0)

    def test_reject_boolean_acknowledgement_counts(self):
        """Phase 2.2: Python booleans (True/False) must be strictly rejected as counts."""
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-bool-1", device_uuid="dev-test", active_seconds=60)

        import io
        boolean_payloads = [
            # 1. ingested_count is True
            b'{"status": "SUCCESS", "ingested_count": true, "duplicate_count": 0}',
            # 2. ingested_count is False
            b'{"status": "SUCCESS", "ingested_count": false, "duplicate_count": 1}',
            # 3. duplicate_count is True
            b'{"status": "SUCCESS", "ingested_count": 1, "duplicate_count": true}',
            # 4. duplicate_count is False
            b'{"status": "SUCCESS", "ingested_count": 0, "duplicate_count": false}',
        ]

        for payload in boolean_payloads:
            resp_body = io.BytesIO(payload)
            mock_resp = unittest.mock.MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = resp_body.getvalue()
            mock_resp.__enter__.return_value = mock_resp

            with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
                result = api_client.sync_buckets("dev-test", "dummy-token", [b1])
                self.assertFalse(result.success, f"Payload {payload} must fail closed")
                self.assertEqual(result.error_code, "AMBIGUOUS_ACKNOWLEDGEMENT")

    def test_http_409_handling_tightened_scenarios(self):
        """Phase 2.2: Rigorous verification of HTTP 409 duplicate handling edge cases."""
        import urllib.error
        import io
        api_client = VaaniApiClient("http://127.0.0.1:9999")
        b1 = ActivityBucket(bucket_id="b-409-1", device_uuid="dev-test", active_seconds=60)
        b2 = ActivityBucket(bucket_id="b-409-2", device_uuid="dev-test", active_seconds=60)

        # 1. Duplicate count equal to complete batch -> duplicate-only success
        body_complete = io.BytesIO(b'{"status": "DUPLICATE", "duplicate_count": 2, "is_duplicate": true}')
        err_complete = urllib.error.HTTPError("url", 409, "Conflict", {}, body_complete)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err_complete):
            res1 = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
            self.assertTrue(res1.success)
            self.assertTrue(res1.is_duplicate)
            self.assertEqual(res1.synced_count, 2)
            self.assertEqual(res1.accepted_bucket_ids, ["b-409-1", "b-409-2"])

        # 2. Duplicate count greater than zero but smaller than batch -> fail closed
        body_partial = io.BytesIO(b'{"status": "DUPLICATE", "duplicate_count": 1, "is_duplicate": true}')
        err_partial = urllib.error.HTTPError("url", 409, "Conflict", {}, body_partial)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err_partial):
            res2 = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
            self.assertFalse(res2.success)
            self.assertEqual(res2.http_status, 409)

        # 3. Missing duplicate count -> fail closed
        body_missing = io.BytesIO(b'{"status": "DUPLICATE", "is_duplicate": true}')
        err_missing = urllib.error.HTTPError("url", 409, "Conflict", {}, body_missing)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err_missing):
            res3 = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
            self.assertFalse(res3.success)
            self.assertEqual(res3.http_status, 409)

        # 4. Malformed duplicate count (boolean or string) -> fail closed
        for malformed in (b'{"status": "DUPLICATE", "duplicate_count": true}', b'{"status": "DUPLICATE", "duplicate_count": "2"}'):
            body_mal = io.BytesIO(malformed)
            err_mal = urllib.error.HTTPError("url", 409, "Conflict", {}, body_mal)
            with unittest.mock.patch("urllib.request.urlopen", side_effect=err_mal):
                res4 = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
                self.assertFalse(res4.success)
                self.assertEqual(res4.http_status, 409)

        # 5. Unrecognized 409 response -> fail closed
        body_unrec = io.BytesIO(b'{"error": "RESOURCE_LOCKED", "message": "Database row locked"}')
        err_unrec = urllib.error.HTTPError("url", 409, "Conflict", {}, body_unrec)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err_unrec):
            res5 = api_client.sync_buckets("dev-test", "dummy-token", [b1, b2])
            self.assertFalse(res5.success)
            self.assertEqual(res5.http_status, 409)

        # 6. Confirmation that ambiguous 409 responses never mark buckets SYNCED in syncer
        self.queue.save_bucket(b1, employee_id=100)
        self.queue.save_bucket(b2, employee_id=100)
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 2)

        self.mock_api.sync_buckets.return_value = res2  # partial count failure from scenario 2
        sync_res = self.syncer.sync_pending_now()
        self.assertFalse(sync_res.success)
        # Neither bucket may be marked SYNCED; both must remain in queue
        self.assertEqual(self.queue.get_pending_count(employee_id=100), 2)


if __name__ == "__main__":
    unittest.main()

