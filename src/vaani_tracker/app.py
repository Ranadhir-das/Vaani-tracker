"""Application orchestrator tying together all tracker subsystems."""

import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

from .config import TrackerConfig
from .constants import TrackerState
from .client.api import VaaniApiClient
from .client.auth import DeviceAuthManager
from .core.aggregator import ActivityAggregator
from .core.state_machine import TrackingStateMachine
from .core.syncer import BackgroundSyncer
from .models import DeviceInfo
from .storage.queue import ActivityQueue
from .ui.tray import VaaniTrayApp
from .windows.apps import WindowsProcessIdentifier
from .windows.idle import WindowsIdleDetector
from .windows.power import WindowsPowerNotifier
from .windows.session import WindowsSessionNotifier

logger = logging.getLogger(__name__)


class SingleInstanceLock:
    """Ensures only a single instance of the tracker runs per user session."""

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fd = None

    def acquire(self) -> bool:
        try:
            # Open exclusively for writing; fail if already exists or locked
            if os.name == "nt":
                if os.path.exists(self.lock_path):
                    try:
                        os.remove(self.lock_path)
                    except OSError:
                        return False
                self._fd = open(self.lock_path, "w")
                self._fd.write(str(os.getpid()))
                self._fd.flush()
                return True
            else:
                import fcntl
                self._fd = open(self.lock_path, "w")
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
        except Exception:
            return False

    def release(self) -> None:
        try:
            if self._fd:
                self._fd.close()
            if os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except Exception:
            pass


class VaaniTrackerApp:
    """Main application lifecycle controller."""

    def __init__(self, config: Optional[TrackerConfig] = None, headless: bool = False):
        self.config = config or TrackerConfig.load()
        self.headless = headless

        self._running = False
        self._tick_thread: Optional[threading.Thread] = None
        self._lock = SingleInstanceLock(str(self.config.data_dir / "tracker.lock"))

        # Configure logging
        self._setup_logging()

        # Initialize storage
        self.queue = ActivityQueue(self.config.db_path)

        # Initialize authentication & networking
        self.auth_manager = DeviceAuthManager(self.config, self.queue)
        self.api_client = VaaniApiClient(self.config.backend_url)

        # Initialize Windows hardware sensors
        self.idle_detector = WindowsIdleDetector()
        self.session_notifier = WindowsSessionNotifier()
        self.power_notifier = WindowsPowerNotifier()
        self.process_identifier = WindowsProcessIdentifier()

        # Initialize core logic
        self.state_machine = TrackingStateMachine(
            idle_detector=self.idle_detector,
            session_notifier=self.session_notifier,
            power_notifier=self.power_notifier,
            idle_threshold_seconds=self.config.idle_threshold_seconds,
        )

        self.aggregator = ActivityAggregator(
            state_machine=self.state_machine,
            process_identifier=self.process_identifier,
            queue=self.queue,
            device_uuid=self.config.device_uuid,
            bucket_duration_seconds=60,
            track_apps=self.config.track_apps,
        )

        self.syncer = BackgroundSyncer(
            config=self.config,
            api_client=self.api_client,
            queue=self.queue,
            auth_manager=self.auth_manager,
            state_machine=self.state_machine,
        )

        # Initialize UI
        self.tray = VaaniTrayApp(
            config=self.config,
            state_machine=self.state_machine,
            syncer=self.syncer,
            queue=self.queue,
            on_exit=self.stop,
        )

    def _setup_logging(self) -> None:
        """Sets up rotating file and console logging."""
        level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        root = logging.getLogger()
        root.setLevel(level)

        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)

        # File handler
        try:
            file_handler = logging.FileHandler(str(self.config.log_path), encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception:
            pass

    def start(self) -> bool:
        """Starts all tracking, sensing, and synchronization workers."""
        if not self._lock.acquire():
            logger.warning("Another instance of Vaani Desktop Tracker is already running.")
            return False

        logger.info(f"Starting Vaani Desktop Tracker (Device UUID: {self.config.device_uuid})")
        logger.info(f"Target Backend: {self.config.backend_url}")

        self._running = True

        # Start Windows event listeners
        self.session_notifier.start()
        self.power_notifier.start()

        # Start background sync & heartbeat
        self.syncer.start()

        # Start 1-second aggregator ticking thread
        self._tick_thread = threading.Thread(
            target=self._run_tick_loop,
            daemon=True,
            name="VaaniAggregatorTickThread",
        )
        self._tick_thread.start()

        # Register OS signal handlers for graceful exit
        try:
            signal.signal(signal.SIGINT, lambda *_: self.stop())
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
        except Exception:
            pass

        # Start tray UI (blocking on main thread if not headless)
        if not self.headless:
            self.tray.run()
        else:
            logger.info("Running in headless mode (no system tray).")

        return True

    def stop(self) -> None:
        """Cleanly halts all workers and releases resources."""
        if not self._running:
            return

        logger.info("Stopping Vaani Desktop Tracker...")
        self._running = False

        # Close and save any in-progress bucket
        try:
            self.aggregator.close_current_bucket()
        except Exception as e:
            logger.error(f"Error finalizing bucket on stop: {e}")

        # Stop workers
        self.syncer.stop()
        self.session_notifier.stop()
        self.power_notifier.stop()
        self.tray.stop()

        if self._tick_thread and self._tick_thread.is_alive():
            self._tick_thread.join(timeout=2.0)

        self._lock.release()
        logger.info("Vaani Desktop Tracker stopped.")

    def _run_tick_loop(self) -> None:
        """Ticks the activity aggregator every 1 second."""
        while self._running:
            start_mono = time.monotonic()
            try:
                self.aggregator.tick(elapsed_seconds=1.0)
            except Exception as e:
                logger.error(f"Error in aggregator tick: {e}", exc_info=True)

            elapsed = time.monotonic() - start_mono
            sleep_time = max(0.0, 1.0 - elapsed)
            time.sleep(sleep_time)

