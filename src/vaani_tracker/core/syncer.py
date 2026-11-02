"""Background worker for periodic heartbeat and queued batch synchronization."""

import logging
import threading
import time
from typing import Optional, Callable

from ..client.api import VaaniApiClient
from ..client.auth import DeviceAuthManager
from ..config import TrackerConfig
from ..models import ShiftInfo, SyncResult
from ..storage.queue import ActivityQueue
from .state_machine import TrackingStateMachine

logger = logging.getLogger(__name__)


class BackgroundSyncer:
    """Manages periodic heartbeat polling and batch synchronization of queued activity buckets."""

    def __init__(
        self,
        config: TrackerConfig,
        api_client: VaaniApiClient,
        queue: ActivityQueue,
        auth_manager: DeviceAuthManager,
        state_machine: TrackingStateMachine,
        on_status_change: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self.api_client = api_client
        self.queue = queue
        self.auth_manager = auth_manager
        self.state_machine = state_machine
        self.on_status_change = on_status_change

        self._running = False
        self._sync_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._sync_event = threading.Event()
        self._last_sync_result: Optional[SyncResult] = None
        self._consecutive_failures = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_sync_result(self) -> Optional[SyncResult]:
        return self._last_sync_result

    def start(self) -> None:
        """Starts background heartbeat and synchronization worker threads."""
        if self._running:
            return

        self._running = True
        self._sync_thread = threading.Thread(
            target=self._run_sync_loop,
            daemon=True,
            name="VaaniSyncWorker",
        )
        self._heartbeat_thread = threading.Thread(
            target=self._run_heartbeat_loop,
            daemon=True,
            name="VaaniHeartbeatWorker",
        )
        self._sync_thread.start()
        self._heartbeat_thread.start()
        logger.info("Background synchronization workers started.")

    def stop(self) -> None:
        """Stops background synchronization worker threads."""
        self._running = False
        self._sync_event.set()
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=3.0)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=3.0)
        logger.info("Background synchronization workers stopped.")

    def trigger_immediate_sync(self) -> None:
        """Wakes up the sync worker to immediately process queued buckets."""
        self._sync_event.set()

    def sync_pending_now(self) -> SyncResult:
        """Synchronizes pending buckets immediately in current thread."""
        token = self.auth_manager.get_token()
        if not token:
            return SyncResult(success=False, error_message="Device not authenticated.")

        pending_buckets = self.queue.get_pending_buckets(limit=50)
        if not pending_buckets:
            return SyncResult(success=True, synced_count=0)

        result = self.api_client.sync_buckets(
            device_uuid=self.config.device_uuid,
            token=token,
            buckets=pending_buckets,
        )

        self._last_sync_result = result

        if result.success:
            self._consecutive_failures = 0
            bucket_ids = [b.bucket_id for b in pending_buckets]
            self.queue.mark_buckets_synced(bucket_ids)
            logger.info(f"Successfully synced batch of {len(bucket_ids)} buckets to Vaani backend.")
            self.state_machine.set_connected(True)
        elif result.is_revoked:
            logger.warning("Device token revoked by server. Unpairing device.")
            self.auth_manager.clear_credentials()
            self.state_machine.update_shift_status(ShiftInfo())
        else:
            self._consecutive_failures += 1
            bucket_ids = [b.bucket_id for b in pending_buckets]
            err = result.error_message or "Unknown sync error"
            self.queue.mark_buckets_failed(bucket_ids, err)
            logger.warning(f"Batch sync failed (attempt {self._consecutive_failures}): {err}")

        if self.on_status_change:
            try:
                self.on_status_change()
            except Exception:
                pass

        return result

    def heartbeat_now(self) -> bool:
        """Polls backend for device approval, policy, and authoritative shift status."""
        token = self.auth_manager.get_token()
        if not token:
            return False

        online, shift_info, extra = self.api_client.fetch_status(
            device_uuid=self.config.device_uuid,
            token=token,
        )

        if online:
            self.state_machine.set_connected(True)
            self.state_machine.update_shift_status(shift_info)

            # Apply server tracking policy overrides if provided
            if "idle_threshold_seconds" in extra:
                new_threshold = int(extra["idle_threshold_seconds"])
                if new_threshold != self.state_machine.idle_threshold_seconds:
                    logger.info(f"Server updated idle threshold: {new_threshold}s")
                    self.state_machine.idle_threshold_seconds = new_threshold

            if self.on_status_change:
                try:
                    self.on_status_change()
                except Exception:
                    pass
            return True

        elif extra.get("revoked"):
            logger.warning("Heartbeat indicates device token is revoked. Clearing local credentials.")
            self.auth_manager.clear_credentials()
            self.state_machine.update_shift_status(ShiftInfo())
            return False

        else:
            logger.debug(f"Heartbeat failed: {extra.get('error', 'OFFLINE')}")
            self.state_machine.set_connected(False)
            return False

    def _run_heartbeat_loop(self) -> None:
        """Worker loop polling backend status periodically."""
        # Initial heartbeat immediately on startup
        self.heartbeat_now()

        while self._running:
            try:
                self.heartbeat_now()
            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}", exc_info=True)

            # Wait heartbeat interval (default 30s)
            time.sleep(self.config.heartbeat_interval_seconds)

    def _run_sync_loop(self) -> None:
        """Worker loop uploading queued buckets periodically with exponential backoff on failure."""
        while self._running:
            try:
                self.sync_pending_now()
            except Exception as e:
                logger.error(f"Error in sync loop: {e}", exc_info=True)

            # Calculate sleep duration (exponential backoff if failing: 60s, 120s, max 300s)
            backoff = min(300, self.config.sync_interval_seconds * (2 ** min(self._consecutive_failures, 3)))
            self._sync_event.wait(timeout=backoff)
            self._sync_event.clear()

