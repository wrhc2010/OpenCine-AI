# Architecture

## Runtime boundary

The Python core owns Creative IR, project state, event history and policy. The
orchestrator is a domain state machine; it does not expose FastAPI, LangGraph,
SQLAlchemy or vendor SDK types. Frameworks and providers can therefore change
without rewriting project semantics.

```text
brief -> clarification -> plan approval -> queue
                                      |
                         worker -> provider job -> artifacts
                                      |
                         criterion judge -> diagnosis -> repair/retry
                                      |
                    continuity -> audio/video assembly -> delivery approval
```

## Creative IR

`CreativeBrief`, `PlanVersion`, `Scene`, `Shot`, bibles, references,
`AcceptanceCriterion`, `PromptBundle`, `AudioCue`, `Attempt`, `JudgeResult`,
`Diagnosis`, `RepairAction` and `ArtifactRef` are the source-of-truth types. A
Shot cannot be approved for generation without both a PromptBundle and at least
one AcceptanceCriterion. Every generation records prompt version, parameters,
reference IDs, provider job, cost and artifacts.

## Quality policy

Judges return one result per planned criterion. Missing evidence, missing
criteria, malformed verdicts and provider timeouts fail closed. The repair policy
orders low-cost prompt/parameter changes before reference refresh, provider
switching, shot splitting and human escalation. Automatic attempts are bounded.

Continuity runs after each completed shot set and immediately before assembly.
The baseline implementation compares character, location and style signatures;
embedding and audio identity providers can be added behind the same report.

## Queue and recovery

The API can enqueue `project.run` or `shot.retry` commands. `SQLiteJobQueue` is a
durable local queue for development and chaos tests. `RedisStreamJobQueue` uses a
consumer group, pending-message reclamation and Redis hashes for Job state. Both
support idempotency keys, worker leases, heartbeat, cancellation and bounded
failure handling. A project snapshot is loaded before execution, so a restarted
worker skips passed Shots and resumes the next incomplete Shot.

### Dependency-ready waves

When DIRECTOR_PARALLELISM is 1, the scheduler follows the same dependency graph
serially. Values greater than 1 enable bounded waves: ready_shots selects only
Shots whose depends_on_shot_ids have passed, then the scheduler takes up to the
configured number for the current wave. A cycle, unknown dependency or empty
ready set is a fail-closed scheduler block and moves the project to
awaiting_human.

Each wave starts from a deep-copied baseline and each Shot executes in an
isolated in-memory EventStore. The merge step applies only fields that differ
from that worker baseline, so a worker cannot restore stale attempts or erase
another worker's repair. Attempt cost deltas, newly inserted split Shots, scene
membership, refreshed references and provider lifecycle state are merged under
a lock and persisted with the normal snapshot CAS. The worker EventStore is then
replayed into the main stream with parallel_worker_shot_id and
parallel_worker_event_id metadata, preserving audit detail such as shot.split,
shot.repair_planned, shot.submitted and shot.judged.

Split-Shot repair changes the active plan topology. The new segment is added to
pending_shot_ids and can only run in a subsequent dependency-ready wave; it is
never treated as passed merely because the parent Shot produced artifacts.
Completed Shots are cached by their passed Attempt, so a worker restart or a
replayed command does not submit a duplicate attempt.

The current implementation shares Provider instances between worker threads.
Mock providers use locking where needed; production adapters should provide
thread-safe HTTP clients, a per-thread client, or an explicit Provider pool.
The 40-Shot benchmark should therefore use Mock/Replay Providers first and
measure queue latency, retry cost, recovery and duplicate callback behavior
before enabling paid providers.

## Persistence

SQLite and PostgreSQL EventStore implementations expose the same methods:
snapshot upsert, append-only events, event queries, project load and
idempotency claims. Project snapshots carry a monotonic `revision`; writes use
compare-and-swap semantics and raise `SnapshotConflict` when a stale worker
tries to overwrite newer state. This keeps a worker restart or duplicate
command from silently losing another worker's progress. Object storage is
represented by `ArtifactRef`;
`LocalArtifactStore` provides content-addressed development storage and
`S3ArtifactStore` maps the same contract to S3/MinIO.

## Deployment

Docker Compose runs API, worker, PostgreSQL, Redis and MinIO. For local work,
SQLite plus the mock provider keeps the full loop deterministic and free.
Production deployments should use Postgres, Redis Streams, S3-compatible object
storage and a process supervisor for API/worker health and restart.

The Compose file supplies the infrastructure endpoints and passes
DIRECTOR_PARALLELISM to both API and worker. It does not provision model
credentials, configure a VLM judge, or create a MinIO bucket policy; those are
deployment concerns. The local API falls back to the Mock provider when a
cloud URL is not configured so health checks remain useful on a clean checkout.
