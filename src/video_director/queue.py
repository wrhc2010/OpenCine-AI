"""Durable execution queue contracts and worker lease handling.

The queue is deliberately small and provider-neutral.  ``SQLiteJobQueue`` is
the default for local development and chaos tests; ``RedisStreamJobQueue``
uses Redis Streams consumer groups when the deployment enables Redis.  Both
implement the same lease/idempotency semantics, so orchestration code does not
need to know which broker is running.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_json(value: Any, default: Any, *, require_mapping: bool = False) -> tuple[Any, bool]:
    """Decode persisted JSON and report whether the value was malformed."""
    if value is None or value == "":
        return default, False
    try:
        decoded = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default, True
    if require_mapping and not isinstance(decoded, Mapping):
        return default, True
    return decoded, False


def _safe_datetime(value: Any, fallback: datetime | None = None) -> tuple[datetime | None, bool]:
    if value in (None, ""):
        return fallback, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC), False
        return value.astimezone(UTC), False
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return fallback, True
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
            return parsed, False
        except (TypeError, ValueError, OverflowError):
            pass
    return fallback, True


def _safe_int(value: Any, default: int = 0, *, minimum: int = 0) -> tuple[int, bool]:
    if isinstance(value, bool):
        return default, True
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default, True
    if number < minimum:
        return default, True
    return number, False


def _safe_text(value: Any, default: str = "") -> tuple[str, bool]:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return default, True
    if isinstance(value, str) and value.strip():
        return value, False
    return default, True


def _safe_optional_text(value: Any) -> tuple[str | None, bool]:
    """Decode nullable text fields where an empty Redis value means null."""
    if value is None or value == "":
        return None, False
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None, True
    if isinstance(value, str) and value.strip():
        return value, False
    return None, True


@dataclass(slots=True)
class Job:
    id: str
    project_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    attempts: int = 0
    available_at: datetime = field(default_factory=_now)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    idempotency_key: str | None = None
    last_error: str | None = None
    result: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


class JobQueue(Protocol):
    def enqueue(self, project_id: str, kind: str, payload: dict[str, Any] | None = None, *, idempotency_key: str | None = None, delay_seconds: float = 0.0) -> Job: ...
    def claim(self, *, worker_id: str, lease_seconds: float = 60.0) -> Job | None: ...
    def heartbeat(self, job_id: str, *, worker_id: str, lease_seconds: float = 60.0) -> bool: ...
    def complete(self, job_id: str, *, worker_id: str, result: Any | None = None) -> bool: ...
    def fail(self, job_id: str, *, worker_id: str, error: str, retry_delay_seconds: float = 0.0) -> bool: ...
    def cancel(self, job_id: str, *, reason: str = "cancelled") -> bool: ...
    def recover_expired(self) -> int: ...
    def get(self, job_id: str) -> Job | None: ...


class SQLiteJobQueue:
    """SQLite-backed queue with atomic claim and expiring worker leases."""

    def __init__(self, path: str | Path = ".data/director-queue.db", *, max_attempts: int = 5) -> None:
        raw = str(path)
        if raw.startswith("sqlite:///"):
            raw = raw.removeprefix("sqlite:///")
            if raw.startswith("/") and os.name == "nt":
                raw = raw[1:]
        self.path = raw
        self.max_attempts = max(1, max_attempts)
        if raw != ":memory:":
            Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(raw, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
              payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
              available_at TEXT NOT NULL, lease_owner TEXT, lease_expires_at TEXT,
              idempotency_key TEXT, last_error TEXT, result TEXT, metadata TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
              ON jobs(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, available_at, lease_expires_at);
            """
        )
        # Keep local queues forward-compatible with databases created by the
        # initial Phase 0 schema. SQLite has no ADD COLUMN IF NOT EXISTS.
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(jobs)").fetchall()}
        if "result" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN result TEXT")
        if "metadata" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        self.connection.commit()

    @staticmethod
    def _serialize(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _deserialize(value: Any) -> datetime | None:
        parsed, _ = _safe_datetime(value)
        return parsed

    @classmethod
    def _row(cls, row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        payload, payload_bad = _safe_json(row["payload"], {}, require_mapping=True)
        result, result_bad = _safe_json(row["result"], None)
        metadata, metadata_bad = _safe_json(row["metadata"], {}, require_mapping=True)
        attempts, attempts_bad = _safe_int(row["attempts"], 0)
        available_at, available_bad = _safe_datetime(row["available_at"], _now())
        lease_expires_at, lease_bad = _safe_datetime(row["lease_expires_at"])
        created_at, created_bad = _safe_datetime(row["created_at"], _now())
        updated_at, updated_bad = _safe_datetime(row["updated_at"], _now())
        id_value, id_bad = _safe_text(row["id"])
        project_value, project_bad = _safe_text(row["project_id"])
        kind_value, kind_bad = _safe_text(row["kind"])
        corrupt = any((payload_bad, result_bad, metadata_bad, attempts_bad, available_bad, lease_bad, created_bad, updated_bad, id_bad, project_bad, kind_bad))
        raw_status = row["status"]
        if isinstance(raw_status, (bytes, bytearray)):
            try:
                raw_status = bytes(raw_status).decode("utf-8")
            except UnicodeDecodeError:
                raw_status = None
        allowed_statuses = {"queued", "running", "succeeded", "failed", "cancelled"}
        status = raw_status if isinstance(raw_status, str) and raw_status in allowed_statuses else "failed"
        last_error = row["last_error"] if isinstance(row["last_error"], str) and row["last_error"] else None
        if raw_status not in allowed_statuses:
            corrupt = True
        if corrupt:
            status = "failed"
            last_error = "corrupt persisted job record"
        return Job(
            id=id_value, project_id=project_value, kind=kind_value,
            payload=dict(payload) if isinstance(payload, Mapping) else {}, status=status, attempts=attempts,
            available_at=available_at or _now(),
            lease_owner=row["lease_owner"] if isinstance(row["lease_owner"], str) and row["lease_owner"] else None, lease_expires_at=lease_expires_at,
            idempotency_key=row["idempotency_key"] if isinstance(row["idempotency_key"], str) and row["idempotency_key"] else None, last_error=last_error,
            result=result, metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
            created_at=created_at or _now(), updated_at=updated_at or _now(),
        )

    @staticmethod
    def _is_corrupt(job: Job | None) -> bool:
        return bool(job and job.last_error == "corrupt persisted job record")

    def _mark_corrupt(self, job_id: str, *, reason: str = "corrupt persisted job record") -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status='failed',last_error=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
                (reason, _now().isoformat(), job_id),
            )

    def enqueue(self, project_id: str, kind: str, payload: dict[str, Any] | None = None, *, idempotency_key: str | None = None, delay_seconds: float = 0.0) -> Job:
        now = _now()
        job = Job(uuid.uuid4().hex, project_id, kind, dict(payload or {}), available_at=now + timedelta(seconds=max(0.0, delay_seconds)), idempotency_key=idempotency_key, created_at=now, updated_at=now)
        with self.connection:
            if idempotency_key:
                existing = self.connection.execute("SELECT * FROM jobs WHERE project_id=? AND idempotency_key=?", (project_id, idempotency_key)).fetchone()
                if existing:
                    return self._row(existing)  # type: ignore[return-value]
            self.connection.execute(
                "INSERT INTO jobs (id,project_id,kind,payload,status,attempts,available_at,lease_owner,lease_expires_at,"
                "idempotency_key,last_error,result,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    job.project_id,
                    job.kind,
                    json.dumps(job.payload, ensure_ascii=False),
                    job.status,
                    job.attempts,
                    job.available_at.isoformat(),
                    None,
                    None,
                    job.idempotency_key,
                    None,
                    None,
                    json.dumps(job.metadata),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
        return job

    def claim(self, *, worker_id: str, lease_seconds: float = 60.0) -> Job | None:
        now = _now()
        lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
        with self.connection:
            self.recover_expired()
            while True:
                row = self.connection.execute("SELECT * FROM jobs WHERE status='queued' AND available_at<=? ORDER BY created_at LIMIT 1", (now.isoformat(),)).fetchone()
                if row is None:
                    return None
                candidate = self._row(row)
                if self._is_corrupt(candidate):
                    self._mark_corrupt(row["id"])
                    continue
                updated = self.connection.execute("UPDATE jobs SET status='running',attempts=attempts+1,lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND status='queued'", (worker_id, lease_until.isoformat(), now.isoformat(), row["id"])).rowcount
                if not updated:
                    continue
                return self.get(row["id"])

    def heartbeat(self, job_id: str, *, worker_id: str, lease_seconds: float = 60.0) -> bool:
        now = _now()
        lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
        with self.connection:
            count = self.connection.execute("UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE id=? AND status='running' AND lease_owner=?", (lease_until.isoformat(), now.isoformat(), job_id, worker_id)).rowcount
        return bool(count)

    def complete(self, job_id: str, *, worker_id: str, result: Any | None = None) -> bool:
        now = _now()
        encoded_result = None if result is None else json.dumps(result, ensure_ascii=False)
        with self.connection:
            count = self.connection.execute("UPDATE jobs SET status='succeeded',result=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND status='running' AND lease_owner=?", (encoded_result, now.isoformat(), job_id, worker_id)).rowcount
        return bool(count)

    def fail(self, job_id: str, *, worker_id: str, error: str, retry_delay_seconds: float = 0.0) -> bool:
        now = _now()
        row = self.connection.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        attempts = int(row[0]) if row else self.max_attempts
        status = "queued" if retry_delay_seconds >= 0 and attempts < self.max_attempts else "failed"
        available = now + timedelta(seconds=max(0.0, retry_delay_seconds))
        with self.connection:
            count = self.connection.execute("UPDATE jobs SET status=?,last_error=?,available_at=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND status='running' AND lease_owner=?", (status, error, available.isoformat(), now.isoformat(), job_id, worker_id)).rowcount
        return bool(count)

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> bool:
        with self.connection:
            count = self.connection.execute("UPDATE jobs SET status='cancelled',last_error=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND status IN ('queued','running')", (reason, _now().isoformat(), job_id)).rowcount
        return bool(count)

    def recover_expired(self) -> int:
        now = _now()
        with self.connection:
            count = self.connection.execute("UPDATE jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,lease_owner=NULL,lease_expires_at=NULL,available_at=?,updated_at=? WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?", (self.max_attempts, now.isoformat(), now.isoformat(), now.isoformat())).rowcount
        return int(count)

    def get(self, job_id: str) -> Job | None:
        job = self._row(self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if self._is_corrupt(job):
            self._mark_corrupt(job_id)
        return job

    def list(self, *, project_id: str | None = None, status: str | None = None) -> list[Job]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id=?"; params.append(project_id)
        if status:
            query += " AND status=?"; params.append(status)
        query += " ORDER BY created_at"
        jobs: list[Job] = []
        for row in self.connection.execute(query, params).fetchall():
            job = self._row(row)
            if job is not None:
                if self._is_corrupt(job):
                    self._mark_corrupt(job.id)
                jobs.append(job)
        return jobs

    def close(self) -> None:
        self.connection.close()


class RedisStreamJobQueue:
    """Redis Streams consumer-group adapter. Requires the optional ``redis`` package."""

    def __init__(self, url: str | None = None, *, stream: str = "video-director.jobs", group: str = "video-director-workers", max_attempts: int = 5, client: Any | None = None) -> None:
        try:
            import redis
        except ImportError as error:  # pragma: no cover - optional deployment dependency
            raise RuntimeError("Redis queue requires redis>=5") from error
        self.redis = client or redis.Redis.from_url(url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        self.stream = stream
        self.group = group
        self.max_attempts = max(1, max_attempts)
        self._stream_ids: dict[str, str] = {}
        self._job_prefix = f"{self.stream}:job:"
        self._idempotency_prefix = f"{self.stream}:idempotency:"
        # Redis Streams do not have a native delayed-delivery primitive. Keep
        # future jobs in a sorted set and promote them to the stream when a
        # worker polls; this keeps the public queue contract identical to the
        # SQLite implementation without requiring an extra scheduler service.
        self._schedule_key = f"{self.stream}:scheduled"
        self._claim_lock_prefix = f"{self.stream}:claim-lock:"
        self._group_ready = False

    def _ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as error:  # BUSYGROUP is expected on startup
            if "BUSYGROUP" not in str(error):
                raise RuntimeError(f"Redis stream is unavailable: {error}") from error
        self._group_ready = True

    def _atomic(self, builder):
        """Execute Redis commands in one transaction when supported.

        A tiny in-memory fake is useful for contract tests and may not expose
        ``pipeline``; in that case the builder runs directly against the
        client. Production Redis always takes the MULTI/EXEC path.
        """
        factory = getattr(self.redis, "pipeline", None)
        if not callable(factory):
            return builder(self.redis)
        try:
            pipe = factory(transaction=True)
        except TypeError:
            pipe = factory()
        builder(pipe)
        return pipe.execute()

    def _acquire_claim_lock(self, job_id: str, worker_id: str) -> str | None:
        """Serialize competing claims for one durable job across workers."""
        setter = getattr(self.redis, "set", None)
        if not callable(setter):
            return None
        token = f"{worker_id}:{uuid.uuid4().hex}"
        key = self._claim_lock_prefix + job_id
        try:
            acquired = setter(key, token, nx=True, ex=10)
        except TypeError:
            try:
                acquired = setter(key, token, nx=True)
            except (AttributeError, OSError, RuntimeError, ValueError):
                return None
        except (AttributeError, OSError, RuntimeError, ValueError):
            return None
        return token if acquired else ""

    def _release_claim_lock(self, job_id: str, token: str | None) -> None:
        if not token:
            return
        key = self._claim_lock_prefix + job_id
        try:
            if self.redis.get(key) == token:
                self.redis.delete(key)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return

    def enqueue(self, project_id: str, kind: str, payload: dict[str, Any] | None = None, *, idempotency_key: str | None = None, delay_seconds: float = 0.0) -> Job:
        if not self._group_ready:
            self._ensure_group()
        if idempotency_key:
            existing_id = self.redis.get(self._idempotency_prefix + f"{project_id}:{idempotency_key}")
            if existing_id:
                existing = self.get(existing_id)
                if existing:
                    return existing
        job = Job(uuid.uuid4().hex, project_id, kind, dict(payload or {}), idempotency_key=idempotency_key)
        job.available_at = _now() + timedelta(seconds=max(0.0, delay_seconds))
        idempotency_redis_key = self._idempotency_prefix + f"{project_id}:{idempotency_key}" if idempotency_key else None
        if idempotency_redis_key and not self.redis.set(idempotency_redis_key, job.id, nx=True):
            existing_id = self.redis.get(idempotency_redis_key)
            # A concurrent publisher may have reserved the key just before it
            # finished writing its hash. Give that writer a brief window to
            # publish the durable record instead of reporting a false error.
            existing = None
            for _ in range(10):
                existing = self.get(existing_id) if existing_id else None
                if existing is not None:
                    break
                time.sleep(0.01)
            if existing:
                return existing
            raise RuntimeError("idempotency key is currently being claimed")
        try:
            # Persist the hash before publishing the stream entry. A consumer
            # can therefore always decode a complete job, even when it runs in
            # another process immediately after XADD.
            if delay_seconds > 0:
                self._atomic(lambda pipe: (
                    pipe.hset(self._job_prefix + job.id, mapping=self._encode_job(job)),
                    pipe.zadd(self._schedule_key, {job.id: job.available_at.timestamp()}),
                ))
            else:
                self._atomic(lambda pipe: (
                    pipe.hset(self._job_prefix + job.id, mapping=self._encode_job(job)),
                    pipe.xadd(self.stream, {"job_id": job.id}),
                ))
        except Exception:
            if idempotency_redis_key:
                self.redis.delete(idempotency_redis_key)
            self.redis.delete(self._job_prefix + job.id)
            if delay_seconds > 0:
                self.redis.zrem(self._schedule_key, job.id)
            raise
        # Do not write the stream id after XADD. A consumer can legitimately
        # claim the message between those two operations and persist its own
        # running state; leaving the id out until claim avoids an old queued
        # snapshot overwriting that state. The message id is carried by the
        # stream delivery and recorded by claim().
        return job

    @staticmethod
    def _encode_job(job: Job) -> dict[str, str]:
        return {
            "id": job.id,
            "project_id": job.project_id,
            "kind": job.kind,
            "payload": json.dumps(job.payload, ensure_ascii=False),
            "status": job.status,
            "attempts": str(job.attempts),
            "available_at": job.available_at.isoformat(),
            "lease_owner": job.lease_owner or "",
            "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else "",
            "idempotency_key": job.idempotency_key or "",
            "last_error": job.last_error or "",
            "result": json.dumps(job.result, ensure_ascii=False) if job.result is not None else "",
            "metadata": json.dumps(job.metadata, ensure_ascii=False),
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }

    @staticmethod
    def _decode_job(values: Mapping[str, Any]) -> Job:
        """Decode a Redis hash without allowing one corrupt hash to crash a worker."""
        payload, payload_bad = _safe_json(values.get("payload", "{}"), {}, require_mapping=True)
        result, result_bad = _safe_json(values.get("result"), None)
        metadata, metadata_bad = _safe_json(values.get("metadata", "{}"), {}, require_mapping=True)
        attempts, attempts_bad = _safe_int(values.get("attempts", 0), 0)
        available_at, available_bad = _safe_datetime(values.get("available_at"), _now())
        lease_expires_at, lease_bad = _safe_datetime(values.get("lease_expires_at"))
        created_at, created_bad = _safe_datetime(values.get("created_at"), _now())
        updated_at, updated_bad = _safe_datetime(values.get("updated_at"), _now())
        id_value, id_bad = _safe_text(values.get("id"))
        project_value, project_bad = _safe_text(values.get("project_id"))
        kind_value, kind_bad = _safe_text(values.get("kind"))
        corrupt = any((payload_bad, result_bad, metadata_bad, attempts_bad, available_bad, lease_bad, created_bad, updated_bad, id_bad, project_bad, kind_bad))
        raw_status = values.get("status")
        if isinstance(raw_status, (bytes, bytearray)):
            try:
                raw_status = bytes(raw_status).decode("utf-8")
            except UnicodeDecodeError:
                raw_status = None
        allowed_statuses = {"queued", "running", "succeeded", "failed", "cancelled"}
        status = raw_status if isinstance(raw_status, str) and raw_status in allowed_statuses else "failed"
        last_error, last_error_bad = _safe_optional_text(values.get("last_error"))
        if raw_status not in allowed_statuses:
            corrupt = True
        corrupt = corrupt or last_error_bad
        if corrupt:
            status = "failed"
            last_error = "corrupt persisted job record"
        return Job(
            id=id_value,
            project_id=project_value,
            kind=kind_value,
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            status=status,
            attempts=attempts,
            available_at=available_at or _now(),
            lease_owner=_safe_optional_text(values.get("lease_owner"))[0],
            lease_expires_at=lease_expires_at,
            idempotency_key=_safe_optional_text(values.get("idempotency_key"))[0],
            last_error=last_error,
            result=result,
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
            created_at=created_at or _now(),
            updated_at=updated_at or _now(),
        )

    def _mark_corrupt(self, job_id: str, *, reason: str = "corrupt persisted job record") -> None:
        self.redis.hset(
            self._job_prefix + job_id,
            mapping={"status": "failed", "last_error": reason, "lease_owner": "", "lease_expires_at": "", "updated_at": _now().isoformat()},
        )

    def _promote_due(self, *, limit: int = 100) -> int:
        """Move due delayed jobs into the consumer-group stream."""
        now = _now().timestamp()
        try:
            candidates = self.redis.zrangebyscore(self._schedule_key, "-inf", now, start=0, num=max(1, limit))
        except (AttributeError, TypeError, ValueError):
            return 0
        promoted = 0
        for raw_job_id in candidates or []:
            job_id = str(raw_job_id)
            job = self.get(job_id)
            if job is None or job.status != "queued":
                self.redis.zrem(self._schedule_key, job_id)
                continue
            if job.available_at > _now():
                self.redis.zadd(self._schedule_key, {job.id: job.available_at.timestamp()})
                continue
            try:
                # The durable hash is already queued and contains no lease.
                # Publish before removing the schedule entry: if a worker dies
                # between these operations, a later promotion may create a
                # duplicate stream entry, but the terminal-state check in
                # claim() will acknowledge it without executing the job twice.
                # This ordering guarantees a delayed job is never lost.
                self._atomic(lambda pipe, job_id=job.id: (
                    pipe.xadd(self.stream, {"job_id": job_id}),
                    pipe.zrem(self._schedule_key, job_id),
                ))
                promoted += 1
            except Exception:  # noqa: BLE001 - keep delayed jobs recoverable on transient Redis errors
                # Leave the job durable and eligible for a later promotion if
                # Redis briefly rejects XADD.
                self.redis.zadd(self._schedule_key, {job.id: job.available_at.timestamp()})
        return promoted

    def claim(self, *, worker_id: str, lease_seconds: float = 60.0) -> Job | None:
        if not self._group_ready:
            self._ensure_group()
        self._promote_due()
        # Reclaim messages left pending by a crashed worker before taking new
        # work. XAUTOCLAIM is available in Redis 6.2+.
        rows = []
        try:
            claimed_result = self.redis.xautoclaim(self.stream, self.group, worker_id, int(max(1.0, lease_seconds) * 1000), start_id="0-0", count=1)
            claimed = claimed_result[1] if isinstance(claimed_result, (tuple, list)) and len(claimed_result) > 1 else []
            if claimed:
                rows = [(self.stream, claimed)]
        except (AttributeError, RuntimeError, TypeError, ValueError):
            rows = []
        if not rows:
            rows = self.redis.xreadgroup(self.group, worker_id, {self.stream: ">"}, count=1, block=1)
        if not rows:
            return None
        _, messages = rows[0]
        if not isinstance(messages, (list, tuple)):
            return None
        for message in messages:
            if not isinstance(message, (list, tuple)) or len(message) != 2:
                continue
            message_id, values = message
            if not isinstance(values, Mapping):
                self.redis.xack(self.stream, self.group, message_id)
                continue
            raw_job_id = values.get("job_id")
            if not isinstance(raw_job_id, str) or not raw_job_id.strip():
                self.redis.xack(self.stream, self.group, message_id)
                continue
            job_id = raw_job_id.strip()
            lock_token = self._acquire_claim_lock(job_id, worker_id)
            if lock_token == "":
                # Another worker is claiming this job. Keep this delivery
                # pending so it can be reclaimed if the other worker crashes.
                continue
            try:
                stored = self.get(job_id)
                if stored is None:
                    self.redis.xack(self.stream, self.group, message_id)
                    continue
                if stored.available_at > _now():
                    # A stale/legacy stream entry may point at a future job.
                    self.redis.xack(self.stream, self.group, message_id)
                    self.redis.zadd(self._schedule_key, {stored.id: stored.available_at.timestamp()})
                    continue
                if stored.last_error == "corrupt persisted job record" or stored.status in {"failed", "cancelled", "succeeded"}:
                    self.redis.xack(self.stream, self.group, message_id)
                    self._stream_ids.pop(job_id, None)
                    continue
                assigned_stream_id = stored.metadata.get("stream_id") if isinstance(stored.metadata, Mapping) else None
                if stored.status == "running" and assigned_stream_id and str(assigned_stream_id) != str(message_id):
                    # Delayed promotion/recovery can leave a duplicate stream
                    # entry behind. Only the delivery recorded in the job hash
                    # is allowed to execute; acknowledge the duplicate.
                    self.redis.xack(self.stream, self.group, message_id)
                    continue
                now = _now()
                if stored.status == "running" and stored.lease_expires_at and stored.lease_expires_at > now and assigned_stream_id is None:
                    # Legacy hashes created before stream_id provenance was
                    # persisted are conservatively treated as in-flight.
                    self.redis.xack(self.stream, self.group, message_id)
                    continue
                stored.status = "running"
                stored.attempts += 1
                stored.lease_owner = worker_id
                stored.lease_expires_at = now + timedelta(seconds=max(1.0, lease_seconds))
                stored.updated_at = now
                stored.metadata["stream_id"] = message_id
                self.redis.hset(self._job_prefix + job_id, mapping=self._encode_job(stored))
                self._stream_ids[job_id] = message_id
                return stored
            finally:
                self._release_claim_lock(job_id, lock_token)
        return None

    def heartbeat(self, job_id: str, *, worker_id: str, lease_seconds: float = 60.0) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running" or job.lease_owner != worker_id:
            return False
        job.lease_expires_at = _now() + timedelta(seconds=max(1.0, lease_seconds))
        job.updated_at = _now()
        self.redis.hset(self._job_prefix + job_id, mapping=self._encode_job(job))
        return True

    def complete(self, job_id: str, *, worker_id: str, result: Any | None = None) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running" or job.lease_owner != worker_id:
            return False
        message_id = self._stream_ids.pop(job_id, None) or job.metadata.get("stream_id")
        if not message_id:
            return False
        # Persist the terminal state before acknowledging the delivery. If the
        # process dies after HSET but before XACK, the duplicate delivery will
        # observe ``succeeded`` and be acknowledged without executing again.
        job.status = "succeeded"
        job.result = result
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = _now()
        result_values = self._atomic(lambda pipe: (
            pipe.hset(self._job_prefix + job_id, mapping=self._encode_job(job)),
            pipe.xack(self.stream, self.group, message_id),
        ))
        return bool(result_values and result_values[-1])

    def fail(self, job_id: str, *, worker_id: str, error: str, retry_delay_seconds: float = 0.0) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running" or job.lease_owner != worker_id:
            return False
        message_id = self._stream_ids.pop(job_id, None) or job.metadata.get("stream_id")
        if not message_id:
            return False
        job.last_error = error
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = _now()
        job.status = "queued" if retry_delay_seconds >= 0 and job.attempts < self.max_attempts else "failed"
        # Save the new state and acknowledge the current delivery together. A
        # terminal failed job is still written before XACK, while a queued
        # retry is made visible only after its durable hash is updated.
        if job.status == "queued":
            job.available_at = _now() + timedelta(seconds=max(0.0, retry_delay_seconds))
            # Persist the queued state before publishing a retry. This closes
            # the consumer race where an old running hash could be claimed and
            # then overwritten by the publisher's final HSET.
            job.metadata.pop("stream_id", None)
            if retry_delay_seconds > 0:
                # Delayed retries are invisible to consumers until due.
                result_values = self._atomic(lambda pipe: (
                    pipe.hset(self._job_prefix + job.id, mapping=self._encode_job(job)),
                    pipe.xack(self.stream, self.group, message_id),
                    pipe.zadd(self._schedule_key, {job.id: job.available_at.timestamp()}),
                ))
            else:
                result_values = self._atomic(lambda pipe: (
                    pipe.hset(self._job_prefix + job.id, mapping=self._encode_job(job)),
                    pipe.xack(self.stream, self.group, message_id),
                    pipe.xadd(self.stream, {"job_id": job.id}),
                ))
                # xadd is the final command in the transaction; retain its id
                # locally for completion by this process. The durable hash is
                # intentionally left without stream_id until the next claim.
                if result_values and result_values[-1]:
                    self._stream_ids[job.id] = result_values[-1]
        else:
            result_values = self._atomic(lambda pipe: (
                pipe.hset(self._job_prefix + job.id, mapping=self._encode_job(job)),
                pipe.xack(self.stream, self.group, message_id),
            ))
        return bool(result_values and result_values[1])

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> bool:
        job = self.get(job_id)
        if not job or job.status in {"succeeded", "failed", "cancelled"}:
            return False
        message_id = self._stream_ids.pop(job_id, None) or job.metadata.get("stream_id")
        if message_id:
            self.redis.xack(self.stream, self.group, message_id)
        self.redis.zrem(self._schedule_key, job.id)
        job.status = "cancelled"
        job.last_error = reason
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = _now()
        self.redis.hset(self._job_prefix + job.id, mapping=self._encode_job(job))
        return True

    def recover_expired(self) -> int:
        if not self._group_ready:
            self._ensure_group()
        recovered = 0
        now = _now()
        for key in self.redis.scan_iter(match=self._job_prefix + "*"):
            values = self.redis.hgetall(key)
            job = self._decode_job(values)
            if job.last_error == "corrupt persisted job record":
                self._mark_corrupt(str(key).removeprefix(self._job_prefix))
                recovered += 1
                continue
            if job.status == "running" and job.lease_expires_at and job.lease_expires_at <= now:
                job.status = "queued" if job.attempts < self.max_attempts else "failed"
                job.lease_owner = None
                job.lease_expires_at = None
                if job.status == "queued":
                    old_message_id = job.metadata.get("stream_id")
                    job.metadata.pop("stream_id", None)
                    # Recovery must be all-or-nothing.  A separate HSET/XACK
                    # followed by XADD can lose a job if the process exits
                    # after acknowledging the old delivery but before the
                    # replacement reaches the stream.  Redis MULTI/EXEC (or
                    # the injected fake's equivalent) commits the queued
                    # snapshot, old-message acknowledgement and replacement
                    # publication together.
                    result_values = self._atomic(lambda pipe, key=key, job=job, old_message_id=old_message_id: (
                        pipe.hset(key, mapping=self._encode_job(job)),
                        pipe.xack(self.stream, self.group, old_message_id) if old_message_id else 0,
                        pipe.xadd(self.stream, {"job_id": job.id}),
                    ))
                    if result_values and result_values[-1]:
                        self._stream_ids[job.id] = result_values[-1]
                else:
                    old_message_id = job.metadata.get("stream_id")
                    result_values = self._atomic(lambda pipe, key=key, job=job, old_message_id=old_message_id: (
                        pipe.hset(key, mapping=self._encode_job(job)),
                        pipe.xack(self.stream, self.group, old_message_id) if old_message_id else 0,
                    ))
                    self._stream_ids.pop(job.id, None)
                recovered += 1
        return recovered

    def get(self, job_id: str) -> Job | None:
        values = self.redis.hgetall(self._job_prefix + job_id)
        if not values:
            return None
        job = self._decode_job(values)
        if job.last_error == "corrupt persisted job record":
            self._mark_corrupt(job_id)
        return job


def make_job_queue(url: str | None = None) -> JobQueue:
    value = url or os.getenv("REDIS_URL")
    if value and value.startswith("redis://"):
        return RedisStreamJobQueue(value)
    return SQLiteJobQueue(value or ".data/director-queue.db")


class DirectorWorker:
    """Small worker loop with lease heartbeat and deterministic shutdown."""

    def __init__(self, queue: JobQueue, handler, *, worker_id: str | None = None, lease_seconds: float = 60.0, max_failures: int = 5) -> None:
        self.queue = queue
        self.handler = handler
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.max_failures = max(1, max_failures)
        self.running = True

    def run_once(self) -> Job | None:
        job = self.queue.claim(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        heartbeat_stop = threading.Event()
        heartbeat_period = max(0.25, self.lease_seconds / 3)

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(heartbeat_period):
                self.queue.heartbeat(job.id, worker_id=self.worker_id, lease_seconds=self.lease_seconds)

        heartbeat_thread = threading.Thread(target=heartbeat_loop, name=f"heartbeat-{job.id[:8]}", daemon=True)
        heartbeat_thread.start()
        try:
            result = self.handler(job)
        except Exception as error:  # noqa: BLE001 - worker must release its lease
            retry_delay = 0.0 if job.attempts < self.max_failures else -1.0
            self.queue.fail(job.id, worker_id=self.worker_id, error=str(error), retry_delay_seconds=retry_delay)
            return job
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=heartbeat_period)
        self.queue.complete(job.id, worker_id=self.worker_id, result=result)
        return job

    def stop(self) -> None:
        self.running = False

    def run(self, *, max_jobs: int | None = None, idle_cycles: int | None = None) -> int:
        processed = 0
        idle = 0
        while self.running and (max_jobs is None or processed < max_jobs):
            job = self.run_once()
            if job is None:
                idle += 1
                if idle_cycles is not None and idle >= idle_cycles:
                    break
                continue
            idle = 0
            processed += 1
        return processed
