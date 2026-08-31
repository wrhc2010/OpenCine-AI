from __future__ import annotations

import threading

import pytest

from video_director import cli
from video_director.execution import BudgetExceeded, DirectorOrchestrator
from video_director.providers.base import ProviderContext, VideoRequest
from video_director.providers.mock import (
    MockAssembler,
    MockAudioProvider,
    MockJudgeProvider,
    MockVideoProvider,
)
from video_director.schemas import CreativeBrief, RepairKind
from video_director.store import EventStore


def make_brief(shots: int, budget: float = 100.0) -> CreativeBrief:
    return CreativeBrief(
        request="A cinematic story about Lin in a rain-lit city who returns a letter at dawn with narration and music.",
        title="Parallel fixture",
        duration_seconds=shots * 15.0,
        shot_duration_seconds=15.0,
        max_shots=shots,
        budget_usd=budget,
        audio_required=True,
    )


def make_orchestrator(provider: MockVideoProvider, *, parallelism: int = 2, budget: float = 100.0) -> DirectorOrchestrator:
    return DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(":memory:"),
        parallelism=parallelism,
    )


def ready_project(orchestrator: DirectorOrchestrator, shots: int, budget: float = 100.0):
    project = orchestrator.create_project(make_brief(shots, budget))
    if project.clarification_turns:
        orchestrator.answer_clarifications(project, {turn.id: "Approved creative facts" for turn in project.clarification_turns})
    orchestrator.plan(project)
    orchestrator.approve_plan(project, actor="test")
    return project


def make_independent(project) -> None:
    for shot in project.active_plan.shots:
        shot.depends_on_shot_ids = []


class BarrierVideoProvider(MockVideoProvider):
    name = "barrier-video"

    def __init__(self, parties: int) -> None:
        super().__init__(fail_first_attempts=0, cost_usd=1.0)
        self.barrier = threading.Barrier(parties)

    def submit(self, request: VideoRequest, context: ProviderContext):
        self.barrier.wait(timeout=5)
        return super().submit(request, context)


class RecordingVideoProvider(MockVideoProvider):
    name = "recording-video"

    def __init__(self) -> None:
        super().__init__(fail_first_attempts=0, cost_usd=1.0)
        self.events: list[tuple[str, str]] = []
        self.events_lock = threading.Lock()

    def _record(self, kind: str, shot_id: str) -> None:
        with self.events_lock:
            self.events.append((kind, shot_id))

    def submit(self, request: VideoRequest, context: ProviderContext):
        self._record("submit", request.shot.id)
        return super().submit(request, context)

    def poll(self, job):
        result = super().poll(job)
        if result.job.status == "succeeded":
            request = self._jobs[job.external_id][0]
            self._record("success", request.shot.id)
        return result


class CrashOnceVideoProvider(MockVideoProvider):
    name = "crash-once-video"

    def __init__(self) -> None:
        super().__init__(fail_first_attempts=0, cost_usd=1.0)
        self.crash_shot_id: str | None = None
        self.crashed = False

    def submit(self, request: VideoRequest, context: ProviderContext):
        if request.shot.id == self.crash_shot_id and not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated parallel worker crash")
        return super().submit(request, context)


def test_parallel_independent_shots_overlap_and_merge_results():
    provider = BarrierVideoProvider(parties=2)
    orchestrator = make_orchestrator(provider, parallelism=2)
    project = ready_project(orchestrator, shots=4)
    make_independent(project)

    result = orchestrator.run(project)

    assert result.status.value == "awaiting_human"
    assert result.artifacts[-1].metadata["video_count"] == 4
    assert len([attempt for attempt in result.attempts if attempt.status == "passed"]) == 4
    assert result.total_cost_usd == 4.0
    assert sum(event.event_type == "scheduler.wave.started" for event in orchestrator.store.events(result.id)) == 2


def test_parallel_dependency_ready_shot_waits_for_prerequisite():
    provider = RecordingVideoProvider()
    orchestrator = make_orchestrator(provider, parallelism=2)
    project = ready_project(orchestrator, shots=3)
    first, second, dependent = project.active_plan.shots
    first.depends_on_shot_ids = []
    second.depends_on_shot_ids = []
    dependent.depends_on_shot_ids = [first.id]

    result = orchestrator.run(project)

    assert result.status.value == "awaiting_human"
    assert provider.events.index(("success", first.id)) < provider.events.index(("submit", dependent.id))


