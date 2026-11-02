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
from datetime import datetime, timezone
from typing import Dict, Any, List

DEFAULT_PORT = 8085


class MockServerState:
    """Thread-safe state container for the mock backend."""

    def __init__(self):
        self.lock = threading.Lock()
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.valid_tokens: Dict[str, str] = {}  # token -> device_uuid
        self.session_id: int = 101
        self.shift_status: str = "ACTIVE"
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.current_break_name: str = ""
        self.idle_threshold_seconds: int = 60
        self.track_apps: bool = True
        self.ingested_buckets: List[Dict[str, Any]] = []

    def current_utc(self) -> str:
        return datetime.now(timezone.utc).isoformat()


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

        if path == "/api/v1/desktop-tracker/status/":
            token = self.headers.get("X-Device-Token")
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    status_code = 401
                    resp = {"error": "INVALID_OR_REVOKED_TOKEN", "revoked": True}
                else:
                    status_code = 200
                    session_data = {
                        "id": SERVER_STATE.session_id if SERVER_STATE.shift_status != "NONE" else None,
                        "status": SERVER_STATE.shift_status,
                        "started_at": SERVER_STATE.started_at,
                        "net_seconds": 3600,
                        "break_seconds": 600,
                        "current_break_name": SERVER_STATE.current_break_name,
                    }
                    resp = {
                        "is_approved": True,
                        "session": session_data,
                        "tracking_policy": {
                            "idle_threshold_seconds": SERVER_STATE.idle_threshold_seconds,
                            "sync_interval_seconds": 60,
                            "track_apps": SERVER_STATE.track_apps,
                        },
                        "server_time": SERVER_STATE.current_utc(),
                    }
            # Network write performed outside state lock
            self._send_json(status_code, resp)

        elif path == "/mock/inspect":
            with SERVER_STATE.lock:
                data = {
                    "shift_status": SERVER_STATE.shift_status,
                    "session_id": SERVER_STATE.session_id,
                    "started_at": SERVER_STATE.started_at,
                    "current_break_name": SERVER_STATE.current_break_name,
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
            token = self.headers.get("X-Device-Token")
            with SERVER_STATE.lock:
                if not token or token not in SERVER_STATE.valid_tokens:
                    status_code = 401
                    resp = {"error": "INVALID_TOKEN", "revoked": True}
                else:
                    buckets = body.get("buckets", [])
                    ingested = 0
                    existing_ids = {b.get("bucket_id") for b in SERVER_STATE.ingested_buckets if b.get("bucket_id")}
                    for b in buckets:
                        b_id = b.get("bucket_id")
                        if b_id and b_id in existing_ids:
                            continue
                        SERVER_STATE.ingested_buckets.append(b)
                        if b_id:
                            existing_ids.add(b_id)
                        ingested += 1

                    status_code = 200
                    resp = {
                        "status": "SUCCESS",
                        "ingested_count": ingested,
                        "server_time": SERVER_STATE.current_utc(),
                    }
            # Network write performed outside state lock
            self._send_json(status_code, resp)

        # Developer simulation controls
        elif path == "/mock/shift/start":
            with SERVER_STATE.lock:
                SERVER_STATE.session_id += 1
                SERVER_STATE.shift_status = "ACTIVE"
                SERVER_STATE.started_at = SERVER_STATE.current_utc()
                SERVER_STATE.current_break_name = ""
                sess_id = SERVER_STATE.session_id
            self._send_json(200, {"message": "Shift started (ACTIVE)", "session_id": sess_id})

        elif path == "/mock/shift/break":
            reason = body.get("reason", "Lunch Break")
            with SERVER_STATE.lock:
                SERVER_STATE.shift_status = "PAUSED"
                SERVER_STATE.current_break_name = reason
            self._send_json(200, {"message": f"Shift paused on break: {reason}"})

        elif path == "/mock/shift/resume":
            with SERVER_STATE.lock:
                SERVER_STATE.shift_status = "ACTIVE"
                SERVER_STATE.current_break_name = ""
            self._send_json(200, {"message": "Shift resumed (ACTIVE)"})

        elif path == "/mock/shift/end":
            with SERVER_STATE.lock:
                SERVER_STATE.shift_status = "COMPLETED"
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
