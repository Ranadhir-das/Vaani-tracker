"""Thread-safe SQLite Write-Ahead Log (WAL) event queue."""

import json
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..constants import TrackerState, SyncStatus
from ..models import ActivityBucket, AppUsage, current_utc_iso


class ActivityQueue:
    """Persistent local SQLite activity queue with WAL mode and thread safety."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Returns a configured SQLite connection."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode; we manage transactions explicitly
        )
        conn.row_factory = sqlite3.Row
        # Enable WAL (Write-Ahead Logging) and normal synchronous mode for optimal durability & performance
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        """Creates the required tables and indexes if they do not exist."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS activity_buckets (
                        bucket_id TEXT PRIMARY KEY,
                        device_uuid TEXT NOT NULL,
                        session_id INTEGER,
                        bucket_start TEXT NOT NULL,
                        bucket_end TEXT NOT NULL,
                        active_seconds INTEGER NOT NULL,
                        idle_seconds INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        is_locked INTEGER NOT NULL,
                        is_sleeping INTEGER NOT NULL,
                        is_paused INTEGER NOT NULL,
                        apps_json TEXT NOT NULL,
                        sync_status TEXT NOT NULL,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        created_at TEXT NOT NULL
                    );
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_buckets_sync_status
                    ON activity_buckets(sync_status, created_at);
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_buckets_session
                    ON activity_buckets(session_id, bucket_start);
                """)
            finally:
                conn.close()

    def save_bucket(self, bucket: ActivityBucket) -> None:
        """Saves or updates an activity bucket in the local queue."""
        apps_json = json.dumps([app.to_dict() for app in bucket.app_usages])
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    INSERT INTO activity_buckets (
                        bucket_id, device_uuid, session_id, bucket_start, bucket_end,
                        active_seconds, idle_seconds, state, is_locked, is_sleeping,
                        is_paused, apps_json, sync_status, retry_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket_id) DO UPDATE SET
                        active_seconds = excluded.active_seconds,
                        idle_seconds = excluded.idle_seconds,
                        state = excluded.state,
                        is_locked = excluded.is_locked,
                        is_sleeping = excluded.is_sleeping,
                        is_paused = excluded.is_paused,
                        apps_json = excluded.apps_json,
                        sync_status = excluded.sync_status,
                        retry_count = excluded.retry_count;
                """, (
                    bucket.bucket_id,
                    bucket.device_uuid,
                    bucket.session_id,
                    bucket.bucket_start,
                    bucket.bucket_end,
                    bucket.active_seconds,
                    bucket.idle_seconds,
                    bucket.state.value if isinstance(bucket.state, TrackerState) else str(bucket.state),
                    1 if bucket.is_locked else 0,
                    1 if bucket.is_sleeping else 0,
                    1 if bucket.is_paused else 0,
                    apps_json,
                    bucket.sync_status.value if isinstance(bucket.sync_status, SyncStatus) else str(bucket.sync_status),
                    bucket.retry_count,
                    bucket.created_at,
                ))
            finally:
                conn.close()

    def get_pending_buckets(self, limit: int = 50) -> List[ActivityBucket]:
        """Retrieves a batch of pending activity buckets ordered by bucket_start."""
        buckets: List[ActivityBucket] = []
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("""
                    SELECT * FROM activity_buckets
                    WHERE sync_status = ? OR sync_status = ?
                    ORDER BY bucket_start ASC
                    LIMIT ?;
                """, (SyncStatus.PENDING.value, SyncStatus.FAILED.value, limit))

                for row in cursor.fetchall():
                    raw_apps = json.loads(row["apps_json"])
                    app_usages = [
                        AppUsage(process_name=a.get("process_name", ""), duration_seconds=int(a.get("duration_seconds", 0)))
                        for a in raw_apps
                    ]
                    bucket = ActivityBucket(
                        bucket_id=row["bucket_id"],
                        device_uuid=row["device_uuid"],
                        session_id=row["session_id"],
                        bucket_start=row["bucket_start"],
                        bucket_end=row["bucket_end"],
                        active_seconds=row["active_seconds"],
                        idle_seconds=row["idle_seconds"],
                        state=TrackerState(row["state"]) if row["state"] in TrackerState._value2member_map_ else TrackerState.STOPPED,
                        is_locked=bool(row["is_locked"]),
                        is_sleeping=bool(row["is_sleeping"]),
                        is_paused=bool(row["is_paused"]),
                        app_usages=app_usages,
                        sync_status=SyncStatus(row["sync_status"]) if row["sync_status"] in SyncStatus._value2member_map_ else SyncStatus.PENDING,
                        retry_count=row["retry_count"],
                        created_at=row["created_at"],
                    )
                    buckets.append(bucket)
            finally:
                conn.close()
        return buckets

    def mark_buckets_synced(self, bucket_ids: List[str]) -> None:
        """Marks a batch of buckets as successfully synced."""
        if not bucket_ids:
            return
        with self._lock:
            conn = self._get_connection()
            try:
                placeholders = ",".join("?" for _ in bucket_ids)
                conn.execute(f"""
                    UPDATE activity_buckets
                    SET sync_status = ?, last_error = NULL
                    WHERE bucket_id IN ({placeholders});
                """, [SyncStatus.SYNCED.value] + bucket_ids)
            finally:
                conn.close()

    def mark_buckets_failed(self, bucket_ids: List[str], error_message: str) -> None:
        """Increments retry count and marks buckets as FAILED for subsequent retry."""
        if not bucket_ids:
            return
        with self._lock:
            conn = self._get_connection()
            try:
                placeholders = ",".join("?" for _ in bucket_ids)
                conn.execute(f"""
                    UPDATE activity_buckets
                    SET sync_status = ?,
                        retry_count = retry_count + 1,
                        last_error = ?
                    WHERE bucket_id IN ({placeholders});
                """, [SyncStatus.FAILED.value, error_message[:255]] + bucket_ids)
            finally:
                conn.close()

    def get_summary_stats(self, since_iso: str) -> Dict[str, Any]:
        """Calculates aggregated activity summary since a given timestamp (e.g. shift start)."""
        stats: Dict[str, Any] = {
            "total_active_seconds": 0,
            "total_idle_seconds": 0,
            "total_bucket_count": 0,
            "app_breakdown": {},
        }
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("""
                    SELECT active_seconds, idle_seconds, apps_json
                    FROM activity_buckets
                    WHERE bucket_start >= ?;
                """, (since_iso,))

                for row in cursor.fetchall():
                    stats["total_active_seconds"] += row["active_seconds"]
                    stats["total_idle_seconds"] += row["idle_seconds"]
                    stats["total_bucket_count"] += 1
                    try:
                        apps = json.loads(row["apps_json"])
                        for a in apps:
                            proc = a.get("process_name", "unknown")
                            dur = int(a.get("duration_seconds", 0))
                            stats["app_breakdown"][proc] = stats["app_breakdown"].get(proc, 0) + dur
                    except Exception:
                        pass
            finally:
                conn.close()
        return stats

    def get_pending_count(self) -> int:
        """Returns count of buckets pending sync."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("""
                    SELECT COUNT(*) as cnt FROM activity_buckets
                    WHERE sync_status != ?;
                """, (SyncStatus.SYNCED.value,))
                row = cursor.fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()

    def set_meta(self, key: str, value: Any) -> None:
        """Saves a string/JSON value in metadata table."""
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    INSERT INTO meta (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at;
                """, (key, val_str, current_utc_iso()))
            finally:
                conn.close()

    def get_meta(self, key: str, default: Any = None) -> Any:
        """Retrieves a metadata value."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("SELECT value FROM meta WHERE key = ?;", (key,))
                row = cursor.fetchone()
                if not row:
                    return default
                val_str = row["value"]
                try:
                    return json.loads(val_str)
                except Exception:
                    return val_str
            finally:
                conn.close()

    def clear_meta(self) -> None:
        """Clears all metadata (used on logout/employee unpairing)."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("DELETE FROM meta;")
            finally:
                conn.close()

