"""Lightweight Mock Vaani Backend Server for Desktop Tracker Development & Testing.

Implements the audited Desktop Tracker REST API contracts with concurrent multithreading:
- POST /api/v1/desktop-tracker/register/
- GET  /api/v1/desktop-tracker/status/
- POST /api/v1/desktop-tracker/sync/

Also exposes developer simulation controls:
- POST /mock/shift/start   - Sets shift status to ACTIVE
- POST /mock/shift/break   - Sets shift status to PAUSED (Break)
- POST /mock/shift/resume  - Resumes shift to ACTIVE
- POST /mock/shift/end     - Sets shift status to COMPLETED
- POST /mock/token/revoke  - Simulates token revocation
- GET  /mock/inspect       - Returns all ingested buckets and current server state
"""

import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

DEFAULT_PORT = 8085


class MockServerState:
    """Thread-safe state container for the mock backend."""

    def __init__(self):
        self.lock = threading.Lock()
        self.categories = [
            {"id": 1, "name": "Calling / Inbound", "slug": "calling", "color_hex": "#4F46E5"},
            {"id": 2, "name": "Back Office / Ops", "slug": "backoffice", "color_hex": "#059669"},
            {"id": 3, "name": "Training / Review", "slug": "training", "color_hex": "#D97706"},
        ]
        self.break_reasons = [
            {"id": 1, "name": "Lunch", "code": "LUNCH", "warning_threshold_minutes": 45, "requires_note": False},
            {"id": 2, "name": "Tea Break", "code": "TEA_BREAK", "warning_threshold_minutes": 15, "requires_note": False},
            {"id": 3, "name": "Personal / Emergency", "code": "PERSONAL", "warning_threshold_minutes": 15, "requires_note": True},
        ]
        self.reset()

    def reset(self) -> None:
        """Resets mock server to a clean fresh-start state with no active, paused, or completed shift."""
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.valid_tokens: Dict[str, str] = {}  # token -> device_uuid
        self._last_session_seq: int = 100
        self.session_id: Optional[int] = None
        self.shift_status: str = "NOT_STARTED"
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.break_started_mono: Optional[float] = None
        self.accumulated_break_seconds: float = 0.0
        self.current_break_id: Optional[int] = None
        self.current_break_name: str = ""
        self.current_break_note: str = ""
        self.pause_requests: List[Dict[str, Any]] = []
        self.current_category_id: Optional[int] = None
        self.current_category_name: str = ""
        self.idle_threshold_seconds: int = 60
        self.track_apps: bool = True
        self.ingested_buckets: List[Dict[str, Any]] = []

    def allocate_next_session_id(self) -> int:
        """Allocates the next sequential session ID starting at 101 on fresh state."""
        base_id = self.session_id if self.session_id is not None else self._last_session_seq
        self.session_id = int(base_id) + 1
        self._last_session_seq = self.session_id
        return self.session_id

    def current_utc(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_break_seconds(self) -> int:
        if self.shift_status not in ("ACTIVE", "PAUSED", "COMPLETED"):
            return 0
        sec = self.accumulated_break_seconds
        if self.shift_status == "PAUSED" and self.break_started_mono is not None:
            sec += max(0.0, time.monotonic() - self.break_started_mono)
        return int(sec)

    def get_gross_seconds(self) -> int:
        if self.shift_status not in ("ACTIVE", "PAUSED", "COMPLETED") or not self.started_at:
            return 0
        try:
            st = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            if self.shift_status == "COMPLETED" and self.ended_at:
                end_dt = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
            else:
                end_dt = datetime.now(timezone.utc)
            return max(0, int((end_dt - st).total_seconds()))
        except Exception:
            return 0

    def get_net_seconds(self) -> int:
        if self.shift_status not in ("ACTIVE", "PAUSED", "COMPLETED"):
            return 0
        return max(0, self.get_gross_seconds() - self.get_break_seconds())

    def serialize_shift_state(self) -> Dict[str, Any]:
        brk_sec = self.get_break_seconds()
        gross_sec = self.get_gross_seconds()
        net_sec = self.get_net_seconds()
        is_active = (self.shift_status in ("ACTIVE", "PAUSED")) and (self.session_id is not None)
        is_completed = (self.shift_status == "COMPLETED") and (self.session_id is not None)
        started_iso = self.started_at or self.current_utc()

        session_data = None
        if is_active:
            session_data = {
                "id": self.session_id,
                "status": self.shift_status,
                "started_at": started_iso,
                "elapsed_gross_seconds": gross_sec,
                "break_seconds": brk_sec,
                "net_seconds": net_sec,
                "current_break_name": self.current_break_name if self.shift_status == "PAUSED" else "",
                "current_category_name": self.current_category_name or "Calling / Inbound",
            }

        last_completed = None
        if is_completed:
            last_completed = {
                "id": self.session_id,
                "status": "COMPLETED",
                "started_at": started_iso,
                "ended_at": self.ended_at or self.current_utc(),
                "elapsed_gross_seconds": gross_sec,
                "break_seconds": brk_sec,
                "net_seconds": net_sec,
            }

        cat_id = self.current_category_id or 1
        cat_name = self.current_category_name or "Calling / Inbound"

        return {
            "has_active_session": is_active,
            "has_completed_today": is_completed,
            "session": session_data,
            "current_segment": {
                "category": {
                    "id": cat_id,
                    "name": cat_name,
                    "slug": "calling",
                    "color_hex": "#4F46E5",
                },
                "started_at": started_iso,
            } if is_active else None,
            "current_break": {
                "id": 1,
                "reason": {
                    "id": self.current_break_id or 1,
                    "name": self.current_break_name or "Break",
                    "code": "BREAK",
                },
                "started_at": self.current_utc(),
                "elapsed_seconds": brk_sec,
            } if (is_active and self.shift_status == "PAUSED") else None,
            "last_completed_session": last_completed,
            "server_time": self.current_utc(),
        }


SERVER_STATE = MockServerState()


class VaaniMockThreadingServer(ThreadingHTTPServer):
    """Threaded HTTP server using daemon request threads for concurrency and clean shutdown."""
    daemon_threads = True
    allow_reuse_address = True


class MockVaaniHandler(BaseHTTPRequestHandler):
    """Concurrent HTTP request handler implementing the Vaani desktop tracker API."""

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        """Encodes and writes JSON response over network outside shared state lock."""
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> Dict[str, Any]:
        """Reads JSON request body outside shared state lock."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _extract_token(self) -> Optional[str]:
        """Extracts token from either X-Device-Token or Authorization: Bearer header."""
        t = self.headers.get("X-Device-Token")
        if t:
            return t.strip()
        auth = self.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def log_request(self, code="-", size="-"):
        """Concise endpoint and status logging without leaking tokens or private headers."""
        path = getattr(self, "path", "-").split("?")[0]
        method = getattr(self, "command", "-")
        sys.stderr.write(f"[MockServer] {method} {path} -> {code}\n")

    def log_error(self, format, *args):
        """Logs concise error line without tokens."""
        sys.stderr.write(f"[MockServer ERROR] {format % args}\n")

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/v1/desktop-tracker/shift/meta/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    status_code = 401
                    resp = {"error": "INVALID_TOKEN", "message": "Unauthorized"}
                else:
                    status_code = 200
                    resp = {
                        "categories": SERVER_STATE.categories,
                        "break_reasons": SERVER_STATE.break_reasons,
                    }
            self._send_json(status_code, resp)

        elif path == "/api/v1/desktop-tracker/status/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    status_code = 401
                    resp = {"error": "INVALID_OR_REVOKED_TOKEN", "revoked": True}
                else:
                    status_code = 200
                    shift_data = SERVER_STATE.serialize_shift_state()
                    resp = {
                        "is_approved": True,
                        **shift_data,
                        "tracking_policy": {
                            "idle_threshold_seconds": SERVER_STATE.idle_threshold_seconds,
                            "sync_interval_seconds": 60,
                            "track_apps": SERVER_STATE.track_apps,
                        },
                    }
            # Network write performed outside state lock
            self._send_json(status_code, resp)

        elif path == "/mock/inspect":
            with SERVER_STATE.lock:
                data = {
                    "shift_status": SERVER_STATE.shift_status,
                    "session_id": SERVER_STATE.session_id,
                    "started_at": SERVER_STATE.started_at,
                    "current_break_id": SERVER_STATE.current_break_id,
                    "current_break_name": SERVER_STATE.current_break_name,
                    "current_break_note": SERVER_STATE.current_break_note,
                    "pause_requests": list(SERVER_STATE.pause_requests),
                    "ingested_buckets_count": len(SERVER_STATE.ingested_buckets),
                    "ingested_buckets": list(SERVER_STATE.ingested_buckets),
                    "devices": dict(SERVER_STATE.devices),
                }
            # Network write performed outside state lock
            self._send_json(200, data)

        else:
            self._send_json(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json_body()

        if path == "/api/v1/desktop-tracker/register/":
            dev_uuid = body.get("device_uuid")
            if not dev_uuid:
                self._send_json(400, {"error": "MISSING_DEVICE_UUID", "message": "device_uuid is required."})
                return

            machine = body.get("machine_name", "UNKNOWN-PC")
            token = f"vdt_mock_token_{dev_uuid[:8]}"

            with SERVER_STATE.lock:
                SERVER_STATE.devices[dev_uuid] = {
                    "machine_name": machine,
                    "token": token,
                    "registered_at": SERVER_STATE.current_utc(),
                }
                SERVER_STATE.valid_tokens[token] = dev_uuid

            resp = {
                "status": "APPROVED",
                "auth_token": token,
                "device_id": dev_uuid,
                "employee_id": 42,
                "employee_name": "Tuhi Mukherjee (Pilot)",
                "employee_email": "tuhi@vaani.internal",
                "message": "Device paired successfully.",
            }
            # Network write performed outside state lock
            self._send_json(201, resp)

        elif path == "/api/v1/desktop-tracker/sync/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    status_code = 401
                    resp = {"error": "INVALID_TOKEN", "revoked": True}
                else:
                    buckets = body.get("buckets", [])
                    ingested = 0
                    duplicates = 0
                    existing_ids = {b.get("bucket_id") for b in SERVER_STATE.ingested_buckets if b.get("bucket_id")}
                    for b in buckets:
                        b_id = b.get("bucket_id")
                        if b_id and b_id in existing_ids:
                            duplicates += 1
                            continue
                        SERVER_STATE.ingested_buckets.append(b)
                        if b_id:
                            existing_ids.add(b_id)
                        ingested += 1

                    status_code = 200
                    resp = {
                        "status": "SUCCESS",
                        "ingested_count": ingested,
                        "duplicate_count": duplicates,
                        "server_time": SERVER_STATE.current_utc(),
                    }
            # Network write performed outside state lock
            self._send_json(status_code, resp)

        elif path == "/api/v1/desktop-tracker/shift/start/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    self._send_json(401, {"error": "INVALID_TOKEN", "message": "Unauthorized"})
                    return
                if SERVER_STATE.shift_status in ("ACTIVE", "PAUSED"):
                    self._send_json(409, {
                        "error": "ACTIVE_SESSION_EXISTS",
                        "message": f"User already has an active or paused session (id={SERVER_STATE.session_id}).",
                        "session_id": SERVER_STATE.session_id,
                    })
                    return
                cat_id = body.get("category_id")
                if cat_id is not None:
                    matched = next((c for c in SERVER_STATE.categories if c["id"] == int(cat_id)), None)
                    if not matched:
                        self._send_json(400, {"error": "INVALID_CATEGORY", "message": "Invalid category."})
                        return
                    SERVER_STATE.current_category_id = matched["id"]
                    SERVER_STATE.current_category_name = matched["name"]
                else:
                    SERVER_STATE.current_category_id = 1
                    SERVER_STATE.current_category_name = SERVER_STATE.categories[0]["name"]

                SERVER_STATE.allocate_next_session_id()
                SERVER_STATE.shift_status = "ACTIVE"
                SERVER_STATE.started_at = SERVER_STATE.current_utc()
                SERVER_STATE.ended_at = None
                SERVER_STATE.break_started_mono = None
                SERVER_STATE.accumulated_break_seconds = 0.0
                SERVER_STATE.current_break_id = None
                SERVER_STATE.current_break_name = ""
                resp = {"status": "SHIFT_STARTED", **SERVER_STATE.serialize_shift_state()}
            self._send_json(200, resp)

        elif path == "/api/v1/desktop-tracker/shift/pause/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    self._send_json(401, {"error": "INVALID_TOKEN", "message": "Unauthorized"})
                    return
                if SERVER_STATE.shift_status == "PAUSED":
                    self._send_json(409, {"error": "ALREADY_PAUSED", "message": "Your shift is already paused on break."})
                    return
                if SERVER_STATE.shift_status != "ACTIVE":
                    self._send_json(404, {"error": "NO_ACTIVE_SESSION", "message": "No active work session to pause."})
                    return
                brk_id = body.get("break_reason_id")
                if not brk_id:
                    self._send_json(400, {"error": "MISSING_BREAK_REASON", "message": "A break reason must be selected."})
                    return
                matched = next((b for b in SERVER_STATE.break_reasons if b["id"] == int(brk_id)), None)
                if not matched:
                    self._send_json(400, {"error": "INVALID_BREAK_REASON", "message": "Invalid break reason selected."})
                    return
                note = (body.get("note") or "").strip()
                if matched.get("requires_note") and not note:
                    self._send_json(400, {"error": "NOTE_REQUIRED", "message": "Please enter a short note for this break reason."})
                    return
                SERVER_STATE.shift_status = "PAUSED"
                SERVER_STATE.break_started_mono = time.monotonic()
                SERVER_STATE.current_break_id = matched["id"]
                SERVER_STATE.current_break_name = matched["name"]
                SERVER_STATE.current_break_note = note
                SERVER_STATE.pause_requests.append(
                    {
                        "break_reason_id": matched["id"],
                        "break_name": matched["name"],
                        "note": note,
                    }
                )
                resp = {"status": "SHIFT_PAUSED", **SERVER_STATE.serialize_shift_state()}
            self._send_json(200, resp)

        elif path == "/api/v1/desktop-tracker/shift/resume/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    self._send_json(401, {"error": "INVALID_TOKEN", "message": "Unauthorized"})
                    return
                if SERVER_STATE.shift_status == "ACTIVE":
                    self._send_json(409, {"error": "ALREADY_ACTIVE", "message": "Your shift is already active."})
                    return
                if SERVER_STATE.shift_status != "PAUSED":
                    self._send_json(404, {"error": "NO_PAUSED_SESSION", "message": "No paused work session to resume."})
                    return
                cat_id = body.get("category_id")
                if cat_id is not None:
                    matched = next((c for c in SERVER_STATE.categories if c["id"] == int(cat_id)), None)
                    if not matched:
                        self._send_json(400, {"error": "INVALID_CATEGORY", "message": "Invalid category."})
                        return
                    SERVER_STATE.current_category_id = matched["id"]
                    SERVER_STATE.current_category_name = matched["name"]
                if SERVER_STATE.break_started_mono is not None:
                    SERVER_STATE.accumulated_break_seconds += max(0.0, time.monotonic() - SERVER_STATE.break_started_mono)
                    SERVER_STATE.break_started_mono = None
                SERVER_STATE.shift_status = "ACTIVE"
                SERVER_STATE.current_break_id = None
                SERVER_STATE.current_break_name = ""
                SERVER_STATE.current_break_note = ""
                resp = {"status": "SHIFT_RESUMED", **SERVER_STATE.serialize_shift_state()}
            self._send_json(200, resp)

        elif path == "/api/v1/desktop-tracker/shift/end/":
            token = self._extract_token()
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    self._send_json(401, {"error": "INVALID_TOKEN", "message": "Unauthorized"})
                    return
                if SERVER_STATE.shift_status not in ("ACTIVE", "PAUSED"):
                    self._send_json(404, {"error": "NO_SESSION_TO_END", "message": "No active or paused session to end."})
                    return
                if SERVER_STATE.shift_status == "PAUSED" and SERVER_STATE.break_started_mono is not None:
                    SERVER_STATE.accumulated_break_seconds += max(0.0, time.monotonic() - SERVER_STATE.break_started_mono)
                    SERVER_STATE.break_started_mono = None
                SERVER_STATE.shift_status = "COMPLETED"
                SERVER_STATE.ended_at = SERVER_STATE.current_utc()
                resp = {"status": "SHIFT_ENDED", **SERVER_STATE.serialize_shift_state()}
            self._send_json(200, resp)

        # Developer simulation controls
        elif path == "/mock/shift/start":
            with SERVER_STATE.lock:
                sess_id = SERVER_STATE.allocate_next_session_id()
                SERVER_STATE.shift_status = "ACTIVE"
                SERVER_STATE.started_at = SERVER_STATE.current_utc()
                SERVER_STATE.ended_at = None
                SERVER_STATE.break_started_mono = None
                SERVER_STATE.accumulated_break_seconds = 0.0
                SERVER_STATE.current_break_id = None
                SERVER_STATE.current_break_name = ""
                if not SERVER_STATE.current_category_id:
                    SERVER_STATE.current_category_id = 1
                    SERVER_STATE.current_category_name = SERVER_STATE.categories[0]["name"]
            self._send_json(200, {"message": "Shift started (ACTIVE)", "session_id": sess_id})

        elif path == "/mock/shift/break":
            reason = body.get("reason", "Lunch Break")
            with SERVER_STATE.lock:
                SERVER_STATE.shift_status = "PAUSED"
                SERVER_STATE.current_break_name = reason
                SERVER_STATE.break_started_mono = time.monotonic()
            self._send_json(200, {"message": f"Shift paused on break: {reason}"})

        elif path == "/mock/shift/resume":
            with SERVER_STATE.lock:
                if SERVER_STATE.break_started_mono is not None:
                    SERVER_STATE.accumulated_break_seconds += max(0.0, time.monotonic() - SERVER_STATE.break_started_mono)
                    SERVER_STATE.break_started_mono = None
                SERVER_STATE.shift_status = "ACTIVE"
                SERVER_STATE.current_break_id = None
                SERVER_STATE.current_break_name = ""
            self._send_json(200, {"message": "Shift resumed (ACTIVE)"})

        elif path == "/mock/shift/end":
            with SERVER_STATE.lock:
                if SERVER_STATE.shift_status == "PAUSED" and SERVER_STATE.break_started_mono is not None:
                    SERVER_STATE.accumulated_break_seconds += max(0.0, time.monotonic() - SERVER_STATE.break_started_mono)
                    SERVER_STATE.break_started_mono = None
                SERVER_STATE.shift_status = "COMPLETED"
                SERVER_STATE.ended_at = SERVER_STATE.current_utc()
            self._send_json(200, {"message": "Shift ended (COMPLETED)"})

        elif path == "/mock/token/revoke":
            with SERVER_STATE.lock:
                SERVER_STATE.valid_tokens.clear()
            self._send_json(200, {"message": "All tokens revoked."})

        else:
            self._send_json(404, {"error": "NOT_FOUND"})


def run_mock_server(port: int = DEFAULT_PORT) -> VaaniMockThreadingServer:
    """Starts the concurrent threaded mock server."""
    server = VaaniMockThreadingServer(("127.0.0.1", port), MockVaaniHandler)
    print(f"✓ Mock Vaani Backend listening at http://127.0.0.1:{port}")
    print("  Controls:")
    print(f"  - http://127.0.0.1:{port}/mock/inspect")
    print(f"  - POST http://127.0.0.1:{port}/mock/shift/start")
    print(f"  - POST http://127.0.0.1:{port}/mock/shift/break")
    print(f"  - POST http://127.0.0.1:{port}/mock/shift/resume")
    print(f"  - POST http://127.0.0.1:{port}/mock/shift/end")
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = run_mock_server(port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping mock server.")
        server.shutdown()
        server.server_close()
