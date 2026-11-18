"""Command-line entry point for Vaani Desktop Tracker."""

import argparse
import os
import platform
import sys
from pathlib import Path

# Ensure package root is in sys.path when executed directly as a script
if __package__ in (None, ""):
    _src_dir = str(Path(__file__).resolve().parent.parent)
    if _src_dir not in sys.path:
        sys.path.insert(0, _src_dir)

from vaani_tracker.app import VaaniTrackerApp
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import APP_VERSION
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.models import DeviceInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.ui.summary import show_activity_summary
from vaani_tracker.windows.win32 import enable_high_dpi_awareness


def parse_args():
    parser = argparse.ArgumentParser(
        prog="vaani-tracker",
        description="Vaani Windows Desktop Activity Tracker MVP",
    )
    parser.add_argument(
        "--register",
        metavar="PAIRING_CODE",
        help="Register/pair device with Vaani backend using a pairing code.",
    )
    parser.add_argument(
        "--backend",
        help="Vaani backend CRM base URL (e.g. http://127.0.0.1:8000 or https://vaaniapp.co.in).",
    )
    parser.add_argument(
        "--data-dir",
        help="Custom path for local database and configuration storage.",
    )
    parser.add_argument(
        "--idle-threshold",
        type=int,
        help="Idle timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without system tray icon (e.g. background service or test runner).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Open activity summary in web browser and exit.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print registration and device info and exit.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable detailed debug logging.",
    )
    return parser.parse_args()


def main():
    enable_high_dpi_awareness()
    args = parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    config = TrackerConfig.load(custom_data_dir=data_dir)

    if args.backend:
        config.backend_url = args.backend.rstrip("/")
    if args.idle_threshold:
        config.idle_threshold_seconds = args.idle_threshold
    if args.verbose:
        config.log_level = "DEBUG"

    # Status check mode
    if args.status:
        queue = ActivityQueue(config.db_path)
        auth = DeviceAuthManager(config, queue)
        auth_id = auth.get_authenticated_employee_id()
        is_auth = auth.is_authenticated()
        print("=== Vaani Desktop Tracker Status ===")
        print(f"Device UUID:      {config.device_uuid}")
        print(f"Backend URL:      {config.backend_url}")
        print(f"Authenticated:    {'YES' if is_auth else 'NO'}")
        if auth_id is not None:
            emp = auth.get_bound_employee()
            print(f"Bound Employee:   {emp.get('employee_name') or 'None'} (ID: {auth_id})")
            print(f"Idle Threshold:   {config.idle_threshold_seconds} seconds")
            print(f"Pending Buckets:  {queue.get_pending_count(employee_id=auth_id)}")
        else:
            print("Bound Employee:   None (Unpaired)")
            print(f"Idle Threshold:   {config.idle_threshold_seconds} seconds")
            print("Pending Buckets:  0 (No active employee binding)")
        print(f"Database Path:    {config.db_path}")
        return 0

    # Device Registration Mode
    if args.register:
        queue = ActivityQueue(config.db_path)
        auth = DeviceAuthManager(config, queue)
        client = VaaniApiClient(config.backend_url)

        device_info = DeviceInfo(
            device_uuid=config.device_uuid,
            machine_name=platform.node(),
            os_version=f"{platform.system()} {platform.release()} ({platform.version()})",
            client_version=APP_VERSION,
        )

        print(f"Registering device '{device_info.machine_name}' with code '{args.register}'...")
        success, resp = client.register_device(device_info, pairing_code=args.register)

        if success:
            token = resp.get("auth_token") or resp.get("token")
            emp_id = resp.get("employee_id")
            emp_name = resp.get("employee_name") or "Vaani Employee"
            emp_email = resp.get("employee_email")

            bind_ok = auth.register_and_bind(
                token=token,
                employee_id=emp_id,
                employee_name=emp_name,
                employee_email=emp_email,
            )

            if bind_ok:
                print(f"✓ Registration successful! Device bound to {emp_name} (ID: {emp_id}).")
                return 0
            else:
                print("✗ Registration failed: Could not securely store credentials or bind employee.")
                return 1
        else:
            print(f"✗ Registration failed: {resp.get('error') or resp.get('message') or resp}")
            return 1

    # App run mode
    app = VaaniTrackerApp(config=config, headless=args.headless)

    # Summary mode
    if args.summary:
        show_activity_summary(config, app.state_machine, app.queue, auth_manager=app.auth_manager)
        return 0

    started = app.start()
    if not started and getattr(app, "activated_existing", False):
        return 0
    return 0 if started else 1


if __name__ == "__main__":
    sys.exit(main())

