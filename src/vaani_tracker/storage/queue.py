"""Thread-safe SQLite Write-Ahead Log (WAL) event queue with employee isolation and migrations."""

from datetime import datetime, timezone
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..constants import (
    TrackerState,
    SyncStatus,
    MAX_RETRY_COUNT,
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
)
from ..models import ActivityBucket, AppUsage, current_utc_iso

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2


def derive_collision_bucket_id(original_bucket_id: str, employee_id: int) -> str:
    """Derives a deterministic, RFC 4122-compliant UUIDv5 for cross-employee collision resolution.

    Ensures that resolved collision IDs always satisfy backend Django UUIDField validation
    and remain 100% deterministic across retries and restarts.
    """
    canonical_input = f"vaani:bucket:{original_bucket_id}:emp:{employee_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, canonical_input))


class ActivityQueue:
    """Persistent local SQLite activity queue with WAL mode, thread safety, and employee isolation."""

    def __init__(self, db_path: Path, busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS):
        self.db_path = Path(db_path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        self.last_quarantined_db_path: Optional[Path] = None
        self.last_quarantine_reason: Optional[str] = None
        self._init_db()

    def _quarantine_corrupt_db(self, reason: str) -> Optional[Path]:
        """Moves a corrupt or zero-byte database and any associated -wal/-shm files aside safely.

        Never overwrites existing quarantine files and never logs secrets.
        """
        ts_base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = ts_base
        counter = 1
        while self.db_path.with_name(f"{self.db_path.name}.corrupt.{suffix}").exists():
            suffix = f"{ts_base}_{counter}"
            counter += 1

        quarantined_db = self.db_path.with_name(f"{self.db_path.name}.corrupt.{suffix}")
        moved_any = False

        for ext in ("", "-wal", "-shm"):
            src = Path(f"{self.db_path}{ext}")
            if src.exists():
                dst = self.db_path.with_name(f"{self.db_path.name}{ext}.corrupt.{suffix}")
                try:
                    shutil.move(str(src), str(dst))
                    moved_any = True
                except OSError as move_err:
                    logger.error(f"Failed to quarantine corrupt SQLite artifact {src.name}: {move_err}")

        if moved_any:
            self.last_quarantined_db_path = quarantined_db
            self.last_quarantine_reason = reason
            logger.error(
                f"Quarantined corrupt SQLite queue database '{self.db_path.name}' "
                f"to '{quarantined_db.name}' (reason: {reason}). Reinitializing clean database."
            )
            return quarantined_db
        return None

    def _verify_existing_db_integrity(self) -> Optional[str]:
        """Checks an existing database file for zero-byte truncation, corruption, or broken schema.

        Returns None if valid, or a diagnostic reason string if corrupt.
        """
        if not self.db_path.exists():
            return None

        try:
            size = self.db_path.stat().st_size
        except OSError as exc:
            return f"unable to stat database file: {exc}"

        if size == 0:
            return "zero-byte database file"

        try:
            with self.db_path.open("rb") as fh:
                header = fh.read(16)
            if header != b"SQLite format 3\x00":
                return "invalid SQLite file header"
        except OSError as exc:
            return f"unable to read database header: {exc}"

        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._get_connection()
            cursor = conn.execute("PRAGMA integrity_check;")
            row = cursor.fetchone()
            integrity_result = str(row[0]).strip().lower() if row else "empty integrity_check result"
            if integrity_result != "ok":
                return f"PRAGMA integrity_check failed: {integrity_result}"

            cursor = conn.execute("PRAGMA user_version;")
            ver_row = cursor.fetchone()
            current_ver = int(ver_row[0]) if ver_row else 0

            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_buckets';"
            )
            table_exists = cursor.fetchone() is not None

            if current_ver > 0 and not table_exists:
                return f"missing activity_buckets table with user_version={current_ver}"

            if table_exists:
                cursor = conn.execute("PRAGMA table_info(activity_buckets);")
                cols = {r["name"] for r in cursor.fetchall()}
                required_cols = {
                    "bucket_id",
                    "device_uuid",
                    "bucket_start",
                    "bucket_end",
                    "active_seconds",
                    "idle_seconds",
                    "state",
                    "sync_status",
                }
                missing = required_cols - cols
                if missing:
                    return f"activity_buckets table missing required columns: {sorted(missing)}"

            return None
        except sqlite3.DatabaseError as exc:
            return f"SQLite DatabaseError: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _get_connection(self, busy_timeout_ms: Optional[int] = None) -> sqlite3.Connection:
        """Returns a configured SQLite connection with bounded busy timeout."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        effective_timeout = int(busy_timeout_ms) if busy_timeout_ms is not None else self.busy_timeout_ms
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=effective_timeout / 1000.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode; we manage transactions explicitly
        )
        try:
            conn.row_factory = sqlite3.Row
            # Enable WAL (Write-Ahead Logging) and normal synchronous mode for durability & concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(f"PRAGMA busy_timeout={effective_timeout};")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def _init_db(self) -> None:
        """Verifies database integrity, quarantines corrupt files if needed, and applies schema migrations."""
        with self._lock:
            corruption_reason = self._verify_existing_db_integrity()
            if corruption_reason is not None:
                self._quarantine_corrupt_db(corruption_reason)

            conn = self._get_connection()
            try:
                # Check current schema version
                cursor = conn.execute("PRAGMA user_version;")
                row = cursor.fetchone()
                current_ver = row[0] if row else 0

                # Check if activity_buckets table exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_buckets';"
                )
                table_exists = cursor.fetchone() is not None

                if current_ver == 0 and not table_exists:
                    # Fresh database installation: create current (v2) schema directly
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        self._create_v2_schema(conn)
                        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
                        conn.execute("COMMIT;")
                        logger.info(f"Initialized fresh SQLite queue at schema version {CURRENT_SCHEMA_VERSION}.")
                    except Exception:
                        conn.execute("ROLLBACK;")
                        raise

                elif current_ver < CURRENT_SCHEMA_VERSION:
                    # Run sequential schema migrations
                    if current_ver < 2:
                        conn.execute("BEGIN IMMEDIATE;")
                        try:
                            self._migrate_to_v2(conn)
                            conn.execute("PRAGMA user_version = 2;")
                            conn.execute("COMMIT;")
                            logger.info(f"Migrated SQLite queue from version {current_ver} to {CURRENT_SCHEMA_VERSION}.")
                        except Exception:
                            conn.execute("ROLLBACK;")
                            raise

            finally:
                conn.close()

    def _create_v2_schema(self, conn: sqlite3.Connection) -> None:
        """Creates the initial v2 schema with employee isolation and metadata support."""
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
                employee_id INTEGER,
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

        self._create_v2_indexes(conn)

    def _migrate_to_v2(self, conn: sqlite3.Connection) -> None:
        """Migrates legacy or v1 database to v2 (adds employee_id and quarantines unassigned records)."""
        logger.info("Applying SQLite schema migration v1 -> v2 (Employee isolation)...")

        # 1. Ensure meta table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 2. Check existing columns of activity_buckets
        cursor = conn.execute("PRAGMA table_info(activity_buckets);")
        columns = [r["name"] for r in cursor.fetchall()]

        if "employee_id" not in columns:
            conn.execute("ALTER TABLE activity_buckets ADD COLUMN employee_id INTEGER;")
            logger.info("Added employee_id column to activity_buckets table.")

        # 3. Quarantine legacy records that have no employee_id.
        # DO NOT guess employee ownership or assign them to the current user!
        # Existing SYNCED records remain SYNCED; only un-synced records are quarantined.
        cursor = conn.execute("""
            UPDATE activity_buckets
            SET sync_status = ?,
                last_error = 'Legacy unassigned bucket quarantined for review'
            WHERE employee_id IS NULL
              AND sync_status IN (?, ?);
        """, (
            SyncStatus.ORPHANED.value,
            SyncStatus.PENDING.value,
            SyncStatus.FAILED.value,
        ))
        quarantined_count = cursor.rowcount
        if quarantined_count > 0:
            logger.warning(
                f"Quarantined {quarantined_count} legacy activity buckets without employee ownership."
            )

        # 4. Create optimized indexes
        self._create_v2_indexes(conn)

    def _create_v2_indexes(self, conn: sqlite3.Connection) -> None:
        """Creates composite indexes for employee-scoped queue queries."""
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_buckets_emp_sync
            ON activity_buckets(employee_id, sync_status, retry_count, bucket_start);
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_buckets_sync_status
            ON activity_buckets(sync_status, created_at);
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_buckets_session
            ON activity_buckets(session_id, bucket_start);
        """)

    def save_bucket(
        self,
        bucket: ActivityBucket,
        employee_id: Optional[int] = None,
        lock_timeout: Optional[float] = None,
        busy_timeout_ms: Optional[int] = None,
    ) -> None:
        """Saves or updates an activity bucket in the local queue with employee association.

        Safety rules:
        1. No implicit meta.employee_id lookup: identity must be explicitly provided.
        2. If no verified employee ID exists, the bucket is preserved as ORPHANED.
        3. Prevents bucket ID conflicts from overwriting another employee's activity.
        4. Bounded lock and busy timeouts protect shutdown callers from hanging.
        """
        apps_json = json.dumps([app.to_dict() for app in bucket.app_usages])
        resolved_emp_id = bucket.employee_id if bucket.employee_id is not None else employee_id

        if resolved_emp_id is None:
            # Unbound activity: quarantine to ORPHANED so it is never uploaded or reassigned
            if bucket.sync_status in (SyncStatus.DEAD_LETTER, SyncStatus.SYNCED):
                sync_status_val = (
                    bucket.sync_status.value
                    if isinstance(bucket.sync_status, SyncStatus)
                    else str(bucket.sync_status)
                )
            else:
                sync_status_val = SyncStatus.ORPHANED.value
            last_error_val = bucket.last_error or "Activity recorded without authenticated employee binding"
        else:
            sync_status_val = (
                bucket.sync_status.value
                if isinstance(bucket.sync_status, SyncStatus)
                else str(bucket.sync_status)
            )
            last_error_val = bucket.last_error

        save_bucket_id = bucket.bucket_id

        if lock_timeout is not None:
            acquired = self._lock.acquire(timeout=lock_timeout)
            if not acquired:
                raise TimeoutError(f"ActivityQueue lock acquisition timed out after {lock_timeout}s.")
        else:
            self._lock.acquire()

        try:
            conn = self._get_connection(busy_timeout_ms=busy_timeout_ms)
            try:
                # Check for bucket ID conflict across different employee identities
                cursor = conn.execute(
                    "SELECT employee_id FROM activity_buckets WHERE bucket_id = ?;",
                    (bucket.bucket_id,)
                )
                existing = cursor.fetchone()
                if existing:
                    existing_emp = existing["employee_id"]
                    if existing_emp is not None and resolved_emp_id is not None and existing_emp != resolved_emp_id:
                        # Collision: bucket_id already belongs to a different employee.
                        # Preserve existing employee's activity intact, assign unique collision-free RFC 4122 UUIDv5
                        orig_id = bucket.bucket_id
                        save_bucket_id = derive_collision_bucket_id(orig_id, resolved_emp_id)
                        bucket.bucket_id = save_bucket_id
                        logger.warning(
                            f"Bucket ID collision detected for bucket_id='{orig_id}': "
                            f"existing employee {existing_emp} != new employee {resolved_emp_id}. "
                            f"Preserving existing record and saving new record as valid UUIDv5 '{save_bucket_id}'."
                        )

                conn.execute("""
                    INSERT INTO activity_buckets (
                        bucket_id, device_uuid, employee_id, session_id, bucket_start, bucket_end,
                        active_seconds, idle_seconds, state, is_locked, is_sleeping,
                        is_paused, apps_json, sync_status, retry_count, last_error, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket_id) DO UPDATE SET
                        employee_id = COALESCE(activity_buckets.employee_id, excluded.employee_id),
                        active_seconds = excluded.active_seconds,
                        idle_seconds = excluded.idle_seconds,
                        state = excluded.state,
                        is_locked = excluded.is_locked,
                        is_sleeping = excluded.is_sleeping,
                        is_paused = excluded.is_paused,
                        apps_json = excluded.apps_json,
                        sync_status = excluded.sync_status,
                        retry_count = excluded.retry_count,
                        last_error = COALESCE(excluded.last_error, activity_buckets.last_error)
                    WHERE (activity_buckets.employee_id IS NULL OR excluded.employee_id IS NULL OR activity_buckets.employee_id = excluded.employee_id);
                """, (
                    save_bucket_id,
                    bucket.device_uuid,
                    resolved_emp_id,
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
                    sync_status_val,
                    bucket.retry_count,
                    last_error_val,
                    bucket.created_at,
                ))
            except sqlite3.OperationalError as exc:
                logger.error(f"SQLite operational error saving bucket {save_bucket_id}: {exc}")
                raise
            except sqlite3.DatabaseError as exc:
                logger.error(f"SQLite database error saving bucket {save_bucket_id}: {exc}")
                raise
            finally:
                conn.close()
        finally:
            self._lock.release()

    def get_pending_buckets(
        self,
        employee_id: Optional[int] = None,
        limit: int = 50,
        max_retries: int = MAX_RETRY_COUNT,
        allow_unfiltered: bool = False,
    ) -> List[ActivityBucket]:
        """Retrieves a batch of pending activity buckets ordered by bucket_start.

        Strict employee isolation:
        - If employee_id is provided, strictly filters to that employee's buckets.
        - If employee_id is None, fails closed and returns an empty list unless
          allow_unfiltered=True is explicitly passed for diagnostics.
        - Excludes records that have reached max_retries or are in DEAD_LETTER / ORPHANED status.
        """
        buckets: List[ActivityBucket] = []
        if employee_id is None and not allow_unfiltered:
            logger.debug("get_pending_buckets called without employee_id and allow_unfiltered=False; failing closed.")
            return []

        with self._lock:
            conn = self._get_connection()
            try:
                if employee_id is not None:
                    cursor = conn.execute("""
                        SELECT * FROM activity_buckets
                        WHERE employee_id = ?
                          AND (sync_status = ? OR (sync_status = ? AND retry_count < ?))
                        ORDER BY bucket_start ASC
                        LIMIT ?;
                    """, (
                        employee_id,
                        SyncStatus.PENDING.value,
                        SyncStatus.FAILED.value,
                        max_retries,
                        limit,
                    ))
                else:
                    # In explicit diagnostic queries
                    cursor = conn.execute("""
                        SELECT * FROM activity_buckets
                        WHERE (sync_status = ? OR (sync_status = ? AND retry_count < ?))
                        ORDER BY bucket_start ASC
                        LIMIT ?;
                    """, (
                        SyncStatus.PENDING.value,
                        SyncStatus.FAILED.value,
                        max_retries,
                        limit,
                    ))

                for row in cursor.fetchall():
                    raw_apps = json.loads(row["apps_json"])
                    app_usages = [
                        AppUsage(
                            process_name=a.get("process_name", ""),
                            duration_seconds=int(a.get("duration_seconds", 0)),
                        )
                        for a in raw_apps
                    ]
                    bucket = ActivityBucket(
                        bucket_id=row["bucket_id"],
                        device_uuid=row["device_uuid"],
                        employee_id=row["employee_id"],
                        session_id=row["session_id"],
                        bucket_start=row["bucket_start"],
                        bucket_end=row["bucket_end"],
                        active_seconds=row["active_seconds"],
                        idle_seconds=row["idle_seconds"],
                        state=(
                            TrackerState(row["state"])
                            if row["state"] in TrackerState._value2member_map_
                            else TrackerState.STOPPED
                        ),
                        is_locked=bool(row["is_locked"]),
                        is_sleeping=bool(row["is_sleeping"]),
                        is_paused=bool(row["is_paused"]),
                        app_usages=app_usages,
                        sync_status=(
                            SyncStatus(row["sync_status"])
                            if row["sync_status"] in SyncStatus._value2member_map_
                            else SyncStatus.PENDING
                        ),
                        retry_count=row["retry_count"],
                        last_error=row["last_error"],
                        created_at=row["created_at"],
                    )
                    buckets.append(bucket)
            finally:
                conn.close()
        return buckets

    def mark_buckets_synced(self, bucket_ids: List[str], employee_id: Optional[int] = None) -> None:
        """Marks a batch of buckets as successfully synced.

        Enforces employee ownership predicate when employee_id is provided.
        """
        if not bucket_ids:
            return
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    placeholders = ",".join("?" for _ in bucket_ids)
                    if employee_id is not None:
                        conn.execute(f"""
                            UPDATE activity_buckets
                            SET sync_status = ?, last_error = NULL
                            WHERE bucket_id IN ({placeholders}) AND employee_id = ?;
                        """, [SyncStatus.SYNCED.value] + bucket_ids + [employee_id])
                    else:
                        conn.execute(f"""
                            UPDATE activity_buckets
                            SET sync_status = ?, last_error = NULL
                            WHERE bucket_id IN ({placeholders});
                        """, [SyncStatus.SYNCED.value] + bucket_ids)
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
            finally:
                conn.close()

    def mark_buckets_failed(
        self,
        bucket_ids: List[str],
        error_message: str,
        is_fatal: bool = False,
        max_retries: int = MAX_RETRY_COUNT,
        employee_id: Optional[int] = None,
    ) -> None:
        """Increments retry count and marks buckets as FAILED or DEAD_LETTER.

        Enforces employee ownership predicate when employee_id is provided.
        """
        if not bucket_ids:
            return
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    placeholders = ",".join("?" for _ in bucket_ids)
                    emp_predicate = " AND employee_id = ?" if employee_id is not None else ""
                    emp_param = [employee_id] if employee_id is not None else []

                    if is_fatal:
                        # Fatal error: move immediately to DEAD_LETTER so it does not block the queue
                        conn.execute(f"""
                            UPDATE activity_buckets
                            SET sync_status = ?,
                                retry_count = retry_count + 1,
                                last_error = ?
                            WHERE bucket_id IN ({placeholders}){emp_predicate};
                        """, [SyncStatus.DEAD_LETTER.value, error_message[:255]] + bucket_ids + emp_param)
                    else:
                        # Bounded retry: if retry_count exceeds threshold, transition to DEAD_LETTER
                        conn.execute(f"""
                            UPDATE activity_buckets
                            SET sync_status = CASE
                                    WHEN retry_count + 1 >= ? THEN ?
                                    ELSE ?
                                END,
                                retry_count = retry_count + 1,
                                last_error = ?
                            WHERE bucket_id IN ({placeholders}){emp_predicate};
                        """, [
                            max_retries,
                            SyncStatus.DEAD_LETTER.value,
                            SyncStatus.FAILED.value,
                            error_message[:255],
                        ] + bucket_ids + emp_param)
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
            finally:
                conn.close()

    def mark_buckets_dead_letter(
        self,
        bucket_ids: List[str],
        reason: str,
        employee_id: Optional[int] = None,
    ) -> None:
        """Explicitly moves buckets to DEAD_LETTER state without deleting data.

        Enforces employee ownership predicate when employee_id is provided.
        """
        if not bucket_ids:
            return
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    placeholders = ",".join("?" for _ in bucket_ids)
                    emp_predicate = " AND employee_id = ?" if employee_id is not None else ""
                    emp_param = [employee_id] if employee_id is not None else []

                    conn.execute(f"""
                        UPDATE activity_buckets
                        SET sync_status = ?,
                            last_error = ?
                        WHERE bucket_id IN ({placeholders}){emp_predicate};
                    """, [SyncStatus.DEAD_LETTER.value, reason[:255]] + bucket_ids + emp_param)
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
            finally:
                conn.close()

    def prune_synced_buckets(self, max_age_days: int = 30) -> int:
        """Safely removes successfully synced records older than max_age_days.

        Never deletes PENDING, FAILED, ORPHANED, or DEAD_LETTER records.
        """
        if max_age_days < 0:
            raise ValueError("max_age_days must be non-negative.")
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    cursor = conn.execute("""
                        DELETE FROM activity_buckets
                        WHERE sync_status = ?
                          AND datetime(bucket_start) < datetime('now', ?);
                    """, (SyncStatus.SYNCED.value, f"-{max_age_days} days"))
                    deleted = cursor.rowcount
                    conn.execute("COMMIT;")
                    if deleted > 0:
                        logger.info(f"Pruned {deleted} synced activity buckets older than {max_age_days} days.")
                    return deleted
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
            finally:
                conn.close()

    def get_orphaned_buckets(self, limit: int = 50) -> List[ActivityBucket]:
        """Retrieves quarantined orphaned buckets for review or support recovery."""
        buckets: List[ActivityBucket] = []
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("""
                    SELECT * FROM activity_buckets
                    WHERE sync_status = ?
                    ORDER BY bucket_start ASC
                    LIMIT ?;
                """, (SyncStatus.ORPHANED.value, limit))
                for row in cursor.fetchall():
                    raw_apps = json.loads(row["apps_json"])
                    app_usages = [
                        AppUsage(
                            process_name=a.get("process_name", ""),
                            duration_seconds=int(a.get("duration_seconds", 0)),
                        )
                        for a in raw_apps
                    ]
                    bucket = ActivityBucket(
                        bucket_id=row["bucket_id"],
                        device_uuid=row["device_uuid"],
                        employee_id=row["employee_id"],
                        session_id=row["session_id"],
                        bucket_start=row["bucket_start"],
                        bucket_end=row["bucket_end"],
                        active_seconds=row["active_seconds"],
                        idle_seconds=row["idle_seconds"],
                        state=(
                            TrackerState(row["state"])
                            if row["state"] in TrackerState._value2member_map_
                            else TrackerState.STOPPED
                        ),
                        is_locked=bool(row["is_locked"]),
                        is_sleeping=bool(row["is_sleeping"]),
                        is_paused=bool(row["is_paused"]),
                        app_usages=app_usages,
                        sync_status=SyncStatus.ORPHANED,
                        retry_count=row["retry_count"],
                        last_error=row["last_error"],
                        created_at=row["created_at"],
                    )
                    buckets.append(bucket)
            finally:
                conn.close()
        return buckets

    def get_dead_letter_buckets(
        self,
        employee_id: Optional[int] = None,
        limit: int = 50,
        allow_unfiltered: bool = False,
    ) -> List[ActivityBucket]:
        """Retrieves dead-letter buckets for review or support recovery.

        Strict employee isolation: fails closed if employee_id is None unless allow_unfiltered=True.
        """
        buckets: List[ActivityBucket] = []
        if employee_id is None and not allow_unfiltered:
            logger.debug("get_dead_letter_buckets called without employee_id and allow_unfiltered=False; failing closed.")
            return []

        with self._lock:
            conn = self._get_connection()
            try:
                if employee_id is not None:
                    cursor = conn.execute("""
                        SELECT * FROM activity_buckets
                        WHERE sync_status = ? AND employee_id = ?
                        ORDER BY bucket_start ASC
                        LIMIT ?;
                    """, (SyncStatus.DEAD_LETTER.value, employee_id, limit))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM activity_buckets
                        WHERE sync_status = ?
                        ORDER BY bucket_start ASC
                        LIMIT ?;
                    """, (SyncStatus.DEAD_LETTER.value, limit))

                for row in cursor.fetchall():
                    raw_apps = json.loads(row["apps_json"])
                    app_usages = [
                        AppUsage(
                            process_name=a.get("process_name", ""),
                            duration_seconds=int(a.get("duration_seconds", 0)),
                        )
                        for a in raw_apps
                    ]
                    bucket = ActivityBucket(
                        bucket_id=row["bucket_id"],
                        device_uuid=row["device_uuid"],
                        employee_id=row["employee_id"],
                        session_id=row["session_id"],
                        bucket_start=row["bucket_start"],
                        bucket_end=row["bucket_end"],
                        active_seconds=row["active_seconds"],
                        idle_seconds=row["idle_seconds"],
                        state=(
                            TrackerState(row["state"])
                            if row["state"] in TrackerState._value2member_map_
                            else TrackerState.STOPPED
                        ),
                        is_locked=bool(row["is_locked"]),
                        is_sleeping=bool(row["is_sleeping"]),
                        is_paused=bool(row["is_paused"]),
                        app_usages=app_usages,
                        sync_status=SyncStatus.DEAD_LETTER,
                        retry_count=row["retry_count"],
                        last_error=row["last_error"],
                        created_at=row["created_at"],
                    )
                    buckets.append(bucket)
            finally:
                conn.close()
        return buckets

    def get_summary_stats(
        self,
        since_iso: str,
        employee_id: Optional[int] = None,
        allow_unfiltered: bool = False,
    ) -> Dict[str, Any]:
        """Calculates aggregated activity summary since a given timestamp.

        Strict employee isolation:
        - If employee_id is provided, filters strictly to that employee.
        - If employee_id is None, returns empty zeroed stats unless allow_unfiltered=True.
        """
        stats: Dict[str, Any] = {
            "total_active_seconds": 0,
            "total_idle_seconds": 0,
            "total_bucket_count": 0,
            "app_breakdown": {},
        }
        if employee_id is None and not allow_unfiltered:
            logger.debug("get_summary_stats called without employee_id and allow_unfiltered=False; failing closed.")
            return stats

        with self._lock:
            conn = self._get_connection()
            try:
                if employee_id is not None:
                    cursor = conn.execute("""
                        SELECT active_seconds, idle_seconds, apps_json
                        FROM activity_buckets
                        WHERE bucket_start >= ? AND employee_id = ?;
                    """, (since_iso, employee_id))
                else:
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

    def get_pending_count(
        self,
        employee_id: Optional[int] = None,
        allow_unfiltered: bool = False,
    ) -> int:
        """Returns count of buckets pending sync.

        Strict employee isolation:
        - If employee_id is provided, counts only that employee's pending records.
        - If employee_id is None, returns 0 unless allow_unfiltered=True.
        """
        if employee_id is None and not allow_unfiltered:
            logger.debug("get_pending_count called without employee_id and allow_unfiltered=False; failing closed.")
            return 0

        with self._lock:
            conn = self._get_connection()
            try:
                if employee_id is not None:
                    cursor = conn.execute("""
                        SELECT COUNT(*) as cnt FROM activity_buckets
                        WHERE employee_id = ?
                          AND (sync_status = ? OR sync_status = ?);
                    """, (employee_id, SyncStatus.PENDING.value, SyncStatus.FAILED.value))
                else:
                    cursor = conn.execute("""
                        SELECT COUNT(*) as cnt FROM activity_buckets
                        WHERE sync_status = ? OR sync_status = ?;
                    """, (SyncStatus.PENDING.value, SyncStatus.FAILED.value))
                row = cursor.fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()

    def get_total_bucket_count(self, employee_id: Optional[int] = None, allow_unfiltered: bool = False) -> int:
        """Returns total count of activity buckets stored locally.

        Strict employee isolation: returns 0 if employee_id is None unless allow_unfiltered=True.
        """
        if employee_id is None and not allow_unfiltered:
            logger.debug("get_total_bucket_count called without employee_id and allow_unfiltered=False; failing closed.")
            return 0

        with self._lock:
            conn = self._get_connection()
            try:
                if employee_id is not None:
                    cursor = conn.execute(
                        "SELECT COUNT(*) as cnt FROM activity_buckets WHERE employee_id = ?;",
                        (employee_id,),
                    )
                else:
                    cursor = conn.execute("SELECT COUNT(*) as cnt FROM activity_buckets;")
                row = cursor.fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()

    def get_recent_buckets(
        self,
        limit: int = 50,
        employee_id: Optional[int] = None,
        allow_unfiltered: bool = False,
    ) -> List[ActivityBucket]:
        """Retrieves recent activity buckets ordered by start time descending.

        Strict employee isolation:
        - If employee_id is provided, filters strictly to that employee.
        - If employee_id is None, fails closed unless allow_unfiltered=True.
        """
        buckets: List[ActivityBucket] = []
        if employee_id is None and not allow_unfiltered:
            logger.debug("get_recent_buckets called without employee_id and allow_unfiltered=False; failing closed.")
            return []

        with self._lock:
            conn = self._get_connection()
            try:
                if employee_id is not None:
                    cursor = conn.execute("""
                        SELECT * FROM activity_buckets
                        WHERE employee_id = ?
                        ORDER BY bucket_start DESC
                        LIMIT ?;
                    """, (employee_id, limit))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM activity_buckets
                        ORDER BY bucket_start DESC
                        LIMIT ?;
                    """, (limit,))

                for row in cursor.fetchall():
                    raw_apps = json.loads(row["apps_json"])
                    app_usages = [
                        AppUsage(
                            process_name=a.get("process_name", ""),
                            duration_seconds=int(a.get("duration_seconds", 0)),
                        )
                        for a in raw_apps
                    ]
                    bucket = ActivityBucket(
                        bucket_id=row["bucket_id"],
                        device_uuid=row["device_uuid"],
                        employee_id=row["employee_id"],
                        session_id=row["session_id"],
                        bucket_start=row["bucket_start"],
                        bucket_end=row["bucket_end"],
                        active_seconds=row["active_seconds"],
                        idle_seconds=row["idle_seconds"],
                        state=(
                            TrackerState(row["state"])
                            if row["state"] in TrackerState._value2member_map_
                            else TrackerState.STOPPED
                        ),
                        is_locked=bool(row["is_locked"]),
                        is_sleeping=bool(row["is_sleeping"]),
                        is_paused=bool(row["is_paused"]),
                        app_usages=app_usages,
                        sync_status=(
                            SyncStatus(row["sync_status"])
                            if row["sync_status"] in SyncStatus._value2member_map_
                            else SyncStatus.PENDING
                        ),
                        retry_count=row["retry_count"],
                        last_error=row["last_error"],
                        created_at=row["created_at"],
                    )
                    buckets.append(bucket)
            finally:
                conn.close()
        return buckets

    def get_quarantine_diagnostics(self) -> Dict[str, Any]:
        """Provides diagnostic visibility into quarantined ORPHANED and DEAD_LETTER records.

        Explicitly separated from employee-facing views. Never reassigns or uploads records.
        """
        with self._lock:
            conn = self._get_connection()
            try:
                c_orphaned = conn.execute(
                    "SELECT COUNT(*) as cnt FROM activity_buckets WHERE sync_status = ?;",
                    (SyncStatus.ORPHANED.value,)
                ).fetchone()["cnt"]

                c_dead_letter = conn.execute(
                    "SELECT COUNT(*) as cnt FROM activity_buckets WHERE sync_status = ?;",
                    (SyncStatus.DEAD_LETTER.value,)
                ).fetchone()["cnt"]

                reasons_cursor = conn.execute(
                    "SELECT DISTINCT last_error FROM activity_buckets WHERE sync_status IN (?, ?) AND last_error IS NOT NULL LIMIT 10;",
                    (SyncStatus.DEAD_LETTER.value, SyncStatus.ORPHANED.value)
                )
                sample_errors = [r["last_error"] for r in reasons_cursor.fetchall() if r["last_error"]]

                return {
                    "orphaned_count": int(c_orphaned),
                    "dead_letter_count": int(c_dead_letter),
                    "total_quarantined": int(c_orphaned + c_dead_letter),
                    "sample_reasons": sample_errors,
                }
            finally:
                conn.close()

    def get_device_diagnostic_counts(self) -> Dict[str, int]:
        """Returns total queue status breakdown across the entire workstation for technical diagnostics."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("SELECT sync_status, COUNT(*) as cnt FROM activity_buckets GROUP BY sync_status;")
                counts = {status.value: 0 for status in SyncStatus}
                total = 0
                for row in cursor.fetchall():
                    s = row["sync_status"]
                    cnt = int(row["cnt"])
                    counts[s] = cnt
                    total += cnt
                counts["total"] = total
                return counts
            finally:
                conn.close()

    def set_auth_state_atomic(
        self,
        auth_token_protected: Optional[str] = None,
        employee_id: Optional[int] = None,
        employee_name: Optional[str] = None,
        employee_email: Optional[str] = None,
        auth_state: str = "BOUND",
    ) -> None:
        """Atomically persists authentication token and employee binding metadata in a single transaction."""
        now_iso = current_utc_iso()
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    if auth_token_protected is not None:
                        conn.execute("""
                            INSERT INTO meta (key, value, updated_at) VALUES ('auth_token', ?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                        """, (auth_token_protected, now_iso))
                    else:
                        conn.execute("DELETE FROM meta WHERE key = 'auth_token';")

                    if employee_id is not None:
                        conn.execute("""
                            INSERT INTO meta (key, value, updated_at) VALUES ('employee_id', ?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                        """, (str(employee_id), now_iso))
                    else:
                        conn.execute("DELETE FROM meta WHERE key = 'employee_id';")

                    if employee_name is not None:
                        conn.execute("""
                            INSERT INTO meta (key, value, updated_at) VALUES ('employee_name', ?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                        """, (employee_name, now_iso))
                    else:
                        conn.execute("DELETE FROM meta WHERE key = 'employee_name';")

                    if employee_email is not None:
                        conn.execute("""
                            INSERT INTO meta (key, value, updated_at) VALUES ('employee_email', ?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                        """, (employee_email, now_iso))
                    else:
                        conn.execute("DELETE FROM meta WHERE key = 'employee_email';")

                    conn.execute("""
                        INSERT INTO meta (key, value, updated_at) VALUES ('auth_state', ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                    """, (auth_state, now_iso))

                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
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
