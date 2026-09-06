"""Event-sourced persistence for local development and production adapters.

The domain model intentionally stays independent of a database framework. The
SQLite implementation is used by the CLI and API in development; the Postgres
implementation mirrors the same small interface for multi-worker deployments.
Snapshots make long-running projects cheap to resume while the append-only
event stream preserves an auditable history.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import (
    AcceptanceCriterion,
    ArtifactRef,
    AssetKind,
    Attempt,
    AudioCue,
    CharacterBible,
    ClarificationTurn,
    CostRecord,
    CreativeBrief,
    CriterionCategory,
    CriterionResult,
    Diagnosis,
    Evidence,
    JudgeResult,
    LocationBible,
    PlanVersion,
    Project,
    ProjectStatus,
    PromptBundle,
    ProviderJob,
    ReferenceAsset,
    RepairAction,
    RepairKind,
    Scene,
    Severity,
    Shot,
    StyleBible,
    Verdict,
    as_jsonable,
    stable_hash,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Event:
    id: int
    project_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


class SnapshotConflict(RuntimeError):
    """Raised when a stale project snapshot attempts to overwrite newer state."""

    def __init__(self, project_id: str, expected_revision: int, actual_revision: int | None) -> None:
        actual = "missing" if actual_revision is None else str(actual_revision)
        super().__init__(
            f"Project snapshot conflict for {project_id}: expected revision {expected_revision}, actual {actual}"
        )
        self.project_id = project_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class EventStore:
    """SQLite event store; its methods form the persistence contract."""

    def __init__(self, path: str | Path = ".data/director.db") -> None:
        raw_path = str(path)
        if raw_path.startswith("sqlite:///"):
            raw_path = raw_path.removeprefix("sqlite:///")
            if raw_path.startswith("/") and os.name == "nt":
                raw_path = raw_path[1:]
        self.path = raw_path
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, snapshot TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                project_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT,
                created_at TEXT NOT NULL, PRIMARY KEY(project_id, key)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_users (
                id INTEGER PRIMARY KEY CHECK (id = 1), username TEXT NOT NULL,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Existing Phase 0 databases predate optimistic snapshot revisions.
        # SQLite does not support ADD COLUMN IF NOT EXISTS, so inspect the
        # schema before applying the one-time migration.
        project_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(projects)").fetchall()}
        if "revision" not in project_columns:
            self.connection.execute("ALTER TABLE projects ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        self.connection.commit()

    def save_project(
        self,
        project: Project,
        *,
        event_type: str = "project.updated",
        payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> Event:
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        with self._lock:
            now = _now()
            previous_updated_at = project.updated_at
            previous_revision = project.revision
            try:
                with self.connection:
                    row = self.connection.execute(
                        "SELECT revision FROM projects WHERE id=?", (project.id,)
                    ).fetchone()
                    if row is None:
                        # A Project object may have been built against another
                        # store. A new database has no prior revision to
                        # compare, so initialize its local sequence at one.
                        if expected_revision not in (None, 0):
                            raise SnapshotConflict(project.id, expected_revision, None)
                        next_revision = 1
                        project.revision = next_revision
                        project.updated_at = datetime.now(UTC)
                        snapshot = json.dumps(
                            as_jsonable(project), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        try:
                            self.connection.execute(
                                "INSERT INTO projects(id,name,snapshot,snapshot_hash,updated_at,revision) VALUES(?,?,?,?,?,?)",
                                (project.id, project.name, snapshot, stable_hash(project), now, next_revision),
                            )
                        except sqlite3.IntegrityError as error:
                            # Another connection may have inserted the same
                            # project after the SELECT. Surface a domain
                            # conflict rather than overwriting it.
                            raise SnapshotConflict(project.id, 0, None) from error
                    else:
                        actual_revision = int(row["revision"])
                        expected = project.revision if expected_revision is None else expected_revision
                        if expected != actual_revision:
                            raise SnapshotConflict(project.id, expected, actual_revision)
                        next_revision = actual_revision + 1
                        project.revision = next_revision
                        project.updated_at = datetime.now(UTC)
                        snapshot = json.dumps(
                            as_jsonable(project), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        updated = self.connection.execute(
                            "UPDATE projects SET name=?,snapshot=?,snapshot_hash=?,updated_at=?,revision=? "
                            "WHERE id=? AND revision=?",
                            (
                                project.name,
                                snapshot,
                                stable_hash(project),
                                now,
                                next_revision,
                                project.id,
                                actual_revision,
                            ),
                        ).rowcount
                        if updated != 1:
                            raise SnapshotConflict(project.id, expected, actual_revision)
                    return self._append_event(project.id, event_type, dict(payload or {}), now=now)
            except Exception:
                # Keep the caller's object usable after a rejected write.
                project.revision = previous_revision
                project.updated_at = previous_updated_at
                raise

    def append_event(
        self, project_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> Event:
        with self._lock, self.connection:
            return self._append_event(project_id, event_type, dict(payload or {}))

    def _append_event(
        self, project_id: str, event_type: str, payload: dict[str, Any], *, now: str | None = None
    ) -> Event:
        created_at = now or _now()
        cursor = self.connection.execute(
            "INSERT INTO events(project_id,event_type,payload,created_at) VALUES(?,?,?,?)",
            (project_id, event_type, json.dumps(as_jsonable(payload), ensure_ascii=False), created_at),
        )
        return Event(int(cursor.lastrowid), project_id, event_type, payload, created_at)

    def events(self, project_id: str, *, after_id: int = 0, limit: int | None = None) -> list[Event]:
        query = "SELECT id,project_id,event_type,payload,created_at FROM events WHERE project_id=? AND id>? ORDER BY id"
        params: list[Any] = [project_id, after_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, limit))
        rows = self.connection.execute(query, params).fetchall()
        return [
            Event(int(row["id"]), row["project_id"], row["event_type"], json.loads(row["payload"]), row["created_at"])
            for row in rows
        ]

    def load_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self.connection.execute("SELECT snapshot,revision FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Project snapshot is not valid JSON") from error
        project = decode_project(snapshot)
        # Revision is authoritative in the row so hand-edited/legacy JSON
        # cannot accidentally reset the compare-and-swap sequence.
        project.revision = max(0, int(row["revision"] or 0))
        return project

    def list_projects(self) -> list[Project]:
        """Return durable projects newest first for the operator console."""
        with self._lock:
            rows = self.connection.execute("SELECT snapshot FROM projects ORDER BY updated_at DESC").fetchall()
        return [decode_project(json.loads(row["snapshot"])) for row in rows]

    def claim_idempotency(
        self, project_id: str, key: str, result: Any | None = None
    ) -> tuple[bool, Any | None]:
        """Atomically claim a key, returning (claimed, stored_result)."""
        encoded = None if result is None else json.dumps(as_jsonable(result), ensure_ascii=False)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO idempotency_keys(project_id,key,result,created_at) VALUES(?,?,?,?)",
                (project_id, key, encoded, _now()),
            )
            if cursor.rowcount:
                return True, result
            row = self.connection.execute(
                "SELECT result FROM idempotency_keys WHERE project_id=? AND key=?", (project_id, key)
            ).fetchone()
        return False, (json.loads(row["result"]) if row and row["result"] else None)

    def close(self) -> None:
        self.connection.close()

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def auth_user(self) -> tuple[str, str] | None:
        row = self.connection.execute("SELECT username,password_hash FROM auth_users WHERE id=1").fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def create_auth_user(self, username: str, password_hash: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO auth_users(id,username,password_hash,created_at) VALUES(1,?,?,?)",
                (username, password_hash, _now()),
            )

    def create_auth_session(self, token: str, username: str, expires_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO auth_sessions(token,username,expires_at,created_at) VALUES(?,?,?,?)",
                (token, username, expires_at, _now()),
            )

    def auth_session_username(self, token: str) -> str | None:
        row = self.connection.execute(
            "SELECT username,expires_at FROM auth_sessions WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        if row[1] <= _now():
            self.revoke_auth_session(token)
            return None
        return str(row[0])

    def revoke_auth_session(self, token: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM auth_sessions WHERE token=?", (token,))


SQLiteEventStore = EventStore


class PostgresEventStore:
    """PostgreSQL counterpart using psycopg 3, imported only when configured."""

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - optional production dependency
            raise RuntimeError("Postgres storage requires psycopg[binary]") from error
        self.dsn = dsn
        self.connection = psycopg.connect(dsn, autocommit=False)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, snapshot JSONB NOT NULL,
                    snapshot_hash TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                    revision BIGINT NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY, project_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id,id);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    project_id TEXT NOT NULL, key TEXT NOT NULL, result JSONB,
                    created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(project_id,key)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL,
                    password_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            # Existing PostgreSQL databases may have been created before
            # optimistic revisions were introduced. Keep startup idempotent.
            cursor.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0")
        self.connection.commit()

    def save_project(
        self,
        project: Project,
        *,
        event_type: str = "project.updated",
        payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> Event:
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        previous_updated_at = project.updated_at
        previous_revision = project.revision
        now = datetime.now(UTC)
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT revision FROM projects WHERE id=%s FOR UPDATE", (project.id,))
                row = cursor.fetchone()
                if row is None:
                    if expected_revision not in (None, 0):
                        raise SnapshotConflict(project.id, expected_revision, None)
                    next_revision = 1
                    project.revision = next_revision
                    project.updated_at = now
                    snapshot = as_jsonable(project)
                    cursor.execute(
                        "INSERT INTO projects(id,name,snapshot,snapshot_hash,updated_at,revision) VALUES(%s,%s,%s,%s,%s,%s)",
                        (project.id, project.name, json.dumps(snapshot), stable_hash(project), now, next_revision),
                    )
                else:
                    actual_revision = int(row[0])
                    expected = project.revision if expected_revision is None else expected_revision
                    if expected != actual_revision:
                        raise SnapshotConflict(project.id, expected, actual_revision)
                    next_revision = actual_revision + 1
                    project.revision = next_revision
                    project.updated_at = now
                    snapshot = as_jsonable(project)
                    cursor.execute(
                        "UPDATE projects SET name=%s,snapshot=%s,snapshot_hash=%s,updated_at=%s,revision=%s "
                        "WHERE id=%s AND revision=%s",
                        (project.name, json.dumps(snapshot), stable_hash(project), now, next_revision, project.id, actual_revision),
                    )
                    if cursor.rowcount != 1:
                        raise SnapshotConflict(project.id, expected, actual_revision)
                event = self._append_event(project.id, event_type, dict(payload or {}), now=now)
            self.connection.commit()
            return event
        except Exception:
            self.connection.rollback()
            project.revision = previous_revision
            project.updated_at = previous_updated_at
            raise

    def append_event(self, project_id: str, event_type: str, payload: Mapping[str, Any] | None = None) -> Event:
        event = self._append_event(project_id, event_type, dict(payload or {}))
        self.connection.commit()
        return event

    def _append_event(
        self, project_id: str, event_type: str, payload: dict[str, Any], *, now: datetime | None = None
    ) -> Event:
        created = now or datetime.now(UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO events(project_id,event_type,payload,created_at) VALUES(%s,%s,%s,%s) RETURNING id",
                (project_id, event_type, json.dumps(as_jsonable(payload)), created),
            )
            event_id = int(cursor.fetchone()[0])
        return Event(event_id, project_id, event_type, payload, created.isoformat())

    def events(self, project_id: str, *, after_id: int = 0, limit: int | None = None) -> list[Event]:
        query = "SELECT id,project_id,event_type,payload,created_at FROM events WHERE project_id=%s AND id>%s ORDER BY id"
        params: list[Any] = [project_id, after_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, limit))
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return [Event(int(row[0]), row[1], row[2], row[3], row[4].isoformat()) for row in rows]

    def load_project(self, project_id: str) -> Project | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT snapshot,revision FROM projects WHERE id=%s", (project_id,))
            row = cursor.fetchone()
        if not row:
            return None
        project = decode_project(row[0])
        project.revision = max(0, int(row[1] or 0))
        return project

    def list_projects(self) -> list[Project]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT snapshot FROM projects ORDER BY updated_at DESC")
            rows = cursor.fetchall()
        return [decode_project(row[0]) for row in rows]

    def claim_idempotency(self, project_id: str, key: str, result: Any | None = None) -> tuple[bool, Any | None]:
        encoded = json.dumps(as_jsonable(result)) if result is not None else None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO idempotency_keys(project_id,key,result,created_at) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(project_id,key) DO NOTHING RETURNING result",
                (project_id, key, encoded, datetime.now(UTC)),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                self.connection.commit()
                return True, result
            cursor.execute("SELECT result FROM idempotency_keys WHERE project_id=%s AND key=%s", (project_id, key))
            existing = cursor.fetchone()
        self.connection.commit()
        return False, (existing[0] if existing else None)

    def close(self) -> None:
        self.connection.close()

    def get_setting(self, key: str) -> str | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cursor.fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, datetime.now(UTC)),
            )
        self.connection.commit()

    def auth_user(self) -> tuple[str, str] | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT username,password_hash FROM auth_users WHERE id=1")
            row = cursor.fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def create_auth_user(self, username: str, password_hash: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO auth_users(id,username,password_hash,created_at) VALUES(1,%s,%s,%s)",
                (username, password_hash, datetime.now(UTC)),
            )
        self.connection.commit()

    def create_auth_session(self, token: str, username: str, expires_at: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO auth_sessions(token,username,expires_at,created_at) VALUES(%s,%s,%s,%s)",
                (token, username, expires_at, datetime.now(UTC)),
            )
        self.connection.commit()

    def auth_session_username(self, token: str) -> str | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT username,expires_at FROM auth_sessions WHERE token=%s", (token,))
            row = cursor.fetchone()
        if not row:
            return None
        expiry = row[1]
        if isinstance(expiry, str):
            expiry = _dt(expiry)
        if expiry <= datetime.now(UTC):
            self.revoke_auth_session(token)
            return None
        return str(row[0])

    def revoke_auth_session(self, token: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM auth_sessions WHERE token=%s", (token,))
        self.connection.commit()


def make_event_store(url: str | Path | None = None) -> EventStore | PostgresEventStore:
    """Build storage from a URL, keeping SQLite as the safe local default."""
    value = str(url) if url is not None else os.getenv("DIRECTOR_DATABASE_URL", "sqlite:///.data/director.db")
    if value.startswith(("postgresql://", "postgres://")):
        return PostgresEventStore(value)
    return EventStore(value)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
            return parsed
        except ValueError:
            pass
    return datetime.now(UTC)


def _enum(enum_type: Any, value: Any, default: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        try:
            return enum_type(value)
        except (TypeError, ValueError):
            try:
                return enum_type(value.lower())
            except (TypeError, ValueError):
                try:
                    return enum_type(value.upper())
                except (TypeError, ValueError):
                    return default
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return an object-shaped value or an empty mapping for bad snapshots."""
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _string(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: Any) -> list[str]:
    return [item for item in _items(value) if isinstance(item, str)]


