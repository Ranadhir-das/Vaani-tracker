"""HTTPS API client for Vaani backend communication."""

import json
import logging
import platform
from typing import Tuple, Dict, Any, List, Optional
import urllib.request
import urllib.error
import urllib.parse

from ..constants import ShiftStatus
from ..models import DeviceInfo, ShiftInfo, ActivityBucket, SyncResult
from ..privacy import PrivacyGuard

logger = logging.getLogger(__name__)


class VaaniApiClient:
    """Communicates with the Vaani Django CRM backend via HTTPS REST APIs."""

    def __init__(self, backend_url: str, client_version: str = "1.0.0", timeout: float = 15.0):
        self.backend_url = backend_url.rstrip("/")
        self.client_version = client_version
        self.timeout = timeout
        self.user_agent = f"VaaniDesktopTracker/{client_version} ({platform.system()} {platform.release()})"

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Makes an HTTP/HTTPS request using standard urllib.

        Returns (http_status_code, response_json_or_dict).
        """
        url = f"{self.backend_url}{endpoint}"
        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        body_bytes = None
        if data is not None:
            # Audit outgoing payload against strict privacy rules before serializing
            PrivacyGuard.audit_payload(data)
            body_bytes = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                status_code = response.getcode()
                raw_content = response.read().decode("utf-8")
                try:
                    resp_data = json.loads(raw_content) if raw_content else {}
                except json.JSONDecodeError:
                    resp_data = {"raw": raw_content}
                return status_code, resp_data

        except urllib.error.HTTPError as e:
            status_code = e.code
            raw_content = e.read().decode("utf-8")
            try:
                resp_data = json.loads(raw_content) if raw_content else {}
            except json.JSONDecodeError:
                resp_data = {"error": str(e), "raw": raw_content}
            return status_code, resp_data

        except urllib.error.URLError as e:
            logger.warning(f"Network error contacting {url}: {e.reason}")
            return 0, {"error": "NETWORK_UNREACHABLE", "reason": str(e.reason)}

        except Exception as e:
            logger.error(f"Unexpected error contacting {url}: {e}", exc_info=True)
            return 0, {"error": "CLIENT_EXCEPTION", "message": str(e)}

    def register_device(
        self,
        device_info: DeviceInfo,
        pairing_code: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Registers the PC with the Vaani backend.

        Endpoint: POST /api/v1/desktop-tracker/register/
        """
        payload = {
            "device_uuid": device_info.device_uuid,
            "machine_name": device_info.machine_name,
            "os_version": device_info.os_version,
            "client_version": self.client_version,
        }
        if pairing_code:
            payload["pairing_code"] = pairing_code.strip()

        status, data = self._make_request("/api/v1/desktop-tracker/register/", method="POST", data=payload)

        if status in (200, 201):
            return True, data
        return False, data

    def fetch_status(
        self,
        device_uuid: str,
        token: str,
    ) -> Tuple[bool, ShiftInfo, Dict[str, Any]]:
        """Fetches device approval, heartbeat, and authoritative shift status.

        Endpoint: GET /api/v1/desktop-tracker/status/
        Headers: X-Device-Token
        """
        headers = {
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        status, data = self._make_request("/api/v1/desktop-tracker/status/", method="GET", headers=headers)

        if status == 200:
            raw_session = data.get("session") or {}
            raw_status = raw_session.get("status", "NONE").upper()
            shift_status = ShiftStatus(raw_status) if raw_status in ShiftStatus._value2member_map_ else ShiftStatus.NONE

            shift = ShiftInfo(
                session_id=raw_session.get("id"),
                status=shift_status,
                started_at=raw_session.get("started_at"),
                net_seconds=int(raw_session.get("net_seconds", 0)),
                break_seconds=int(raw_session.get("break_seconds", 0)),
                current_break_name=raw_session.get("current_break_name"),
                server_time=data.get("server_time"),
            )
            policy = data.get("tracking_policy") or {}
            return True, shift, policy

        elif status in (401, 403):
            logger.warning(f"Device token revoked or unauthorized (HTTP {status})")
            return False, ShiftInfo(status=ShiftStatus.NONE), {"revoked": True, "error": data.get("error")}

        else:
            return False, ShiftInfo(status=ShiftStatus.NONE), {"error": data.get("error", "OFFLINE")}

    def sync_buckets(
        self,
        device_uuid: str,
        token: str,
        buckets: List[ActivityBucket],
    ) -> SyncResult:
        """Uploads a batch of completed activity buckets.

        Endpoint: POST /api/v1/desktop-tracker/sync/
        Headers: X-Device-Token
        """
        if not buckets:
            return SyncResult(success=True, synced_count=0)

        headers = {
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        payload = {
            "device_uuid": device_uuid,
            "buckets": [b.to_api_payload() for b in buckets],
        }

        status, data = self._make_request("/api/v1/desktop-tracker/sync/", method="POST", data=payload, headers=headers)

        if status in (200, 201):
            return SyncResult(
                success=True,
                synced_count=len(buckets),
                server_time=data.get("server_time"),
            )
        elif status == 409:
            # 409 Conflict: Duplicates already ingested on server.
            # Client treats as success to safely advance queue without data loss.
            logger.info("Batch already received by server (HTTP 409 Conflict idempotency).")
            return SyncResult(
                success=True,
                synced_count=len(buckets),
                is_duplicate=True,
                server_time=data.get("server_time"),
            )
        elif status in (401, 403):
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                is_revoked=True,
                error_message="Device token revoked or unauthorized.",
            )
        else:
            err = data.get("error") or data.get("message") or f"HTTP {status}"
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                error_message=str(err),
            )