def test_parallel_worker_failure_is_persisted_and_recovers_without_duplicate_attempt():
    provider = CrashOnceVideoProvider()
    orchestrator = make_orchestrator(provider, parallelism=2)
    project = ready_project(orchestrator, shots=2)
    make_independent(project)
    provider.crash_shot_id = project.active_plan.shots[1].id

    with pytest.raises(RuntimeError, match="parallel worker crash"):
        orchestrator.run(project)
    restored = orchestrator.store.load_project(project.id)
    assert restored is not None and restored.status.value == "failed"
    assert any(attempt.status == "passed" for attempt in restored.attempts)

    recovered = orchestrator.run(restored, actor="recovery")
    assert recovered.status.value == "awaiting_human"
    assert recovered.artifacts[-1].metadata["video_count"] == 2
    for shot in recovered.active_plan.shots:
        attempts = [attempt for attempt in recovered.attempts if attempt.shot_id == shot.id]
        assert len(attempts) == 1
        assert attempts[0].status == "passed"


def test_parallel_budget_hard_stop_prevents_next_wave():
    provider = MockVideoProvider(fail_first_attempts=0, cost_usd=1.25)
    orchestrator = make_orchestrator(provider, parallelism=2)
    project = ready_project(orchestrator, shots=4, budget=2.5)
    make_independent(project)

    with pytest.raises(BudgetExceeded):
        orchestrator.run(project)
    assert project.status.value == "awaiting_human"
    assert project.total_cost_usd == 2.5
    assert len([attempt for attempt in project.attempts if attempt.status == "passed"]) == 2
    assert any(event.event_type == "budget.hard_stop" for event in orchestrator.store.events(project.id))


def test_parallel_split_shot_is_scheduled_in_a_follow_up_wave():
    class SplitJudge(MockJudgeProvider):
        def judge(self, input):
            result = super().judge(input)
            if input.shot.sequence == 1 and input.artifacts[0].metadata.get("attempt") == 1:
                for criterion in result.criterion_results:
                    criterion.verdict = "FAIL"
                    criterion.failure_code = "motion_overload"
                    criterion.repair_suggestions = [RepairKind.SPLIT_SHOT]
                result.verdict = "FAIL"
            return result

    orchestrator = DirectorOrchestrator(
        video_provider=MockVideoProvider(fail_first_attempts=0, cost_usd=1.0),
        judge_provider=SplitJudge(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(":memory:"),
        max_attempts=2,
        parallelism=2,
    )
    project = ready_project(orchestrator, shots=2, budget=20)
    make_independent(project)

    result = orchestrator.run(project)

    assert result.status.value == "awaiting_human"
    plan = result.active_plan
    assert plan is not None
    assert len(plan.shots) == 3
    assert [shot.sequence for shot in plan.shots] == [1, 2, 3]
    assert plan.validate() == []
    assert len([attempt for attempt in result.attempts if attempt.status == "passed"]) == 3
    assert result.artifacts[-1].metadata["video_count"] == 3
    split_events = [event for event in orchestrator.store.events(result.id) if event.event_type == "shot.split"]
    assert split_events and split_events[0].payload["new_shot_id"] == plan.shots[1].id
    waves = [event for event in orchestrator.store.events(result.id) if event.event_type == "scheduler.wave.started"]
    assert len(waves) == 2
    assert plan.shots[1].depends_on_shot_ids == [plan.shots[0].id]


def test_parallelism_factory_reads_environment_and_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("DIRECTOR_PARALLELISM", "3")
    orchestrator = cli.build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)
    assert orchestrator.parallelism == 3
    orchestrator.store.close()

    monkeypatch.setenv("DIRECTOR_PARALLELISM", "0")
    with pytest.raises(ValueError, match="DIRECTOR_PARALLELISM"):
        cli.build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)

    monkeypatch.setenv("DIRECTOR_PARALLELISM", "not-a-number")
    with pytest.raises(ValueError, match="DIRECTOR_PARALLELISM"):
        cli.build_orchestrator(store_path=":memory:", fail_first_attempts=0)
