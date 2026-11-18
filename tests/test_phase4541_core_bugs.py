"""Regression and integration tests for Phase 4.5.4.1: Core bugs and time handling.

Covers:
- BUG-01: Timeline interval local time conversion (UTC -> Workstation local time).
- BUG-02: Local calendar day start boundary calculation in Dashboard and Summary HTML.
- BUG-03: Settings propagation to live running components (syncer, state_machine, aggregator).
- BUG-04: Tray-triggered dashboard activation dispatched safely via pending activation mechanism.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.app import VaaniTrackerApp
from vaani_tracker.config import TrackerConfig
from vaani_tracker.constants import TrackerState, SyncStatus
from vaani_tracker.core.aggregator import ActivityAggregator
from vaani_tracker.core.state_machine import TrackingStateMachine
from vaani_tracker.core.syncer import BackgroundSyncer
from vaani_tracker.models import ActivityBucket, ShiftInfo
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.ui.dashboard import DashboardViewModel, VaaniDashboard
from vaani_tracker.ui.summary import generate_summary_html


class TestPhase4541CoreBugs(unittest.TestCase):
    """Verifies fixes for BUG-01, BUG-02, BUG-03, and BUG-04."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.config.auth_token = "test-token"
        self.config.registered_employee_id = 101
        self.config.registered_employee_name = "Agent Tester"
        self.queue = ActivityQueue(self.config.db_path)
        self.state_machine = TrackingStateMachine(
            idle_detector=MagicMock(),
            session_notifier=MagicMock(),
            power_notifier=MagicMock(),
            idle_threshold_seconds=60,
        )
        self.auth_manager = MagicMock()
        self.auth_manager.get_authenticated_employee_id.return_value = 101
        self.auth_manager.get_token.return_value = "test-token"
        self.syncer = MagicMock()
        self.syncer.api_client = MagicMock()
        self.syncer.api_client.backend_url = self.config.backend_url
        self._apps = []

    def tearDown(self):
        for app in getattr(self, "_apps", []):
            try:
                app.close_logging()
                app.stop()
            except Exception:
                pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_bug01_timeline_converts_utc_to_local_time(self):
        """BUG-01: Timeline intervals must display in local workstation time, not raw UTC."""
        # Insert a bucket with known UTC timestamp
        start_utc_dt = datetime(2026, 11, 12, 7, 0, 0, tzinfo=timezone.utc)
        end_utc_dt = datetime(2026, 11, 12, 7, 1, 0, tzinfo=timezone.utc)

        bucket = ActivityBucket(
            bucket_id="test-utc-timeline-bucket",
            device_uuid=self.config.device_uuid,
            employee_id=101,
            bucket_start=start_utc_dt.isoformat(),
            bucket_end=end_utc_dt.isoformat(),
            active_seconds=45,
            idle_seconds=15,
            state=TrackerState.ACTIVE,
        )
        self.queue.save_bucket(bucket, employee_id=101)

        vm = DashboardViewModel(
            config=self.config,
            state_machine=self.state_machine,
            syncer=self.syncer,
            queue=self.queue,
            auth_manager=self.auth_manager,
        )

        intervals = vm.get_timeline_intervals(limit=10)
        self.assertEqual(len(intervals), 1)

        expected_start = start_utc_dt.astimezone().strftime("%H:%M:%S")
        expected_end = end_utc_dt.astimezone().strftime("%H:%M:%S")
        expected_range = f"{expected_start} - {expected_end}"

        self.assertEqual(intervals[0]["time_range"], expected_range)

    def test_bug02_today_boundary_matches_local_midnight(self):
        """BUG-02: KPI metrics and HTML summary must calculate 'Today' starting from local midnight."""
        local_now = datetime.now().astimezone()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        expected_today_start_iso = local_midnight.astimezone(timezone.utc).isoformat()

        # Insert bucket occurring 10 minutes after local midnight
        bucket_time_utc = (local_midnight + timedelta(minutes=10)).astimezone(timezone.utc)
        bucket = ActivityBucket(
            bucket_id="test-early-morning-bucket",
            device_uuid=self.config.device_uuid,
            employee_id=101,
            bucket_start=bucket_time_utc.isoformat(),
            bucket_end=(bucket_time_utc + timedelta(seconds=60)).isoformat(),
            active_seconds=60,
            idle_seconds=0,
            state=TrackerState.ACTIVE,
        )
        self.queue.save_bucket(bucket, employee_id=101)

        vm = DashboardViewModel(
            config=self.config,
            state_machine=self.state_machine,
            syncer=self.syncer,
            queue=self.queue,
            auth_manager=self.auth_manager,
        )

        cards = vm.get_kpi_cards()
        active_card = next(c for c in cards if c["title"] == "Active Work Time")
        self.assertEqual(active_card["value"], "1m", "Must include bucket starting after local midnight")

        # HTML summary check
        html = generate_summary_html(self.config, self.state_machine, self.queue, auth_manager=self.auth_manager)
        self.assertIn("1m", html, "Summary HTML must reflect bucket recorded today in local time")

    def test_bug03_settings_propagation_to_running_components(self):
        """BUG-03: Saving settings in dashboard must update running syncer, state machine, and aggregator."""
        mock_root = MagicMock()
        mock_aggregator = MagicMock()
        mock_aggregator.track_apps = True

        dashboard = VaaniDashboard(
            config=self.config,
            state_machine=self.state_machine,
            syncer=self.syncer,
            queue=self.queue,
            auth_manager=self.auth_manager,
            root=mock_root,
            aggregator=mock_aggregator,
        )

        # Mock UI entry widgets
        dashboard.entry_url = MagicMock()
        dashboard.entry_url.get.return_value = "https://new.vaani.internal/api/"
        dashboard.entry_idle = MagicMock()
        dashboard.entry_idle.get.return_value = "120"
        dashboard.entry_sync = MagicMock()
        dashboard.entry_sync.get.return_value = "30"
        dashboard.var_track_apps = MagicMock()
        dashboard.var_track_apps.get.return_value = False
        dashboard.var_startup = MagicMock()
        dashboard.var_startup.get.return_value = False

        with patch("vaani_tracker.ui.dashboard.messagebox.showinfo"), \
             patch("vaani_tracker.ui.dashboard.set_run_on_startup"):
            dashboard._on_save_settings_click()

        # 1. Config disk persistence
        reloaded = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.assertEqual(reloaded.backend_url, "https://new.vaani.internal/api")
        self.assertEqual(reloaded.idle_threshold_seconds, 120)
        self.assertEqual(reloaded.sync_interval_seconds, 30)
        self.assertFalse(reloaded.track_apps)

        # 2. In-memory runtime components
        self.assertEqual(self.syncer.api_client.backend_url, "https://new.vaani.internal/api")
        self.assertEqual(self.state_machine.idle_threshold_seconds, 120)
        self.assertFalse(mock_aggregator.track_apps)
        self.syncer.trigger_immediate_sync.assert_called_once()

    def test_bug04_tray_activation_dispatches_via_pending_activation(self):
        """BUG-04: Tray-triggered dashboard activation must use thread-safe pending activation."""
        app = VaaniTrackerApp(config=self.config, headless=False)
        self._apps.append(app)
        app._running = True

        # Simulate call from tray background worker thread
        caller_thread_id = None
        def _simulate_tray_click():
            nonlocal caller_thread_id
            caller_thread_id = threading.get_ident()
            app.show_dashboard()

        t = threading.Thread(target=_simulate_tray_click)
        t.start()
        t.join(timeout=2.0)

        self.assertNotEqual(caller_thread_id, threading.main_thread().ident)
        # Must have recorded pending activation safely
        self.assertTrue(app._pending_activation)
        self.assertTrue(app.consume_pending_activation())
        self.assertFalse(app._pending_activation)


if __name__ == "__main__":
    unittest.main()
