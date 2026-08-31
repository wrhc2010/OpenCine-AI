from __future__ import annotations

from datetime import UTC, datetime, timedelta

from video_director.queue import DirectorWorker, RedisStreamJobQueue, SQLiteJobQueue


def test_queue_idempotency_and_lease_recovery(tmp_path):
    queue = SQLiteJobQueue(tmp_path / "jobs.db", max_attempts=3)
    first = queue.enqueue("project-1", "project.run", {"x": 1}, idempotency_key="run-1")
    duplicate = queue.enqueue("project-1", "project.run", {"x": 2}, idempotency_key="run-1")
    assert duplicate.id == first.id

    claimed = queue.claim(worker_id="worker-a", lease_seconds=1)
    assert claimed and claimed.status == "running" and claimed.attempts == 1
    assert queue.heartbeat(claimed.id, worker_id="worker-b") is False

    # Simulate a process crash by expiring the lease in the durable row.
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    with queue.connection:
        queue.connection.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (expired, claimed.id))
    assert queue.recover_expired() == 1
    resumed = queue.claim(worker_id="worker-b")
    assert resumed and resumed.id == claimed.id and resumed.attempts == 2
    assert queue.complete(resumed.id, worker_id="worker-b", result={"ok": True})
    assert queue.get(resumed.id).status == "succeeded"
    queue.close()


def test_worker_retries_then_marks_terminal_failure(tmp_path):
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    queue.enqueue("project-1", "broken")
    calls = []

    def handler(job):
        calls.append(job.attempts)
        raise RuntimeError("boom")

    worker = DirectorWorker(queue, handler, worker_id="worker", max_failures=2)
    worker.run(max_jobs=3, idle_cycles=1)
    job = queue.list()[0]
    assert calls == [1, 2]
    assert job.status == "failed"
    assert job.last_error == "boom"
    queue.close()


def test_sqlite_corrupt_row_is_failed_and_does_not_block_next_job(tmp_path):
    queue = SQLiteJobQueue(tmp_path / "corrupt.db")
    with queue.connection:
        queue.connection.execute(
            "INSERT INTO jobs (id,project_id,kind,payload,status,attempts,available_at,lease_owner,lease_expires_at,"
            "idempotency_key,last_error,result,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "bad",
                "project",
                "broken",
                "[]",
                "queued",
                "not-an-int",
                "not-a-date",
                None,
                None,
                None,
                None,
                "{bad-json",
                "[]",
                "not-a-date",
                "not-a-date",
            ),
        )
    good = queue.enqueue("project", "good", {"ok": True})

    corrupt = queue.get("bad")
    assert corrupt is not None
    assert corrupt.status == "failed"
    assert corrupt.last_error == "corrupt persisted job record"
    persisted = queue.connection.execute("SELECT status,last_error FROM jobs WHERE id='bad'").fetchone()
    assert tuple(persisted) == ("failed", "corrupt persisted job record")

    claimed = queue.claim(worker_id="worker")
    assert claimed is not None and claimed.id == good.id
    queue.close()


def test_redis_hash_decoder_fails_closed_for_bad_shapes():
    values = {
        "id": "redis-job",
        "project_id": "project",
        "kind": "render",
        "payload": "[]",
        "status": "queued",
        "attempts": "NaN",
        "available_at": "invalid-date",
        "metadata": "[]",
        "created_at": "invalid-date",
        "updated_at": "invalid-date",
    }
    job = RedisStreamJobQueue._decode_job(values)
    assert job.status == "failed"
    assert job.last_error == "corrupt persisted job record"
    assert job.payload == {} and job.metadata == {} and job.attempts == 0


def test_redis_queue_enqueue_claim_complete_and_idempotency():
    fakeredis = __import__("fakeredis")
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisStreamJobQueue(client=client, stream="contract.jobs", group="workers")
    first = queue.enqueue("project", "render", {"n": 1}, idempotency_key="run-1")
    duplicate = queue.enqueue("project", "render", {"n": 2}, idempotency_key="run-1")
    assert duplicate.id == first.id
    claimed = queue.claim(worker_id="worker-a", lease_seconds=5)
    assert claimed and claimed.id == first.id and claimed.attempts == 1
    assert queue.heartbeat(first.id, worker_id="worker-b") is False
    assert queue.complete(first.id, worker_id="worker-a", result={"ok": True}) is True
    assert queue.get(first.id).status == "succeeded"
    assert queue.claim(worker_id="worker-b") is None


def test_redis_queue_delayed_job_promotes_only_when_due():
    fakeredis = __import__("fakeredis")
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisStreamJobQueue(client=client, stream="delay.jobs", group="workers")
    delayed = queue.enqueue("project", "render", delay_seconds=60)
    assert queue.claim(worker_id="worker") is None
    with client.pipeline(transaction=True) as pipe:
        pipe.hset(queue._job_prefix + delayed.id, mapping={"available_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
        pipe.zadd(queue._schedule_key, {delayed.id: (datetime.now(UTC) - timedelta(seconds=1)).timestamp()})
        pipe.execute()
    claimed = queue.claim(worker_id="worker")
    assert claimed and claimed.id == delayed.id
