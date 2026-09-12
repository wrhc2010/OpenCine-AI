"""Queue-backed project execution services.

The API only enqueues a command; a worker owns the long-running orchestration
and writes every state transition through the same EventStore.  This keeps HTTP
requests short and makes a crashed worker safe to resume from the project
snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from .execution import BudgetExceeded, DirectorOrchestrator, HumanGate
from .queue import Job, JobQueue
from .store import SnapshotConflict


@dataclass(slots=True)
class ProjectJobResult:
    project_id: str
    status: str
    error: str | None = None


class ProjectJobHandler:
    """Execute queue commands against an orchestrator and persisted project."""

    def __init__(self, orchestrator: DirectorOrchestrator, *, snapshot_retries: int = 3, before_execute: Callable[[], None] | None = None) -> None:
        self.orchestrator = orchestrator
        self.snapshot_retries = max(0, int(snapshot_retries))
        self.before_execute = before_execute

    def __call__(self, job: Job) -> dict[str, Any]:
        if self.before_execute is not None:
            self.before_execute()
        payload = job.payload
        command = job.kind
        for conflict_attempt in range(self.snapshot_retries + 1):
            project = self.orchestrator.store.load_project(job.project_id)
            if project is None:
                raise LookupError(f"Project {job.project_id} was not found")
            try:
                result = self._execute(project, command, payload)
            except SnapshotConflict:
                if conflict_attempt >= self.snapshot_retries:
                    raise
                continue
            except (HumanGate, BudgetExceeded) as error:
                # These are deliberate policy pauses, not transient worker
                # errors. The project snapshot already contains the gate state;
                # acknowledge the queue job and let the operator decide.
                self.orchestrator.store.append_event(project.id, "job.awaiting_human", {"job_id": job.id, "kind": command, "reason": str(error)})
                return {"project_id": project.id, "status": project.status.value, "awaiting_human": True, "reason": str(error)}
            return {"project_id": result.id, "status": result.status.value}
        raise RuntimeError("unreachable snapshot conflict retry state")

    def _execute(self, project, command: str, payload: dict[str, Any]):
        if command in {"project.run", "run"}:
            return self.orchestrator.run(
                project,
                approve_plan=_as_bool(payload.get("approve_plan", False)),
                actor=str(payload.get("actor", "worker")),
            )
        if command in {"shot.retry", "retry"}:
            return self.orchestrator.retry_shot(
                project,
                str(payload["shot_id"]),
                actor=str(payload.get("actor", "worker")),
                force=_as_bool(payload.get("force", False)),
            )
        if command in {"shot.generate", "generate"}:
            return self.orchestrator.generate_shot(
                project,
                str(payload["shot_id"]),
                actor=str(payload.get("actor", "worker")),
                force=_as_bool(payload.get("force", False)),
            )
        if command in {"project.deliver", "deliver"}:
            return self.orchestrator.deliver(project, actor=str(payload.get("actor", "worker")))
        raise ValueError(f"Unsupported project job kind: {command}")


def is_terminal_project_error(error: Exception) -> bool:
    return isinstance(error, (LookupError, ValueError, HumanGate, BudgetExceeded))


def queue_project_run(queue: JobQueue, project_id: str, *, approve_plan: bool = False, actor: str = "api", revision: str | None = None) -> Job:
    suffix = revision or "initial"
    key = f"{project_id}:project.run:{int(approve_plan)}:{suffix}"
    return queue.enqueue(project_id, "project.run", {"approve_plan": approve_plan, "actor": actor}, idempotency_key=key)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
