"""Command-line entry point for Vaani Desktop Tracker."""

import argparse
import os
import platform
import sys
from pathlib import Path

from .app import VaaniTrackerApp
from .config import TrackerConfig
from .client.api import VaaniApiClient
from .client.auth import DeviceAuthManager
from .models import DeviceInfo
from .storage.queue import ActivityQueue
from .ui.summary import show_activity_summary


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
        emp = auth.get_bound_employee()
        print("=== Vaani Desktop Tracker Status ===")
        print(f"Device UUID:      {config.device_uuid}")
        print(f"Backend URL:      {config.backend_url}")
        print(f"Authenticated:    {'YES' if auth.is_authenticated() else 'NO'}")
        print(f"Bound Employee:   {emp.get('employee_name') or 'None'} (ID: {emp.get('employee_id') or 'None'})")
        print(f"Idle Threshold:   {config.idle_threshold_seconds} seconds")
        print(f"Pending Buckets:  {queue.get_pending_count()}")
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
            client_version="1.0.0",
        )

        print(f"Registering device '{device_info.machine_name}' with code '{args.register}'...")
        success, resp = client.register_device(device_info, pairing_code=args.register)

        if success:
            token = resp.get("auth_token") or resp.get("token")
            emp_id = resp.get("employee_id")
            emp_name = resp.get("employee_name") or "Vaani Employee"

            if token:
                auth.set_token(token)
            if emp_id:
                auth.bind_employee(emp_id, emp_name)

            print(f"✓ Registration successful! Device bound to {emp_name} (ID: {emp_id}).")
            config.save()
            return 0
        else:
            print(f"✗ Registration failed: {resp.get('error') or resp.get('message') or resp}")
            return 1

    # App run mode
    app = VaaniTrackerApp(config=config, headless=args.headless)

    # Summary mode
    if args.summary:
        show_activity_summary(config, app.state_machine, app.queue)
        return 0

    started = app.start()
    return 0 if started else 1


if __name__ == "__main__":
    sys.exit(main())