def _mapping_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite_float(value: Any, default: float = 0.0, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return default
    return number


def _integer(value: Any, default: int = 0, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except (TypeError, ValueError):
            return default
    else:
        return default
    if minimum is not None and number < minimum:
        return default
    return number


def _boolean(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _artifact(raw: Any) -> ArtifactRef:
    value = _mapping(raw)
    duration = value.get("duration_seconds")
    return ArtifactRef(
        kind=_enum(AssetKind, value.get("kind"), AssetKind.OTHER),
        uri=_string(value.get("uri")),
        sha256=_optional_string(value.get("sha256")),
        mime_type=_optional_string(value.get("mime_type")),
        duration_seconds=_finite_float(duration, minimum=0.0) if duration is not None else None,
        metadata=_mapping_dict(value.get("metadata")),
        id=_string(value.get("id")) or ArtifactRef.__dataclass_fields__["id"].default_factory(),
    )


def _evidence(raw: Any) -> Evidence:
    value = _mapping(raw)
    timestamp = value.get("timestamp_seconds")
    return Evidence(
        _string(value.get("kind"), "frame"),
        _string(value.get("uri")),
        _finite_float(timestamp, minimum=0.0) if timestamp is not None else None,
        _optional_string(value.get("excerpt")),
        _mapping_dict(value.get("metadata")),
    )


def _prompt(raw: Any) -> PromptBundle | None:
    value = _mapping(raw)
    if not value:
        return None
    return PromptBundle(
        positive=_string(value.get("positive")),
        negative=_string(value.get("negative")),
        parameters=_mapping_dict(value.get("parameters")),
        reference_asset_ids=_string_list(value.get("reference_asset_ids")),
        provider=_optional_string(value.get("provider")),
        model=_optional_string(value.get("model")),
        version=_integer(value.get("version"), 1, minimum=1),
        id=_string(value.get("id")) or PromptBundle.__dataclass_fields__["id"].default_factory(),
    )


def _reference(raw: Any) -> ReferenceAsset:
    value = _mapping(raw)
    return ReferenceAsset(
        kind=_enum(AssetKind, value.get("kind"), AssetKind.OTHER),
        uri=_string(value.get("uri")),
        sha256=_optional_string(value.get("sha256")),
        provider=_optional_string(value.get("provider")),
        metadata=_mapping_dict(value.get("metadata")),
        id=_string(value.get("id")) or ReferenceAsset.__dataclass_fields__["id"].default_factory(),
    )


def _character(raw: Any) -> CharacterBible:
    value = _mapping(raw)
    return CharacterBible(
        name=_string(value.get("name")),
        identity=_string(value.get("identity")),
        visual_traits=_string_list(value.get("visual_traits")),
        wardrobe=_string_list(value.get("wardrobe")),
        voice_traits=_string_list(value.get("voice_traits")),
        reference_asset_ids=_string_list(value.get("reference_asset_ids")),
        id=_string(value.get("id")) or CharacterBible.__dataclass_fields__["id"].default_factory(),
    )


def _location(raw: Any) -> LocationBible:
    value = _mapping(raw)
    return LocationBible(
        name=_string(value.get("name")),
        description=_string(value.get("description")),
        geography=_optional_string(value.get("geography")),
        continuity_notes=_string_list(value.get("continuity_notes")),
        reference_asset_ids=_string_list(value.get("reference_asset_ids")),
        id=_string(value.get("id")) or LocationBible.__dataclass_fields__["id"].default_factory(),
    )


def _style(raw: Any) -> StyleBible | None:
    value = _mapping(raw)
    if not value:
        return None
    return StyleBible(
        name=_string(value.get("name")),
        description=_string(value.get("description")),
        palette=_string_list(value.get("palette")),
        lighting=_optional_string(value.get("lighting")),
        camera_language=_optional_string(value.get("camera_language")),
        id=_string(value.get("id")) or StyleBible.__dataclass_fields__["id"].default_factory(),
    )


def _judge(raw: Any) -> JudgeResult | None:
    value = _mapping(raw)
    if not value:
        return None
    raw_results = value.get("criterion_results")
    criterion_results: list[CriterionResult] = []
    if isinstance(raw_results, (list, tuple)):
        for item in raw_results:
            result = _mapping(item)
            if not result:
                criterion_results.append(
                    CriterionResult(
                        "unknown",
                        Verdict.FAIL,
                        failure_code="malformed_criterion",
                        reason="Persisted criterion result is not an object",
                        confidence=0.0,
                        repair_suggestions=[RepairKind.HUMAN],
                    )
                )
                continue
            raw_evidence = result.get("evidence")
            evidence = [_evidence(entry) for entry in raw_evidence] if isinstance(raw_evidence, (list, tuple)) else []
            raw_repairs = result.get("repair_suggestions")
            repairs = (
                [_enum(RepairKind, entry, RepairKind.HUMAN) for entry in raw_repairs]
                if isinstance(raw_repairs, (list, tuple))
                else [RepairKind.HUMAN]
            )
            criterion_results.append(
                CriterionResult(
                    criterion_id=_string(result.get("criterion_id"), "unknown"),
                    verdict=_enum(Verdict, result.get("verdict"), Verdict.FAIL),
                    evidence=evidence,
                    failure_code=_optional_string(result.get("failure_code")),
                    reason=_optional_string(result.get("reason")),
                    confidence=_finite_float(result.get("confidence"), 0.0),
                    repair_suggestions=repairs,
                    skipped=_boolean(result.get("skipped")),
                ).fail_closed()
            )
    elif raw_results is not None:
        criterion_results.append(
            CriterionResult(
                "unknown",
                Verdict.FAIL,
                failure_code="malformed_criterion",
                reason="Persisted criterion results are not an array",
                confidence=0.0,
                repair_suggestions=[RepairKind.HUMAN],
            )
        )
    return JudgeResult(
        shot_id=_string(value.get("shot_id")),
        verdict=_enum(Verdict, value.get("verdict"), Verdict.FAIL),
        criterion_results=criterion_results,
        judge_provider=_string(value.get("judge_provider"), "unknown"),
        judge_model=_optional_string(value.get("judge_model")),
        evaluated_at=_dt(value.get("evaluated_at")),
        summary=_string(value.get("summary")),
    )


def _attempt(raw: Any) -> Attempt:
    value = _mapping(raw)
    shot_id = _string(value.get("shot_id"))
    job_value = _mapping(value.get("provider_job"))
    job = None
    if job_value:
        job = ProviderJob(
            provider=_string(job_value.get("provider"), "unknown"),
            external_id=_string(job_value.get("external_id")),
            status=_string(job_value.get("status"), "queued"),
            idempotency_key=_optional_string(job_value.get("idempotency_key")),
            submitted_at=_dt(job_value.get("submitted_at")),
            metadata=_mapping_dict(job_value.get("metadata")),
        )
    cost_value = _mapping(value.get("cost"))
    cost = None
    if cost_value:
        cost = CostRecord(
            amount_usd=_finite_float(cost_value.get("amount_usd"), 0.0, minimum=0.0),
            currency=_string(cost_value.get("currency"), "USD"),
            provider=_optional_string(cost_value.get("provider")),
            model=_optional_string(cost_value.get("model")),
            estimated=_boolean(cost_value.get("estimated")),
            id=_string(cost_value.get("id")) or CostRecord.__dataclass_fields__["id"].default_factory(),
        )
    diagnosis_value = _mapping(value.get("diagnosis"))
    diagnosis = None
    if diagnosis_value:
        raw_repairs = diagnosis_value.get("recommended_repairs")
        diagnosis = Diagnosis(
            shot_id=_string(diagnosis_value.get("shot_id"), shot_id),
            failure_codes=_string_list(diagnosis_value.get("failure_codes")),
            root_causes=_string_list(diagnosis_value.get("root_causes")),
            recommended_repairs=[_enum(RepairKind, entry, RepairKind.HUMAN) for entry in raw_repairs] if isinstance(raw_repairs, (list, tuple)) else [RepairKind.HUMAN],
            confidence=_finite_float(diagnosis_value.get("confidence"), 0.0),
            id=_string(diagnosis_value.get("id")) or Diagnosis.__dataclass_fields__["id"].default_factory(),
        )
    repair_value = _mapping(value.get("repair_action"))
    repair = None
    if repair_value:
        repair = RepairAction(
            shot_id=_string(repair_value.get("shot_id"), shot_id),
            kind=_enum(RepairKind, repair_value.get("kind"), RepairKind.HUMAN),
            changes=_mapping_dict(repair_value.get("changes")),
            reason=_string(repair_value.get("reason")),
            estimated_cost_usd=_finite_float(repair_value.get("estimated_cost_usd"), 0.0, minimum=0.0),
            attempt_number=_integer(repair_value.get("attempt_number"), 1, minimum=1),
            id=_string(repair_value.get("id")) or RepairAction.__dataclass_fields__["id"].default_factory(),
        )
    raw_artifacts = value.get("artifacts")
    artifacts = [_artifact(item) for item in raw_artifacts] if isinstance(raw_artifacts, (list, tuple)) else []
    return Attempt(
        shot_id=shot_id,
        number=_integer(value.get("number"), 1, minimum=1),
        prompt_bundle=_prompt(value.get("prompt_bundle")) or PromptBundle(positive=""),
        provider_job=job,
        artifacts=artifacts,
        cost=cost,
        judge_result=_judge(value.get("judge_result")),
        diagnosis=diagnosis,
        repair_action=repair,
        status=_string(value.get("status"), "created"),
        id=_string(value.get("id")) or Attempt.__dataclass_fields__["id"].default_factory(),
    )


def _legacy_decode_project(raw: Mapping[str, Any]) -> Project:
    """Decode a snapshot defensively so schema evolution can resume projects."""
    brief_raw = dict(raw.get("brief") or {})
    brief_fields = CreativeBrief.__dataclass_fields__
    brief = CreativeBrief(**{key: value for key, value in brief_raw.items() if key in brief_fields})
    clarifications = [
        ClarificationTurn(**{key: value for key, value in item.items() if key in ClarificationTurn.__dataclass_fields__})
        for item in raw.get("clarification_turns", [])
    ]
    references = [_reference(item) for item in (raw.get("reference_assets") or [])]
    characters = [_character(item) for item in (raw.get("characters") or [])]
    locations = [_location(item) for item in (raw.get("locations") or [])]
    style = _style(raw.get("style_bible"))
    plans: list[PlanVersion] = []
    for plan_raw in raw.get("plans", []):
        scenes = [
            Scene(
                title=str(item.get("title", "")),
                summary=str(item.get("summary", "")),
                location_id=item.get("location_id"),
                time_of_day=item.get("time_of_day"),
                shot_ids=list(item.get("shot_ids", [])),
                id=str(item.get("id") or Scene.__dataclass_fields__["id"].default_factory()),
            )
            for item in plan_raw.get("scenes", [])
        ]
        shots: list[Shot] = []
        for item in plan_raw.get("shots", []):
            criteria = [
                AcceptanceCriterion(
                    statement=str(c.get("statement", "")),
                    category=_enum(CriterionCategory, c.get("category"), CriterionCategory.SUBJECT),
                    severity=_enum(Severity, c.get("severity"), Severity.BLOCKING),
                    evidence_types=list(c.get("evidence_types", ["frame"])),
                    threshold=c.get("threshold"),
                    blocking=bool(c.get("blocking", True)),
                    id=str(c.get("id") or AcceptanceCriterion.__dataclass_fields__["id"].default_factory()),
                )
                for c in item.get("acceptance_criteria", [])
            ]
            shots.append(
                Shot(
                    sequence=int(item.get("sequence", len(shots) + 1)),
                    scene_id=str(item.get("scene_id", "")),
                    title=str(item.get("title", "")),
                    description=str(item.get("description", "")),
                    duration_seconds=float(item.get("duration_seconds", brief.shot_duration_seconds)),
                    character_ids=list(item.get("character_ids", [])),
                    location_id=item.get("location_id"),
                    previous_shot_id=item.get("previous_shot_id"),
                    next_shot_id=item.get("next_shot_id"),
                    acceptance_criteria=criteria,
                    prompt_bundle=_prompt(item.get("prompt_bundle")),
                    depends_on_shot_ids=list(item.get("depends_on_shot_ids", [])),
                    id=str(item.get("id") or Shot.__dataclass_fields__["id"].default_factory()),
                )
            )
        cues = [
            AudioCue(
                kind=str(c.get("kind", "narration")),
                text=str(c.get("text", "")),
                start_seconds=float(c.get("start_seconds", 0.0)),
                duration_seconds=c.get("duration_seconds"),
                voice=c.get("voice"),
                parameters=dict(c.get("parameters") or {}),
                id=str(c.get("id") or AudioCue.__dataclass_fields__["id"].default_factory()),
            )
            for c in plan_raw.get("audio_cues", [])
        ]
        plans.append(
            PlanVersion(
                version=int(plan_raw.get("version", len(plans) + 1)),
                brief=brief,
                scenes=scenes,
                shots=shots,
                characters=[_character(item) for item in (plan_raw.get("characters") or [])] or characters,
                locations=[_location(item) for item in (plan_raw.get("locations") or [])] or locations,
                style_bible=_style(plan_raw.get("style_bible")) or style,
                reference_assets=[_reference(item) for item in (plan_raw.get("reference_assets") or [])] or references,
                audio_cues=cues,
                status=str(plan_raw.get("status", "draft")),
                approved_by=plan_raw.get("approved_by"),
                resolved_settings=dict(plan_raw.get("resolved_settings") or {}),
                id=str(plan_raw.get("id") or PlanVersion.__dataclass_fields__["id"].default_factory()),
            )
        )
    return Project(
        name=str(raw.get("name", brief.title)),
        brief=brief,
        status=_enum(ProjectStatus, raw.get("status"), ProjectStatus.CLARIFYING),
        clarification_turns=clarifications,
        plans=plans,
        attempts=[_attempt(item) for item in raw.get("attempts", [])],
        artifacts=[_artifact(item) for item in raw.get("artifacts", [])],
        total_cost_usd=float(raw.get("total_cost_usd", 0.0)),
        id=str(raw.get("id") or Project.__dataclass_fields__["id"].default_factory()),
        created_at=_dt(raw.get("created_at")),
        updated_at=_dt(raw.get("updated_at")),
        revision=_integer(raw.get("revision"), 0, minimum=0),
        root_project_id=_string(raw.get("root_project_id")) or None,
        parent_project_id=_string(raw.get("parent_project_id")) or None,
        version=_integer(raw.get("version"), 1, minimum=1),
    )


def _sanitize_brief(raw: Any) -> dict[str, Any]:
    value = _mapping(raw)
    return {
        "request": _string(value.get("request")),
        "title": _string(value.get("title"), "Untitled project"),
        "target_audience": _optional_string(value.get("target_audience")),
        "duration_seconds": _finite_float(value.get("duration_seconds"), 150.0, minimum=0.000001),
        "aspect_ratio": _string(value.get("aspect_ratio"), "16:9"),
        "fps": _integer(value.get("fps"), 24, minimum=1),
        "style": _optional_string(value.get("style")),
        "language": _string(value.get("language"), "zh-CN"),
        "content_constraints": _string_list(value.get("content_constraints")),
        "audio_required": _boolean(value.get("audio_required"), True),
        "shot_duration_seconds": _finite_float(value.get("shot_duration_seconds"), 15.0, minimum=0.000001),
        "max_shots": _integer(value.get("max_shots"), 10, minimum=1),
        "budget_usd": _finite_float(value.get("budget_usd"), 75.0, minimum=0.0),
        "parallelism_mode": _string(value.get("parallelism_mode"), "auto"),
        "parallelism": _integer(value.get("parallelism"), 0, minimum=1) if value.get("parallelism") is not None else None,
        "resolution_mode": _string(value.get("resolution_mode"), "auto"),
        "resolution_width": _integer(value.get("resolution_width"), 0, minimum=1) if value.get("resolution_width") is not None else None,
        "resolution_height": _integer(value.get("resolution_height"), 0, minimum=1) if value.get("resolution_height") is not None else None,
        "acceptance_mode": _string(value.get("acceptance_mode"), "standard"),
        "acceptance_custom": _optional_string(value.get("acceptance_custom")),
    }


def _sanitize_clarification(raw: Any) -> dict[str, Any] | None:
    value = _mapping(raw)
    if not value:
        return None
    result: dict[str, Any] = {
        "question": _string(value.get("question")),
        "answer": _optional_string(value.get("answer")),
        "required": _boolean(value.get("required"), True),
        "source": _string(value.get("source"), "agent"),
        "confidence": _finite_float(value.get("confidence"), 0.0),
        "confirmed": _boolean(value.get("confirmed")),
        "skipped": _boolean(value.get("skipped")),
        "options": [
            {
                "label": _string(_mapping(option).get("label")),
                "value": _string(_mapping(option).get("value")),
                "explanation": _string(_mapping(option).get("explanation")),
                **({"id": _string(_mapping(option).get("id"))} if _string(_mapping(option).get("id")) else {}),
            }
            for option in _items(value.get("options")) if isinstance(option, Mapping)
        ],
    }
    if _string(value.get("id")):
        result["id"] = _string(value.get("id"))
    return result


def _sanitize_scene(raw: Any) -> dict[str, Any] | None:
    value = _mapping(raw)
    if not value:
        return None
    result: dict[str, Any] = {
        "title": _string(value.get("title")),
        "summary": _string(value.get("summary")),
        "location_id": _optional_string(value.get("location_id")),
        "time_of_day": _optional_string(value.get("time_of_day")),
        "shot_ids": _string_list(value.get("shot_ids")),
    }
    if _string(value.get("id")):
        result["id"] = _string(value.get("id"))
    return result


def _sanitize_criterion(raw: Any) -> dict[str, Any] | None:
    value = _mapping(raw)
    if not value:
        return None
    result: dict[str, Any] = {
        "statement": _string(value.get("statement")),
        "category": value.get("category") if isinstance(value.get("category"), str) else CriterionCategory.SUBJECT.value,
        "severity": value.get("severity") if isinstance(value.get("severity"), str) else Severity.BLOCKING.value,
        "evidence_types": _string_list(value.get("evidence_types")) or ["frame"],
        "threshold": _optional_string(value.get("threshold")),
        "blocking": _boolean(value.get("blocking"), True),
    }
    if _string(value.get("id")):
        result["id"] = _string(value.get("id"))
    return result


def _sanitize_shot(raw: Any, sequence: int, shot_duration: float) -> dict[str, Any] | None:
    value = _mapping(raw)
    if not value:
        return None
    criteria = [_sanitize_criterion(item) for item in _items(value.get("acceptance_criteria"))]
    result: dict[str, Any] = {
        "sequence": _integer(value.get("sequence"), sequence, minimum=1),
        "scene_id": _string(value.get("scene_id")),
        "title": _string(value.get("title")),
        "description": _string(value.get("description")),
        "duration_seconds": _finite_float(value.get("duration_seconds"), shot_duration, minimum=0.000001),
        "character_ids": _string_list(value.get("character_ids")),
        "location_id": _optional_string(value.get("location_id")),
        "previous_shot_id": _optional_string(value.get("previous_shot_id")),
        "next_shot_id": _optional_string(value.get("next_shot_id")),
        "acceptance_criteria": [item for item in criteria if item is not None],
        "prompt_bundle": _mapping(value.get("prompt_bundle")) or None,
        "depends_on_shot_ids": _string_list(value.get("depends_on_shot_ids")),
    }
    if _string(value.get("id")):
        result["id"] = _string(value.get("id"))
    return result


def _sanitize_cue(raw: Any) -> dict[str, Any] | None:
    value = _mapping(raw)
    if not value:
        return None
    duration = value.get("duration_seconds")
    result: dict[str, Any] = {
        "kind": _string(value.get("kind"), "narration"),
        "text": _string(value.get("text")),
        "start_seconds": _finite_float(value.get("start_seconds"), 0.0, minimum=0.0),
        "duration_seconds": _finite_float(duration, 0.0, minimum=0.000001) if duration is not None else None,
        "voice": _optional_string(value.get("voice")),
        "parameters": _mapping_dict(value.get("parameters")),
    }
    if _string(value.get("id")):
        result["id"] = _string(value.get("id"))
    return result


def _sanitize_plan(raw: Any, version: int, brief: Mapping[str, Any]) -> dict[str, Any] | None:
    value = _mapping(raw)
    if not value:
        return None
    scenes = [_sanitize_scene(item) for item in _items(value.get("scenes"))]
    shots = [_sanitize_shot(item, index + 1, _finite_float(brief.get("shot_duration_seconds"), 15.0, minimum=0.000001)) for index, item in enumerate(_items(value.get("shots")))]
    cues = [_sanitize_cue(item) for item in _items(value.get("audio_cues"))]
    result: dict[str, Any] = {
        "version": _integer(value.get("version"), version, minimum=1),
        "scenes": [item for item in scenes if item is not None],
        "shots": [item for item in shots if item is not None],
        "characters": [item for item in _items(value.get("characters")) if isinstance(item, Mapping)],
        "locations": [item for item in _items(value.get("locations")) if isinstance(item, Mapping)],
        "style_bible": _mapping(value.get("style_bible")) or None,
        "reference_assets": [item for item in _items(value.get("reference_assets")) if isinstance(item, Mapping)],
        "audio_cues": [item for item in cues if item is not None],
        "status": _string(value.get("status"), "draft"),
        "approved_by": _optional_string(value.get("approved_by")),
        "resolved_settings": _mapping_dict(value.get("resolved_settings")),
    }
    if _string(value.get("id")):
        result["id"] = _string(value.get("id"))
    return result


def decode_project(raw: Mapping[str, Any]) -> Project:
    """Decode a project snapshot without letting malformed JSON break recovery."""
    if not isinstance(raw, Mapping):
        raise ValueError("Project snapshot must be a JSON object")  # noqa: TRY004 - stable storage contract
    brief = _sanitize_brief(raw.get("brief"))
    sanitized: dict[str, Any] = {
        "name": _string(raw.get("name"), brief["title"]),
        "brief": brief,
        "status": raw.get("status") if isinstance(raw.get("status"), str) else ProjectStatus.CLARIFYING.value,
        "clarification_turns": [item for item in (_sanitize_clarification(value) for value in _items(raw.get("clarification_turns"))) if item is not None],
        "reference_assets": [item for item in _items(raw.get("reference_assets")) if isinstance(item, Mapping)],
        "characters": [item for item in _items(raw.get("characters")) if isinstance(item, Mapping)],
        "locations": [item for item in _items(raw.get("locations")) if isinstance(item, Mapping)],
        "style_bible": _mapping(raw.get("style_bible")) or None,
        "plans": [],
        "attempts": _items(raw.get("attempts")),
        "artifacts": _items(raw.get("artifacts")),
        "total_cost_usd": _finite_float(raw.get("total_cost_usd"), 0.0, minimum=0.0),
        "id": _string(raw.get("id")),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "revision": _integer(raw.get("revision"), 0, minimum=0),
        "root_project_id": _string(raw.get("root_project_id")) or None,
        "parent_project_id": _string(raw.get("parent_project_id")) or None,
        "version": _integer(raw.get("version"), 1, minimum=1),
    }
    plan_values = _items(raw.get("plans"))
    sanitized["plans"] = [
        item
        for item in (_sanitize_plan(value, index + 1, brief) for index, value in enumerate(plan_values))
        if item is not None
    ]
    return _legacy_decode_project(sanitized)
