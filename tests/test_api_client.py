"""Tests for VaaniApiClient network requests, error handling, and idempotency."""

import json
import unittest
import urllib.request
import urllib.error
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
        # HTTP 409 Conflict with complete duplicate count confirmation
        err = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/v1/desktop-tracker/sync/",
            code=409,
            msg="Conflict: Duplicate batch",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"status": "DUPLICATE", "duplicate_count": 1, "is_duplicate": true}')
        )
        mock_urlopen.side_effect = err

        bucket = ActivityBucket(bucket_id="b-dup", device_uuid="dev-1", active_seconds=60)
        res = self.client.sync_buckets("dev-1", "token_123", [bucket])

        self.assertTrue(res.success)
        self.assertTrue(res.is_duplicate)
        self.assertEqual(res.synced_count, 1)


class TestVaaniApiClientTransportSecurity(unittest.TestCase):
    """Regression and security tests for Phase 3.2 and Phase 3.3 transport security."""

    def setUp(self):
        self.dev = DeviceInfo(device_uuid="dev-sec-1", machine_name="PC-SEC", os_version="Win11", client_version="1.0.0")

    # ==========================================================================
    # HTTP POLICY TESTS
    # ==========================================================================

    @patch("urllib.request.urlopen")
    def test_production_http_rejected_before_network(self, mock_urlopen):
        """Production HTTP is rejected before any network request is attempted."""
        client = VaaniApiClient("http://vaaniapp.co.in")
        ok, resp = client.register_device(self.dev, pairing_code="VN-1234")
        self.assertFalse(ok)
        self.assertEqual(resp["error"], "INSECURE_CONNECTION")
        self.assertIn("strictly prohibited", resp["message"])
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_trailing_dot_production_urls_rejected(self, mock_urlopen):
        """Trailing-dot production URLs (FQDN) are normalized and rejected over plain HTTP."""
        for dot_url in ["http://vaaniapp.co.in.", "http://api.vaaniapp.co.in.", "http://vaaniapp.co.in.:80"]:
            client = VaaniApiClient(dot_url)
            ok, resp = client.register_device(self.dev, pairing_code="VN-1234")
            self.assertFalse(ok)
            self.assertEqual(resp["error"], "INSECURE_CONNECTION")
            self.assertIn("strictly prohibited", resp["message"])
            mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_production_https_accepted(self, mock_urlopen):
        """Production HTTPS (with or without trailing dots) is accepted."""
        for https_url in ["https://vaaniapp.co.in", "https://vaaniapp.co.in.", "https://api.vaaniapp.co.in."]:
            client = VaaniApiClient(https_url)
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = json.dumps({"status": "SUCCESS"}).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            status, data = client._make_request("/api/v1/desktop-tracker/status/")
            self.assertEqual(status, 200)

    @patch("urllib.request.urlopen")
    def test_production_hostname_explicit_port(self, mock_urlopen):
        """Production hostname with explicit port is validated properly."""
        client_https_port = VaaniApiClient("https://vaaniapp.co.in:443")
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        status, _ = client_https_port._make_request("/api/v1/desktop-tracker/status/")
        self.assertEqual(status, 200)

        # Insecure HTTP with explicit port is rejected
        client_http_port = VaaniApiClient("http://vaaniapp.co.in:8080")
        ok, resp = client_http_port.register_device(self.dev, pairing_code="VN-1234")
        self.assertFalse(ok)
        self.assertEqual(resp["error"], "INSECURE_CONNECTION")

    @patch("urllib.request.urlopen")
    def test_production_hostname_case_normalization(self, mock_urlopen):
        """Production hostname is case-normalized during security check."""
        for url in ["http://VAANIAPP.CO.IN", "http://VaaniApp.Co.In:80", "http://VaaniApp.Co.In."]:
            client = VaaniApiClient(url)
            ok, resp = client.register_device(self.dev, pairing_code="VN-1234")
            self.assertFalse(ok)
            self.assertEqual(resp["error"], "INSECURE_CONNECTION")
            mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_remote_crm_http_rejected_for_status_and_sync(self, mock_urlopen):
        """Arbitrary remote CRM hosts over plain HTTP are rejected for status and telemetry sync."""
        client = VaaniApiClient("http://remote-crm.example.com")

        # 1. Status request rejected
        ok, _, resp = client.fetch_status("dev-1", "vdt_token_123")
        self.assertFalse(ok)
        self.assertEqual(resp.get("error"), "INSECURE_CONNECTION")
        mock_urlopen.assert_not_called()

        # 2. Sync request rejected
        bucket = ActivityBucket(bucket_id="b-remote", device_uuid="dev-1", active_seconds=60)
        sync_res = client.sync_buckets("dev-1", "vdt_token_123", [bucket])
        self.assertFalse(sync_res.success)
        self.assertEqual(sync_res.error_code, "NETWORK_ERROR")
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_remote_http_with_sensitive_headers_rejected_before_transport(self, mock_urlopen):
        """Remote HTTP requests bearing X-Device-Token or Authorization are blocked before transport."""
        client = VaaniApiClient("http://insecure-crm.internal.corp")

        status, resp = client._make_request(
            "/api/v1/desktop-tracker/status/",
            headers={"X-Device-Token": "vdt_secret_device_token"}
        )
        self.assertEqual(status, 0)
        self.assertEqual(resp["error"], "INSECURE_CONNECTION")
        self.assertIn("Remote endpoints require HTTPS", resp["message"])
        mock_urlopen.assert_not_called()

        status2, resp2 = client._make_request(
            "/api/v1/desktop-tracker/status/",
            headers={"Authorization": "Bearer secret_bearer_token"}
        )
        self.assertEqual(status2, 0)
        self.assertEqual(resp2["error"], "INSECURE_CONNECTION")
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_remote_ip_over_http_rejected(self, mock_urlopen):
        """Remote IP addresses over plain HTTP are strictly rejected."""
        for ip_url in ["http://192.168.1.50:8000", "http://159.89.172.172", "http://10.0.0.1"]:
            client = VaaniApiClient(ip_url)
            status, resp = client._make_request("/api/v1/desktop-tracker/status/")
            self.assertEqual(status, 0)
            self.assertEqual(resp["error"], "INSECURE_CONNECTION")
            mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_lookalike_attacker_hostname_handling(self, mock_urlopen):
        """Attacker lookalike hostnames are not classified as production and rejected over HTTP."""
        # Over plain HTTP: rejected because they are remote hosts
        for lookalike in [
            "http://vaaniapp.co.in.attacker.example",
            "http://notvaaniapp.co.in",
            "http://attacker-vaaniapp.co.in.example",
        ]:
            client_attacker_http = VaaniApiClient(lookalike)
            ok, resp = client_attacker_http.register_device(self.dev, username="user", password="pwd")
            self.assertFalse(ok)
            self.assertEqual(resp["error"], "INSECURE_CONNECTION")
            mock_urlopen.assert_not_called()

        # Over HTTPS: allowed as arbitrary remote hosts (not production)
        client_attacker_https = VaaniApiClient("https://vaaniapp.co.in.attacker.example")
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        status, _ = client_attacker_https._make_request("/api/v1/desktop-tracker/status/")
        self.assertEqual(status, 200)

    @patch("urllib.request.urlopen")
    def test_localhost_and_loopback_variants_supported(self, mock_urlopen):
        """Local development HTTP URLs (localhost, 127.0.0.1, ::1, testserver) are explicitly permitted."""
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 201
        mock_resp.read.return_value = json.dumps({"status": "APPROVED", "auth_token": "tok"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        for local_url in [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://[::1]:8000",
            "http://testserver",
        ]:
            client = VaaniApiClient(local_url)
            ok, _ = client.register_device(self.dev, username="dev_user", password="dev_password")
            self.assertTrue(ok, f"Failed for local url: {local_url}")

    @patch("urllib.request.urlopen")
    def test_unsupported_schemes_rejected_before_network(self, mock_urlopen):
        """Unsupported URL schemes (ftp, file, etc.) are rejected before opening network connection."""
        for bad_url in ["ftp://vaaniapp.co.in/api", "file:///C:/config.json", "gopher://vaaniapp.co.in"]:
            client = VaaniApiClient(bad_url)
            ok, resp = client.register_device(self.dev, pairing_code="VN-1234")
            self.assertFalse(ok)
            self.assertEqual(resp["error"], "UNSUPPORTED_SCHEME")
            mock_urlopen.assert_not_called()

    # ==========================================================================
    # REDIRECT POLICY TESTS
    # ==========================================================================

    def test_all_five_registration_redirect_codes_blocked(self):
        """All five registration redirect codes (301, 302, 303, 307, 308) are blocked."""
        from vaani_tracker.client.api import StrictRedirectHandler

        handler = StrictRedirectHandler()
        req_reg = urllib.request.Request("https://vaaniapp.co.in/api/v1/desktop-tracker/register/")

        for code in (301, 302, 303, 307, 308):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                handler.redirect_request(req_reg, None, code, "Redirect", {}, "https://vaaniapp.co.in/somewhere-else/")
            self.assertEqual(ctx.exception.code, code)
            self.assertIn("prohibited by security policy", str(ctx.exception.reason))

    def test_https_to_http_downgrade_blocked_for_status_and_sync(self):
        """HTTPS to HTTP downgrade redirect is blocked for both status and sync endpoints."""
        from vaani_tracker.client.api import StrictRedirectHandler

        handler = StrictRedirectHandler()

        # 1. Status endpoint downgrade blocked
        req_status = urllib.request.Request("https://vaaniapp.co.in/api/v1/desktop-tracker/status/")
        with self.assertRaises(urllib.error.HTTPError) as ctx1:
            handler.redirect_request(req_status, None, 302, "Found", {}, "http://vaaniapp.co.in/api/v1/desktop-tracker/status/")
        self.assertEqual(ctx1.exception.code, 302)
        self.assertIn("Insecure redirect downgrade", str(ctx1.exception.reason))

        # 2. Sync endpoint downgrade blocked
        req_sync = urllib.request.Request("https://vaaniapp.co.in/api/v1/desktop-tracker/sync/", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            handler.redirect_request(req_sync, None, 301, "Moved", {}, "http://vaaniapp.co.in/api/v1/desktop-tracker/sync/")
        self.assertEqual(ctx2.exception.code, 301)
        self.assertIn("Insecure redirect downgrade", str(ctx2.exception.reason))

    def test_cross_host_https_redirect_blocked_for_status_and_sync(self):
        """Cross-host HTTPS redirects are blocked for both status and sync endpoints."""
        from vaani_tracker.client.api import StrictRedirectHandler

        handler = StrictRedirectHandler()

        # 1. Cross-host redirect on status GET blocked
        req_status = urllib.request.Request(
            "https://vaaniapp.co.in/api/v1/desktop-tracker/status/",
            headers={"X-Device-Token": "vdt_secret_device_token"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx1:
            handler.redirect_request(req_status, None, 302, "Found", {}, "https://evil.attacker.com/status/")
        self.assertEqual(ctx1.exception.code, 302)
        self.assertIn("Cross-origin redirect", str(ctx1.exception.reason))

        # 2. Cross-host redirect on sync POST blocked
        req_sync = urllib.request.Request(
            "https://vaaniapp.co.in/api/v1/desktop-tracker/sync/",
            method="POST",
            headers={"X-Device-Token": "vdt_secret_device_token"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            handler.redirect_request(req_sync, None, 302, "Found", {}, "https://evil.attacker.com/sync/")
        self.assertEqual(ctx2.exception.code, 302)
        self.assertIn("Cross-origin redirect", str(ctx2.exception.reason))

    def test_cross_port_https_redirect_blocked(self):
        """Redirects changing the effective port are treated as cross-origin and blocked."""
        from vaani_tracker.client.api import StrictRedirectHandler

        handler = StrictRedirectHandler()

        req = urllib.request.Request("https://vaaniapp.co.in:443/api/v1/desktop-tracker/status/")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "https://vaaniapp.co.in:8443/api/v1/desktop-tracker/status/")
        self.assertEqual(ctx.exception.code, 302)
        self.assertIn("Cross-origin redirect", str(ctx.exception.reason))

    def test_same_origin_redirect_method_policy(self):
        """Same-origin redirect is allowed for GET/HEAD but strictly blocked for POST."""
        from vaani_tracker.client.api import StrictRedirectHandler

        handler = StrictRedirectHandler()

        # 1. Same-origin POST redirect is blocked (prevents payload corruption / body loss)
        req_post = urllib.request.Request(
            "https://vaaniapp.co.in/api/v1/desktop-tracker/sync/",
            data=b'{"buckets": []}',
            method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(req_post, None, 302, "Found", {}, "https://vaaniapp.co.in/api/v1/desktop-tracker/sync-v2/")
        self.assertEqual(ctx.exception.code, 302)
        self.assertIn("Redirect for method 'POST' is prohibited", str(ctx.exception.reason))

        # 2. Same-origin GET redirect (e.g. trailing slash normalization) is permitted
        req_get = urllib.request.Request(
            "https://vaaniapp.co.in/api/v1/desktop-tracker/status",
            headers={"X-Device-Token": "vdt_secret_device_token"}
        )
        res = handler.redirect_request(req_get, None, 301, "Moved Permanently", {}, "https://vaaniapp.co.in/api/v1/desktop-tracker/status/")
        self.assertIsNotNone(res)
        self.assertEqual(res.full_url, "https://vaaniapp.co.in/api/v1/desktop-tracker/status/")
        self.assertEqual(res.headers.get("X-device-token"), "vdt_secret_device_token")

        # 3. Same-origin GET redirect with trailing dot hostname is normalized and permitted
        res_dot = handler.redirect_request(req_get, None, 301, "Moved", {}, "https://vaaniapp.co.in./api/v1/desktop-tracker/status/")
        self.assertIsNotNone(res_dot)


if __name__ == "__main__":
    unittest.main()

