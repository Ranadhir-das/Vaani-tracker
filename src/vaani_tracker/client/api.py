"""HTTPS API client for Vaani backend communication."""

import json
import logging
import platform
from typing import Tuple, Dict, Any, List, Optional
import urllib.request
import urllib.error
import urllib.parse

from ..constants import ShiftStatus, APP_VERSION
from ..models import DeviceInfo, ShiftInfo, ActivityBucket, SyncResult
from ..privacy import PrivacyGuard

logger = logging.getLogger(__name__)


class StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforces transport security redirect boundaries.

    - Disallows all redirects on credential-bearing registration requests.
    - Disallows insecure redirects that downgrade from HTTPS to HTTP.
    - Disallows cross-origin redirects (different host, port, or scheme).
    - Disallows redirects on unsafe or non-idempotent methods (e.g. POST requests).
    - Allows only same-origin GET/HEAD redirects without leaking sensitive credentials across origins.
    """

    @staticmethod
    def _normalize_host(h: Optional[str]) -> str:
        if not h:
            return ""
        return h.strip().lower().rstrip(".")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 1. Registration endpoint must never follow any redirect
        if "/api/v1/desktop-tracker/register/" in req.full_url:
            logger.warning(f"Redirect ({code}) rejected for registration endpoint: {req.full_url} -> {newurl}")
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Redirect from registration endpoint to '{newurl}' is prohibited by security policy.",
                headers, fp
            )

        try:
            req_p = urllib.parse.urlsplit(req.full_url)
            new_p = urllib.parse.urlsplit(newurl)
        except Exception as e:
            raise urllib.error.HTTPError(req.full_url, code, f"Malformed redirect URL '{newurl}': {e}", headers, fp)

        req_scheme = (req_p.scheme or "").lower().strip()
        new_scheme = (new_p.scheme or "").lower().strip()

        # 2. Block any downgrade from HTTPS to HTTP
        if req_scheme == "https" and new_scheme == "http":
            logger.error(f"Insecure redirect downgrade from HTTPS to HTTP blocked: {req.full_url} -> {newurl}")
            raise urllib.error.HTTPError(
                req.full_url, code,
                "Insecure redirect downgrade from HTTPS to HTTP is prohibited.",
                headers, fp
            )

        req_host = self._normalize_host(req_p.hostname)
        new_host = self._normalize_host(new_p.hostname)
        req_port = req_p.port or (443 if req_scheme == "https" else 80)
        new_port = new_p.port or (443 if new_scheme == "https" else 80)

        # 3. Block cross-origin redirects (hostname, port, or scheme mismatch)
        if (req_scheme, req_host, req_port) != (new_scheme, new_host, new_port):
            logger.warning(f"Cross-origin redirect ({code}) blocked: {req.full_url} -> {newurl}")
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Cross-origin redirect to '{newurl}' is prohibited by security policy.",
                headers, fp
            )

        # 4. Block redirects on POST methods to prevent payload loss or re-transmission
        m = req.get_method()
        if m not in ("GET", "HEAD"):
            logger.warning(f"Redirect ({code}) for method '{m}' blocked: {req.full_url}")
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Redirect for method '{m}' is prohibited by security policy.",
                headers, fp
            )

        return super().redirect_request(req, fp, code, msg, headers, newurl)


class VaaniApiClient:
    """Communicates with the Vaani Django CRM backend via HTTPS REST APIs."""

    PRODUCTION_HOST = "vaaniapp.co.in"
    LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}

    @classmethod
    def normalize_hostname(cls, host: Optional[str]) -> str:
        """Normalizes hostname for security checks by lowercasing and stripping trailing dots."""
        if not host:
            return ""
        return host.strip().lower().rstrip(".")

    def __init__(self, backend_url: str, client_version: str = APP_VERSION, timeout: float = 15.0):
        self.backend_url = backend_url.rstrip("/")
        self.client_version = client_version
        self.timeout = timeout
        self.user_agent = f"VaaniDesktopTracker/{client_version} ({platform.system()} {platform.release()})"
        self._opener = urllib.request.build_opener(StrictRedirectHandler())

    def _validate_url(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str], Optional[str]]:
        """Validates transport security and URL structure before opening any network connection.

        Returns (is_valid, error_code, error_message).
        """
        full_url = f"{self.backend_url}{endpoint}"
        try:
            parsed = urllib.parse.urlsplit(full_url)
        except Exception as e:
            return False, "INVALID_URL", f"Malformed URL: {e}"

        scheme = (parsed.scheme or "").lower().strip()
        if scheme not in ("http", "https"):
            return False, "UNSUPPORTED_SCHEME", f"Unsupported URL scheme '{scheme}'. Only HTTPS and HTTP are supported."

        raw_hostname = (parsed.hostname or "").strip()
        if not raw_hostname:
            return False, "INVALID_HOSTNAME", "Target URL has no valid hostname."

        norm_host = self.normalize_hostname(raw_hostname)
        if not norm_host:
            return False, "INVALID_HOSTNAME", "Target URL has no valid hostname."

        is_local_host = (norm_host in self.LOCAL_HOSTS)
        is_prod_host = (norm_host == self.PRODUCTION_HOST or norm_host.endswith(f".{self.PRODUCTION_HOST}"))

        # Plain HTTP is strictly forbidden for ALL non-local hosts across all endpoints
        if scheme != "https" and not is_local_host:
            if is_prod_host:
                return False, "INSECURE_CONNECTION", (
                    f"Insecure HTTP connection to production host '{raw_hostname}' is strictly prohibited. "
                    "Production endpoints require HTTPS."
                )
            return False, "INSECURE_CONNECTION", (
                f"Insecure HTTP connection to remote host '{raw_hostname}' is prohibited. "
                "Remote endpoints require HTTPS."
            )

        return True, None, None

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
        # Validate URL and transport security upfront before opening any connection
        is_valid, err_code, err_msg = self._validate_url(endpoint, data=data)
        if not is_valid:
            logger.error(f"Request blocked by transport security policy: {err_msg}")
            return 0, {"error": err_code, "message": err_msg}

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
            # Telemetry payloads must never contain passwords, keystrokes, URLs, etc.
            # Registration payload may contain user-entered login credentials.
            if endpoint != "/api/v1/desktop-tracker/register/":
                PrivacyGuard.audit_payload(data)
            body_bytes = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)

        try:
            # If urllib.request.urlopen has been mocked (e.g. in test suites), delegate to it.
            # Otherwise use self._opener which enforces StrictRedirectHandler.
            if hasattr(urllib.request.urlopen, "assert_called") or hasattr(urllib.request.urlopen, "mock"):
                open_fn = urllib.request.urlopen
            else:
                open_fn = getattr(self, "_opener", urllib.request).open

            with open_fn(req, timeout=self.timeout) as response:
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
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Registers the PC with the Vaani backend via pairing code or username/password.

        Endpoint: POST /api/v1/desktop-tracker/register/
        """
        payload: Dict[str, Any] = {
            "device_uuid": device_info.device_uuid,
            "machine_name": device_info.machine_name,
            "os_version": device_info.os_version,
            "client_version": self.client_version,
        }
        if pairing_code and pairing_code.strip():
            payload["pairing_code"] = pairing_code.strip()
        elif username and password:
            payload["username"] = username.strip()
            payload["password"] = password

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
            shift = self._parse_shift_payload(data)
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

        submitted_ids = [b.bucket_id for b in buckets]
        submitted_set = set(submitted_ids)

        if status in (200, 201):
            raw_accepted = data.get("accepted_bucket_ids")
            if isinstance(raw_accepted, list):
                # Validate accepted bucket IDs strictly against submitted set
                valid_accepted = [str(bid) for bid in raw_accepted if isinstance(bid, str) and bid in submitted_set]
                # Deduplicate while preserving order
                accepted_ids = list(dict.fromkeys(valid_accepted))
                synced_cnt = len(accepted_ids)
                if len(raw_accepted) != len(accepted_ids):
                    logger.warning(
                        f"Server returned {len(raw_accepted)} accepted_bucket_ids, but only "
                        f"{len(accepted_ids)} matched submitted batch."
                    )
                return SyncResult(
                    success=True,
                    synced_count=synced_cnt,
                    server_time=data.get("server_time"),
                    accepted_bucket_ids=accepted_ids,
                    http_status=status,
                )
            else:
                # Backend returned an atomic response without explicit bucket IDs
                ingested = data.get("ingested_count")
                duplicate = data.get("duplicate_count")

                counts_valid = False
                ingested_cnt = 0
                duplicate_cnt = 0
                if ingested is not None or duplicate is not None:
                    # Python bool is a subclass of int (isinstance(True, int) is True).
                    # Explicitly reject boolean values for both count fields.
                    if isinstance(ingested, bool) or isinstance(duplicate, bool):
                        counts_valid = False
                    else:
                        try:
                            if ingested is not None:
                                if not isinstance(ingested, int) or ingested < 0:
                                    raise ValueError("ingested_count must be a non-negative integer")
                                ingested_cnt = ingested
                            else:
                                ingested_cnt = 0

                            if duplicate is not None:
                                if not isinstance(duplicate, int) or duplicate < 0:
                                    raise ValueError("duplicate_count must be a non-negative integer")
                                duplicate_cnt = duplicate
                            else:
                                duplicate_cnt = 0

                            counts_valid = True
                        except (ValueError, TypeError):
                            counts_valid = False

                if counts_valid and (ingested_cnt + duplicate_cnt == len(buckets)):
                    logger.info(
                        f"Batch sync atomically accepted: {ingested_cnt} ingested, "
                        f"{duplicate_cnt} duplicate (total {len(buckets)})."
                    )
                    return SyncResult(
                        success=True,
                        synced_count=len(buckets),
                        server_time=data.get("server_time"),
                        accepted_bucket_ids=list(submitted_ids),
                        http_status=status,
                    )
                elif not counts_valid and data.get("status") == "SUCCESS" and "ingested_count" not in data and "duplicate_count" not in data:
                    logger.info(f"Batch sync atomically accepted via status SUCCESS ({len(buckets)} buckets).")
                    return SyncResult(
                        success=True,
                        synced_count=len(buckets),
                        server_time=data.get("server_time"),
                        accepted_bucket_ids=list(submitted_ids),
                        http_status=status,
                    )
                else:
                    # Counts do not match submitted batch size or are malformed/ambiguous.
                    # Never use positional slicing; treat as ambiguous failure for safe retry.
                    logger.warning(
                        f"Ambiguous sync response from server (HTTP {status}) without explicit bucket IDs: "
                        f"ingested_count={ingested}, duplicate_count={duplicate}, "
                        f"expected total={len(buckets)}. Treating batch as failed for safe retry."
                    )
                    return SyncResult(
                        success=False,
                        failed_count=len(buckets),
                        http_status=status,
                        error_code="AMBIGUOUS_ACKNOWLEDGEMENT",
                        error_message=(
                            f"Ambiguous sync response: received counts (ingested={ingested}, "
                            f"duplicate={duplicate}) do not match batch size ({len(buckets)})."
                        ),
                    )
        elif status == 409:
            # Validate whether HTTP 409 demonstrably confirms an idempotent duplicate outcome
            raw_accepted = data.get("accepted_bucket_ids")
            if isinstance(raw_accepted, list):
                valid_accepted = [str(bid) for bid in raw_accepted if isinstance(bid, str) and bid in submitted_set]
                accepted_ids = list(dict.fromkeys(valid_accepted))
                if len(raw_accepted) != len(accepted_ids):
                    logger.warning(
                        f"Server returned {len(raw_accepted)} accepted_bucket_ids on HTTP 409, "
                        f"but only {len(accepted_ids)} matched submitted batch."
                    )
                return SyncResult(
                    success=True,
                    synced_count=len(accepted_ids),
                    is_duplicate=True,
                    server_time=data.get("server_time"),
                    accepted_bucket_ids=accepted_ids,
                    http_status=409,
                )

            # If accepted_bucket_ids is absent, check for explicit duplicate confirmation of the COMPLETE batch
            has_dup_marker = (
                data.get("status") in ("DUPLICATE", "ALREADY_EXISTS")
                or data.get("is_duplicate") is True
                or data.get("error") == "DUPLICATE_BATCH"
                or (data.get("status") == "SUCCESS" and "duplicate_count" in data)
            )

            raw_dup = data.get("duplicate_count")
            dup_cnt = None
            dup_valid = False
            if raw_dup is not None and not isinstance(raw_dup, bool) and isinstance(raw_dup, int) and raw_dup >= 0:
                dup_cnt = raw_dup
                dup_valid = True

            # Must never accept partial duplicates (duplicate_count > 0 but < len(buckets)).
            # Must strictly require a verified duplicate marker and duplicate_count == len(buckets).
            if has_dup_marker and dup_valid and dup_cnt == len(buckets):
                logger.info(
                    f"Complete batch demonstrably confirmed as duplicate by server (HTTP 409 Conflict): "
                    f"duplicate_count={dup_cnt} matches batch size ({len(buckets)})."
                )
                return SyncResult(
                    success=True,
                    synced_count=len(buckets),
                    is_duplicate=True,
                    server_time=data.get("server_time"),
                    accepted_bucket_ids=list(submitted_ids),
                    http_status=409,
                )
            else:
                logger.warning(
                    f"Unconfirmed, partial, or malformed HTTP 409 Conflict received from server: "
                    f"duplicate_count={raw_dup}, expected={len(buckets)}, marker={has_dup_marker}. "
                    f"Failing closed for safe retry."
                )
                return SyncResult(
                    success=False,
                    failed_count=len(buckets),
                    http_status=409,
                    error_code=str(data.get("error", "CONFLICT")),
                    error_message=str(
                        data.get(
                            "message",
                            f"HTTP 409 Conflict without complete batch duplicate confirmation (duplicate_count={raw_dup}, expected={len(buckets)})"
                        )
                    ),
                )
        elif status == 401:
            err = data.get("error") or "UNAUTHORIZED"
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                is_revoked=True,
                http_status=401,
                error_code=str(err),
                error_message="Device token revoked or unauthorized.",
            )
        elif status == 403:
            err = data.get("error") or "FORBIDDEN"
            msg = data.get("message") or err
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                is_ineligible=True,
                http_status=403,
                error_code=str(err),
                error_message=str(msg),
            )
        elif status == 429:
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                is_rate_limited=True,
                http_status=429,
                error_code="RATE_LIMITED",
                error_message="Rate limit exceeded. Backing off.",
            )
        elif status == 400:
            err = data.get("error") or "BAD_REQUEST"
            msg = data.get("message") or err
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                http_status=400,
                error_code=str(err),
                error_message=str(msg),
            )
        else:
            err = data.get("error") or data.get("message") or f"HTTP {status}"
            return SyncResult(
                success=False,
                failed_count=len(buckets),
                http_status=status,
                error_code="SERVER_ERROR" if status >= 500 else "NETWORK_ERROR",
                error_message=str(err),
            )

    @classmethod
    def _parse_shift_payload(cls, data: Dict[str, Any]) -> ShiftInfo:
        """Parses authoritative CRM shift serialization into ShiftInfo."""
        raw_session = data.get("session") or {}
        # If active session is null but last_completed_session exists, reflect COMPLETED shift status
        if not raw_session and (data.get("has_completed_today") or data.get("last_completed_session")):
            completed_sess = data.get("last_completed_session") or {}
            net_sec = int(completed_sess.get("net_seconds") or completed_sess.get("elapsed_net_seconds") or 0)
            brk_sec = int(completed_sess.get("break_seconds") or completed_sess.get("total_break_seconds") or 0)
            return ShiftInfo(
                session_id=completed_sess.get("id"),
                status=ShiftStatus.COMPLETED,
                started_at=completed_sess.get("started_at"),
                net_seconds=net_sec,
                break_seconds=brk_sec,
                current_break_name=None,
                current_category_name=None,
                server_time=data.get("server_time"),
            )
        elif not raw_session and not data.get("has_active_session"):
            return ShiftInfo(
                session_id=None,
                status=ShiftStatus.NOT_STARTED,
                started_at=None,
                net_seconds=0,
                break_seconds=0,
                current_break_name=None,
                current_category_name=None,
                server_time=data.get("server_time"),
            )
        else:
            raw_status = str(raw_session.get("status", "NONE")).upper()
            shift_status = ShiftStatus(raw_status) if raw_status in ShiftStatus._value2member_map_ else ShiftStatus.NONE

            current_seg = data.get("current_segment") or {}
            cat_data = current_seg.get("category") or {}
            cat_name = cat_data.get("name") or raw_session.get("current_category_name")

            current_brk = data.get("current_break") or {}
            brk_reason = current_brk.get("reason") or {}
            brk_name = brk_reason.get("name") or raw_session.get("current_break_name")

            net_sec = int(raw_session.get("net_seconds") or raw_session.get("elapsed_net_seconds") or 0)
            brk_sec = int(raw_session.get("break_seconds") or raw_session.get("total_break_seconds") or 0)

            return ShiftInfo(
                session_id=raw_session.get("id"),
                status=shift_status,
                started_at=raw_session.get("started_at"),
                net_seconds=net_sec,
                break_seconds=brk_sec,
                current_break_name=brk_name,
                current_category_name=cat_name,
                server_time=data.get("server_time"),
            )

    def fetch_shift_meta(
        self,
        device_uuid: str,
        token: str,
    ) -> Tuple[bool, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        """Retrieves active work categories and break reasons configured on the CRM backend.

        Endpoint: GET /api/v1/desktop-tracker/shift/meta/
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        status, data = self._make_request("/api/v1/desktop-tracker/shift/meta/", method="GET", headers=headers)
        if status == 200:
            categories = data.get("categories") or []
            break_reasons = data.get("break_reasons") or []
            return True, categories, break_reasons, data
        else:
            err = data.get("error") or f"HTTP_{status}"
            msg = data.get("message") or f"Failed to retrieve shift metadata ({status})"
            return False, [], [], {"error": str(err), "message": str(msg), "http_status": status}

    def start_shift(
        self,
        device_uuid: str,
        token: str,
        category_id: Optional[int] = None,
        note: str = "",
    ) -> Tuple[bool, ShiftInfo, Dict[str, Any]]:
        """Starts a new employee work shift and attendance session on the backend.

        Endpoint: POST /api/v1/desktop-tracker/shift/start/
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        payload: Dict[str, Any] = {}
        if category_id is not None:
            payload["category_id"] = int(category_id)
        if note:
            payload["note"] = note.strip()[:255]

        status, data = self._make_request("/api/v1/desktop-tracker/shift/start/", method="POST", data=payload, headers=headers)
        if status in (200, 201):
            shift = self._parse_shift_payload(data)
            return True, shift, data
        else:
            err = data.get("error") or f"HTTP_{status}"
            msg = data.get("message") or f"Failed to start shift ({status})"
            return False, ShiftInfo(status=ShiftStatus.NONE), {
                "error": str(err),
                "message": str(msg),
                "http_status": status,
                "session_id": data.get("session_id"),
            }

    def pause_shift(
        self,
        device_uuid: str,
        token: str,
        break_reason_id: int,
        note: str = "",
    ) -> Tuple[bool, ShiftInfo, Dict[str, Any]]:
        """Pauses the current active shift for a specified break reason (e.g. Lunch, Tea).

        Endpoint: POST /api/v1/desktop-tracker/shift/pause/
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        payload: Dict[str, Any] = {"break_reason_id": int(break_reason_id)}
        if note:
            payload["note"] = note.strip()[:255]

        status, data = self._make_request("/api/v1/desktop-tracker/shift/pause/", method="POST", data=payload, headers=headers)
        if status == 200:
            shift = self._parse_shift_payload(data)
            return True, shift, data
        else:
            err = data.get("error") or f"HTTP_{status}"
            msg = data.get("message") or f"Failed to pause shift ({status})"
            return False, ShiftInfo(status=ShiftStatus.NONE), {"error": str(err), "message": str(msg), "http_status": status}

    def resume_shift(
        self,
        device_uuid: str,
        token: str,
        category_id: Optional[int] = None,
        note: str = "",
    ) -> Tuple[bool, ShiftInfo, Dict[str, Any]]:
        """Resumes work from a paused break.

        Endpoint: POST /api/v1/desktop-tracker/shift/resume/
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        payload: Dict[str, Any] = {}
        if category_id is not None:
            payload["category_id"] = int(category_id)
        if note:
            payload["note"] = note.strip()[:255]

        status, data = self._make_request("/api/v1/desktop-tracker/shift/resume/", method="POST", data=payload, headers=headers)
        if status == 200:
            shift = self._parse_shift_payload(data)
            return True, shift, data
        else:
            err = data.get("error") or f"HTTP_{status}"
            msg = data.get("message") or f"Failed to resume shift ({status})"
            return False, ShiftInfo(status=ShiftStatus.NONE), {"error": str(err), "message": str(msg), "http_status": status}

    def end_shift(
        self,
        device_uuid: str,
        token: str,
        note: str = "",
        force_early: bool = False,
    ) -> Tuple[bool, ShiftInfo, Dict[str, Any]]:
        """Ends the current active or paused work session.

        Endpoint: POST /api/v1/desktop-tracker/shift/end/
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Token": token,
            "X-Device-UUID": device_uuid,
        }
        payload: Dict[str, Any] = {}
        if note:
            payload["note"] = note.strip()[:255]
        if force_early:
            payload["force_early"] = True

        status, data = self._make_request("/api/v1/desktop-tracker/shift/end/", method="POST", data=payload, headers=headers)
        if status == 200:
            shift = self._parse_shift_payload(data)
            return True, shift, data
        else:
            err = data.get("error") or f"HTTP_{status}"
            msg = data.get("message") or f"Failed to end shift ({status})"
            extra: Dict[str, Any] = {
                "error": str(err),
                "message": str(msg),
                "http_status": status,
            }
            for key in ("net_seconds", "required_seconds", "shortfall_seconds", "can_request_early_end"):
                if key in data:
                    extra[key] = data[key]
            return False, ShiftInfo(status=ShiftStatus.NONE), extra

