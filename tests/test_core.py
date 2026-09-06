from __future__ import annotations

import pytest

from video_director.cli import build_mock_orchestrator
from video_director.continuity import ContinuityGuardian
from video_director.execution import BudgetExceeded, DirectorOrchestrator
from video_director.providers.mock import (
    MockAssembler,
    MockAudioProvider,
    MockJudgeProvider,
    MockVideoProvider,
)
from video_director.quality import QualityController
from video_director.queue import DirectorWorker, SQLiteJobQueue
from video_director.scheduler import ProjectJobHandler, queue_project_run
from video_director.schemas import (
    AcceptanceCriterion,
    ArtifactRef,
    AssetKind,
    Attempt,
    CharacterBible,
    CreativeBrief,
    CriterionCategory,
    LocationBible,
    PlanVersion,
    Project,
    PromptBundle,
    ProviderJob,
    Scene,
    Shot,
    Verdict,
)
from video_director.store import EventStore, SnapshotConflict


def make_brief(shots: int = 10, budget: float = 75.0) -> CreativeBrief:
    return CreativeBrief(
        request="A cinematic story about a protagonist named Lin in a rain-lit city, then she returns a letter at dawn.",
        title="Fixture",
        duration_seconds=shots * 15.0,
        shot_duration_seconds=15.0,
        max_shots=shots,
        budget_usd=budget,
        audio_required=True,
    )


def ready_project(orchestrator, shots: int = 10, budget: float = 75.0):
    project = orchestrator.create_project(make_brief(shots, budget))
    if project.clarification_turns:
        orchestrator.answer_clarifications(project, {turn.id: "Approved creative facts" for turn in project.clarification_turns})
    orchestrator.plan(project)
    orchestrator.approve_plan(project, actor="test")
    return project


def test_ten_shot_e2e_has_acceptance_audio_and_provenance():
    orchestrator = build_mock_orchestrator(store_path=":memory:", fail_first_attempts=1)
    project = orchestrator.create_project(make_brief())
    if project.clarification_turns:
        orchestrator.answer_clarifications(project, {turn.id: "Approved creative facts" for turn in project.clarification_turns})
    project = orchestrator.run(project, approve_plan=True, actor="test")
    assert project.status.value == "awaiting_human"
    assert len(project.active_plan.shots) == 10
    assert all(shot.is_ready_to_generate() for shot in project.active_plan.shots)
    assert len(project.attempts) == 20
    assert {cue.kind for cue in project.active_plan.audio_cues} == {"narration", "music", "sfx", "subtitle"}
    assert project.artifacts[-1].metadata["audio_complete"] is True
    assert len(orchestrator.store.events(project.id)) >= 60


def test_event_store_round_trip_and_idempotency(tmp_path):
    store = EventStore(tmp_path / "director.db")
    orchestrator = build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1)
    store.save_project(project, event_type="fixture.saved")
    restored = store.load_project(project.id)
    assert restored is not None
    assert restored.active_plan and restored.active_plan.shots[0].prompt_bundle
    assert len(restored.active_plan.audio_cues) == 4
    assert store.claim_idempotency(project.id, "job-1", {"external_id": "x"})[0] is True
    claimed, result = store.claim_idempotency(project.id, "job-1", {"external_id": "y"})
    assert claimed is False and result == {"external_id": "x"}
    store.close()


def test_budget_hard_stop_before_next_shot():
    orchestrator = build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=2, budget=1.25)
    with pytest.raises(BudgetExceeded):
        orchestrator.run(project)
    assert project.status.value == "awaiting_human"
    assert any(event.event_type == "budget.hard_stop" for event in orchestrator.store.events(project.id))


def test_judge_fail_closed_when_evidence_is_missing():
    shot = Shot(1, "scene", "shot", "description", acceptance_criteria=[AcceptanceCriterion("subject visible", CriterionCategory.SUBJECT)])
    controller = QualityController(MockJudgeProvider())
    result = controller.evaluate(shot, [ArtifactRef(AssetKind.VIDEO, "mock://video.mp4", metadata={"quality": "good"})])
    assert result.verdict == Verdict.PASS
    result = controller.evaluate(shot, [])
    assert result.verdict == Verdict.FAIL
    assert result.failed_criteria[0].failure_code == "missing_evidence"


def test_continuity_reports_signature_drift():
    shot_a = Shot(1, "scene", "a", "a", character_ids=["char_a"], location_id="loc")
    shot_b = Shot(2, "scene", "b", "b", character_ids=["char_a"], location_id="loc", previous_shot_id=shot_a.id)
    from video_director.schemas import Project
    project = Project("x", make_brief(2))
    report = ContinuityGuardian().check(project, [shot_a, shot_b], {
        shot_a.id: [ArtifactRef(AssetKind.VIDEO, "a", metadata={"character_signature": "a"})],
        shot_b.id: [ArtifactRef(AssetKind.VIDEO, "b", metadata={"character_signature": "b"})],
    })
    assert report.passed is False
    assert any(issue.category == "character" for issue in report.issues)


def test_run_resumes_from_persisted_passed_shots(tmp_path):
    from video_director.providers.mock import MockVideoProvider

    video = MockVideoProvider(fail_first_attempts=0)
    original_submit = video.submit
    interrupted = {"value": True}

    def submit(request, context):
        if request.shot.sequence == 2 and interrupted["value"]:
            interrupted["value"] = False
            raise RuntimeError("simulated worker crash")
        return original_submit(request, context)

    video.submit = submit
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "director.db", fail_first_attempts=0)
    orchestrator.video_provider = video
    project = orchestrator.create_project(make_brief(shots=2, budget=20))
    if project.clarification_turns:
        orchestrator.answer_clarifications(project, {turn.id: "Approved creative facts" for turn in project.clarification_turns})
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        orchestrator.run(project, approve_plan=True, actor="test")
    restored = orchestrator.store.load_project(project.id)
    assert restored is not None
    assert any(attempt.status == "passed" and attempt.shot_id == restored.active_plan.shots[0].id for attempt in restored.attempts)
    project = orchestrator.run(restored, actor="recovery")
    assert project.status.value == "awaiting_human"
    assert project.artifacts[-1].metadata["video_count"] == 2
    assert any(event.event_type == "shot.resumed" for event in orchestrator.store.events(project.id))
    assert len([attempt for attempt in project.attempts if attempt.shot_id == project.active_plan.shots[0].id]) == 1


def test_retry_reassembles_delivery_after_new_pass(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "director.db", fail_first_attempts=1)
    project = ready_project(orchestrator, shots=1, budget=20)
    project = orchestrator.run(project)
    first_delivery = project.artifacts[-1].id
    shot = project.active_plan.shots[0]
    project = orchestrator.retry_shot(project, shot.id, actor="reviewer")
    assert project.status.value == "awaiting_human"
    assert project.artifacts[-1].id != first_delivery
    assert project.artifacts[-1].metadata["video_count"] == 1
    assert sum(event.event_type == "assembly.ready" for event in orchestrator.store.events(project.id)) == 2


def test_delivery_lineage_versions_and_supersedes_previous_artifact(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "delivery-lineage.db", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1, budget=20)
    project = orchestrator.run(project)
    first = project.artifacts[-1]
    assert first.metadata["artifact_role"] == "delivery"
    assert first.metadata["delivery_version"] == 1
    assert first.metadata["active"] is True
    project = orchestrator.retry_shot(project, project.active_plan.shots[0].id, actor="reviewer")
    second = project.artifacts[-1]
    assert second.id != first.id
    assert second.metadata["delivery_version"] == 2
    assert second.metadata["supersedes"] == first.id
    assert second.metadata["active"] is True
    assert first.metadata["active"] is False
    assert first.metadata["superseded"] is True
    assert first.metadata["superseded_by"] == second.id
    assert orchestrator._active_delivery(project).id == second.id

    # Delivery selection must survive snapshot reload and unrelated artifacts
    # appended after the delivery.  Lineage markers are authoritative, not
    # list position.
    project.artifacts.append(ArtifactRef(AssetKind.AUDIO, "mock://after-delivery.wav", metadata={"artifact_role": "preview"}))
    orchestrator.store.save_project(project, event_type="fixture.delivery-reload")
    restored = orchestrator.store.load_project(project.id)
    assert restored is not None
    assert orchestrator._active_delivery(restored).id == second.id
    assert sum(
        artifact.metadata.get("active") is True
        for artifact in restored.artifacts
        if artifact.metadata.get("artifact_role") == "delivery"
    ) == 1


def test_active_delivery_rejects_non_boolean_lineage_metadata():
    from video_director.schemas import Project

    project = Project("malformed delivery", make_brief(1))
    project.artifacts.extend(
        [
            ArtifactRef(AssetKind.VIDEO, "mock://bad-active.mp4", metadata={"artifact_role": "delivery", "active": "true", "delivery_version": 99}),
            ArtifactRef(AssetKind.VIDEO, "mock://bad-superseded.mp4", metadata={"artifact_role": "delivery", "active": True, "superseded": "false", "delivery_version": 100}),
        ]
    )
    assert DirectorOrchestrator._active_delivery(project) is None


def test_audio_cache_reuses_cues_and_invalidates_on_inputs_or_provider_version(tmp_path):
    class CountingAudioProvider(MockAudioProvider):
        version = "v1"

        def __init__(self):
            self.calls = []

        def synthesize(self, request, context):
            self.calls.append((request, context))
            return super().synthesize(request, context)

    audio = CountingAudioProvider()
    orchestrator = DirectorOrchestrator(
        video_provider=MockVideoProvider(fail_first_attempts=0),
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=audio,
        store=EventStore(tmp_path / "audio-cache.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    project = orchestrator.run(project)
    assert len(audio.calls) == 4
    cached = orchestrator._audio_for_project(project)
    assert len(cached) == 4
    assert len(audio.calls) == 4

    # A provider version change invalidates every cue without touching the old cache.
    audio.version = "v2"
    orchestrator._audio_for_project(project)
    assert len(audio.calls) == 8

    # Changing a cue's timeline position produces a distinct cache key.
    audio.version = "v3"
    project.active_plan.audio_cues[0].start_seconds = 3.0
    orchestrator._audio_for_project(project)
    assert len(audio.calls) == 12


def test_forty_shot_mock_fixture_completes_with_bounded_cost(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "director.db", fail_first_attempts=1)
    project = ready_project(orchestrator, shots=40, budget=125)
    project = orchestrator.run(project)
    assert project.status.value == "awaiting_human"
    assert len(project.active_plan.shots) == 40
    assert len(project.attempts) == 80
    assert project.total_cost_usd == pytest.approx(100.0)
    assert project.artifacts[-1].metadata["video_count"] == 40
    assert any(event.event_type == "generation.started" for event in orchestrator.store.events(project.id))


def test_judge_provider_exception_fails_closed_with_human_repair():
    class BrokenJudge:
        name = "broken-judge"

        def judge(self, _input):
            raise TimeoutError("judge timeout")

    shot = Shot(1, "scene", "shot", "description", acceptance_criteria=[AcceptanceCriterion("subject visible", CriterionCategory.SUBJECT)])
    result = QualityController(BrokenJudge()).evaluate(shot, [ArtifactRef(AssetKind.VIDEO, "mock://video.mp4")])
    assert result.verdict == Verdict.FAIL
    assert result.failed_criteria[0].failure_code == "judge_error"
    assert result.failed_criteria[0].repair_suggestions


def test_judge_low_confidence_and_unknown_criterion_fail_closed():
    class UncertainJudge:
        name = "uncertain-judge"

        def judge(self, input):
            from video_director.schemas import CriterionResult, JudgeResult

            expected = input.shot.acceptance_criteria[0]
            evidence = [video_director.schemas.Evidence("frame", "mock://frame")]
            return JudgeResult(input.shot.id, Verdict.PASS, [
                CriterionResult(expected.id, Verdict.PASS, evidence, confidence=0.1),
                CriterionResult("not-planned", Verdict.PASS, evidence, confidence=1.0),
            ], self.name)

    import video_director.schemas
    shot = Shot(1, "scene", "shot", "description", acceptance_criteria=[AcceptanceCriterion("subject visible", CriterionCategory.SUBJECT)])
    result = QualityController(UncertainJudge(), min_confidence=0.5).evaluate(shot, [ArtifactRef(AssetKind.VIDEO, "mock://video.mp4")])
    assert result.verdict == Verdict.FAIL
    assert {failure.failure_code for failure in result.failed_criteria} >= {"low_confidence", "unknown_criterion"}


def test_continuity_fails_closed_when_required_signature_is_missing():
    from video_director.schemas import Project

    project = Project("x", make_brief(2))
    project.plans.append(type("Plan", (), {"style_bible": object()})())
    first = Shot(1, "scene", "a", "a", character_ids=["char"], location_id="loc")
    second = Shot(2, "scene", "b", "b", character_ids=["char"], location_id="loc", previous_shot_id=first.id)
    report = ContinuityGuardian().check(project, [first, second], {
        first.id: [ArtifactRef(AssetKind.VIDEO, "a", metadata={})],
        second.id: [ArtifactRef(AssetKind.VIDEO, "b", metadata={})],
    })
    assert not report.passed
    assert any(issue.category == "evidence" for issue in report.issues)


def test_async_project_run_worker_completes_from_persisted_snapshot(tmp_path):
    store_path = tmp_path / "director.db"
    orchestrator = build_mock_orchestrator(store_path=store_path, fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1, budget=20)
    queue = SQLiteJobQueue(tmp_path / "queue.db")
    job = queue_project_run(queue, project.id, approve_plan=False, actor="api", revision="planned:1:0:0")

    worker = DirectorWorker(queue, ProjectJobHandler(orchestrator), worker_id="async-worker")
    processed = worker.run_once()

    assert processed and processed.id == job.id
    assert queue.get(job.id) and queue.get(job.id).status == "succeeded"
    restored = orchestrator.store.load_project(project.id)
    assert restored is not None
    assert restored.status.value == "awaiting_human"
    assert restored.artifacts[-1].metadata["video_count"] == 1
    assert queue.get(job.id).result["status"] == "awaiting_human"
    queue.close()


def test_inflight_provider_attempt_is_polled_after_worker_restart(tmp_path):
    class CrashOnceProvider(MockVideoProvider):
        def __init__(self):
            super().__init__(fail_first_attempts=0)
            self.submit_calls = 0
            self.crash_once = True

        def submit(self, request, context):
            self.submit_calls += 1
            return super().submit(request, context)

        def poll(self, job):
            if self.crash_once:
                self.crash_once = False
                raise RuntimeError("simulated worker interruption after submit")
            return super().poll(job)

    store_path = tmp_path / "director.db"
    provider = CrashOnceProvider()
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(store_path),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    with pytest.raises(RuntimeError, match="after submit"):
        orchestrator.run(project)

    persisted = orchestrator.store.load_project(project.id)
    assert persisted is not None
    assert len(persisted.attempts) == 1
    assert persisted.attempts[0].status == "submitted"
    assert persisted.attempts[0].provider_job is not None

    restarted = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(store_path),
    )
    restored = restarted.store.load_project(project.id)
    assert restored is not None
    restored = restarted.run(restored)
    assert restored.status.value == "awaiting_human"
    assert provider.submit_calls == 1
    assert len(restored.attempts) == 1
    assert restored.total_cost_usd == pytest.approx(1.25)
    assert any(event.event_type == "shot.generated.resumed" for event in restarted.store.events(project.id))


def test_project_transition_rejects_terminal_reopen_without_force():
    from video_director.schemas import Project, ProjectStatus

    project = Project("transitions", make_brief(1))
    with pytest.raises(ValueError, match="Illegal project transition"):
        project.transition(ProjectStatus.DELIVERED)
    project.transition(ProjectStatus.AWAITING_PLAN_APPROVAL)
    project.transition(ProjectStatus.PLANNED)
    project.transition(ProjectStatus.GENERATING)
    project.transition(ProjectStatus.AWAITING_HUMAN)
    project.transition(ProjectStatus.DELIVERED)
    with pytest.raises(ValueError, match="Illegal project transition"):
        project.transition(ProjectStatus.GENERATING)
    project.transition(ProjectStatus.GENERATING, force=True)


def test_provider_callback_resume_judges_without_resubmitting(tmp_path):
    class CallbackProvider(MockVideoProvider):
        def __init__(self):
            super().__init__(fail_first_attempts=0)
            self.submit_calls = 0

        def submit(self, request, context):
            self.submit_calls += 1
            return super().submit(request, context)

    provider = CallbackProvider()
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "callback.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    prompt = shot.prompt_bundle
    attempt = Attempt(shot.id, 1, prompt, provider_job=ProviderJob(provider.name, "external-1", status="submitted"), status="submitted")
    project.attempts.append(attempt)
    orchestrator.store.save_project(project, event_type="fixture.inflight")
    callback = {
        "project_id": project.id,
        "request_id": "external-1",
        "status": "completed",
        "output": {"video_url": "mock://callback/video.mp4"},
        "cost_usd": 1.25,
    }
    orchestrator.reconcile_provider_callback(project, provider.name, callback)
    restored = orchestrator.store.load_project(project.id)
    assert restored is not None
    assert restored.attempts[0].status == "generated"
    restored = orchestrator.run(restored)
    assert restored.status.value == "awaiting_human"
    assert restored.attempts[0].status == "passed"
    assert provider.submit_calls == 0


def test_provider_success_without_artifact_is_fail_closed_and_retried(tmp_path):
    class EmptyThenGoodProvider(MockVideoProvider):
        def __init__(self):
            super().__init__(fail_first_attempts=0)
            self.poll_calls = 0

        def poll(self, job):
            self.poll_calls += 1
            result = super().poll(job)
            if result.job.status == "succeeded" and self.poll_calls == 2:
                return type(result)(result.job, [], result.cost)
            return result

        def fetch_artifacts(self, job):
            request, _, _ = self._jobs[job.external_id]
            if int(request.parameters.get("attempt", 1)) == 1:
                return []
            return super().fetch_artifacts(job)

    provider = EmptyThenGoodProvider()
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "empty-artifact.db"),
        max_attempts=2,
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    result = orchestrator.run(project)
    assert result.status.value == "awaiting_human"
    assert len(result.attempts) == 2
    assert result.attempts[0].status == "provider_failed"
    assert any(event.payload.get("error") == "missing_artifact" for event in orchestrator.store.events(project.id))


def test_split_shot_repair_inserts_linked_node_and_executes_it(tmp_path):
    class SplitJudge(MockJudgeProvider):
        def judge(self, input):
            result = super().judge(input)
            if input.shot.sequence == 1 and input.artifacts[0].metadata.get("attempt") == 1:
                for criterion in result.criterion_results:
                    criterion.verdict = Verdict.FAIL
                    criterion.failure_code = "motion_overload"
                    criterion.repair_suggestions = [RepairKind.SPLIT_SHOT]
                result.verdict = Verdict.FAIL
            return result

    from video_director.schemas import RepairKind

    orchestrator = DirectorOrchestrator(
        video_provider=MockVideoProvider(fail_first_attempts=0),
        judge_provider=SplitJudge(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "split.db"),
        max_attempts=2,
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    result = orchestrator.run(project)
    assert result.status.value == "awaiting_human"
    assert len(result.active_plan.shots) == 2
    assert result.active_plan.shots[1].previous_shot_id == result.active_plan.shots[0].id
    assert any(event.event_type == "shot.split" for event in orchestrator.store.events(project.id))
    assert len([attempt for attempt in result.attempts if attempt.status == "passed"]) == 2


def test_plan_rollback_clones_ids_and_records_soft_budget_warning(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "rollback.db", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1, budget=1.5)
    original_plan = project.active_plan
    result = orchestrator.run(project)
    assert any(event.event_type == "budget.soft_warning" for event in orchestrator.store.events(project.id))
    rolled = orchestrator.rollback_plan(result, original_plan.version, actor="reviewer")
    assert rolled.active_plan.version == original_plan.version + 1
    assert rolled.active_plan.id != original_plan.id
    assert rolled.active_plan.shots[0].id != original_plan.shots[0].id
    assert rolled.status.value == "awaiting_plan_approval"


def test_project_version_clones_plan_entities_and_invalidates_rewound_delivery(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "project-version.db", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1, budget=20)
    project = orchestrator.run(project)
    project = orchestrator.deliver(project)
    source_plan = project.active_plan
    source_shot = source_plan.shots[0]
    source_delivery = project.artifacts[-1]

    version = orchestrator.create_project_version(project, actor="reviewer")

    assert version.version == 2
    assert version.root_project_id == project.root_project_id == project.id
    assert version.parent_project_id == project.id
    assert version.active_plan.id != source_plan.id
    assert version.active_plan.shots[0].id != source_shot.id

    orchestrator.rewind(project, "plan", actor="reviewer", reason="change the story structure")

    assert project.active_plan is None
    assert source_delivery.metadata["active"] is False
    assert source_delivery.metadata["superseded"] is True
    assert all(attempt.status == "invalidated" for attempt in project.attempts)


def test_run_is_idempotent_after_assembly(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "assembly-idempotent.db", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1, budget=20)
    project = orchestrator.run(project)
    assembly_id = project.artifacts[-1].id
    attempts = len(project.attempts)
    resumed = orchestrator.run(project)
    assert resumed.artifacts[-1].id == assembly_id
    assert len(resumed.attempts) == attempts
    assert sum(event.event_type == "assembly.ready" for event in orchestrator.store.events(project.id)) == 1


def test_judge_rejects_result_for_wrong_shot():
    class WrongShotJudge(MockJudgeProvider):
        def judge(self, input):
            result = super().judge(input)
            result.shot_id = "other-shot"
            return result

    shot = Shot(1, "scene", "shot", "description", acceptance_criteria=[AcceptanceCriterion("subject visible", CriterionCategory.SUBJECT)])
    result = QualityController(WrongShotJudge()).evaluate(shot, [ArtifactRef(AssetKind.VIDEO, "mock://video", metadata={"quality": "good"})])
    assert result.verdict == Verdict.FAIL
    assert result.failed_criteria[0].failure_code == "judge_error"


def test_plan_validate_rejects_broken_scene_and_shot_links():
    orchestrator = build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=2)
    plan = project.active_plan
    first, second = plan.shots
    first.next_shot_id = None
    errors = plan.validate()
    assert any("previous/next links are inconsistent" in error for error in errors)
    first.next_shot_id = second.id
    plan.scenes[0].shot_ids = [first.id, first.id]
    errors = plan.validate()
    assert any("belongs to multiple scenes" in error for error in errors)


def test_callback_zero_cost_is_recorded_once(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "callback-zero.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    attempt = Attempt(shot.id, 1, shot.prompt_bundle, provider_job=ProviderJob(provider.name, "external-zero", status="submitted"), status="submitted")
    project.attempts.append(attempt)
    orchestrator.store.save_project(project, event_type="fixture.inflight")
    callback = {"project_id": project.id, "request_id": "external-zero", "status": "succeeded", "output": {"video_url": "mock://zero.mp4"}, "cost_usd": 0}
    orchestrator.reconcile_provider_callback(project, provider.name, callback)
    assert project.total_cost_usd == 0
    assert project.attempts[0].cost is not None and project.attempts[0].cost.amount_usd == 0
    orchestrator.reconcile_provider_callback(project, provider.name, callback)
    assert project.total_cost_usd == 0


def test_callback_duplicate_and_late_terminal_events_are_idempotent(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "callback-terminal.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    attempt = Attempt(
        shot.id,
        1,
        shot.prompt_bundle,
        provider_job=ProviderJob(provider.name, "external-terminal", status="submitted"),
        status="submitted",
    )
    project.attempts.append(attempt)
    orchestrator.store.save_project(project, event_type="fixture.inflight")
    succeeded = {
        "project_id": project.id,
        "request_id": "external-terminal",
        "status": "succeeded",
        "output": {"video_url": "mock://terminal.mp4"},
        "cost_usd": 1.25,
        "event_id": "evt-success",
    }
    orchestrator.reconcile_provider_callback(project, provider.name, succeeded)
    assert project.total_cost_usd == pytest.approx(1.25)
    assert attempt.status == "generated"
    artifact_id = attempt.artifacts[0].id
    event_count = len(orchestrator.store.events(project.id))

    orchestrator.reconcile_provider_callback(project, provider.name, succeeded)
    assert project.total_cost_usd == pytest.approx(1.25)
    assert attempt.artifacts[0].id == artifact_id
    assert len(orchestrator.store.events(project.id)) == event_count

    late_failed = {
        "project_id": project.id,
        "request_id": "external-terminal",
        "status": "failed",
        "error": "provider reported a late failure",
        "event_id": "evt-late-failed",
    }
    orchestrator.reconcile_provider_callback(project, provider.name, late_failed)
    assert attempt.provider_job.status == "succeeded"
    assert attempt.status == "generated"
    assert project.total_cost_usd == pytest.approx(1.25)


def test_callback_rejects_provider_and_external_id_mismatches(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "callback-invalid.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    project.attempts.append(Attempt(shot.id, 1, shot.prompt_bundle, provider_job=ProviderJob(provider.name, "external-id", status="submitted"), status="submitted"))
    orchestrator.store.save_project(project, event_type="fixture.inflight")
    with pytest.raises(ValueError, match="provider name"):
        orchestrator.reconcile_provider_callback(project, "", {"request_id": "external-id", "status": "running"})
    with pytest.raises(ValueError, match="different provider"):
        orchestrator.reconcile_provider_callback(project, "other-provider", {"request_id": "external-id", "status": "running"})
    with pytest.raises(ValueError, match="external job id"):
        orchestrator.reconcile_provider_callback(project, provider.name, {"request_id": 123, "status": "running"})
    with pytest.raises(ValueError, match="external job id"):
        orchestrator.reconcile_provider_callback(project, provider.name, {"status": "running"})


def test_callback_rejects_unknown_provider_without_default_fallback(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "callback-unknown-provider.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    attempt = Attempt(
        shot.id,
        1,
        shot.prompt_bundle,
        provider_job=ProviderJob("ghost-provider", "external-ghost", status="submitted"),
        status="submitted",
    )
    project.attempts.append(attempt)
    orchestrator.store.save_project(project, event_type="fixture.unknown-provider")
    with pytest.raises(ValueError, match="unknown provider"):
        orchestrator.reconcile_provider_callback(
            project,
            "ghost-provider",
            {"request_id": "external-ghost", "status": "succeeded", "output": {"video_url": "mock://ghost.mp4"}, "cost_usd": 9.0},
        )
    assert attempt.status == "submitted"
    assert attempt.artifacts == []
    assert attempt.cost is None
    assert project.total_cost_usd == 0


def test_unknown_prompt_provider_fails_closed_without_submitting(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "prompt-unknown-provider.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    shot.prompt_bundle = PromptBundle(
        positive=shot.prompt_bundle.positive,
        provider="ghost-provider",
        parameters=dict(shot.prompt_bundle.parameters),
        reference_asset_ids=list(shot.prompt_bundle.reference_asset_ids),
    )
    with pytest.raises(ValueError, match="Unknown video provider"):
        orchestrator.run(project)
    assert provider._counter == 0
    assert len(project.attempts) == 1
    assert project.attempts[0].provider_job is None
    assert project.attempts[0].status == "created"


def test_unknown_persisted_job_provider_fails_closed_on_resume(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "job-unknown-provider.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    attempt = Attempt(
        shot.id,
        1,
        shot.prompt_bundle,
        provider_job=ProviderJob("ghost-provider", "job-ghost", status="submitted"),
        status="submitted",
    )
    project.attempts.append(attempt)
    orchestrator.store.save_project(project, event_type="fixture.unknown-job-provider")
    with pytest.raises(ValueError, match="Unknown video provider"):
        orchestrator.run(project)
    assert provider._counter == 0
    assert attempt.status == "submitted"


def test_snapshot_revision_compare_and_swap_rejects_stale_writer(tmp_path):
    path = tmp_path / "snapshot-cas.db"
    first_store = EventStore(path)
    second_store = EventStore(path)
    project = Project("cas", make_brief(1))
    first_store.save_project(project, event_type="fixture.created")
    assert project.revision == 1

    first = first_store.load_project(project.id)
    second = second_store.load_project(project.id)
    assert first is not None and second is not None
    assert first.revision == second.revision == 1

    first.name = "newer writer"
    first_store.save_project(first, event_type="fixture.newer")
    assert first.revision == 2

    second.name = "stale writer"
    with pytest.raises(SnapshotConflict, match="snapshot conflict") as conflict:
        second_store.save_project(second, event_type="fixture.stale")
    assert conflict.value.expected_revision == 1
    assert conflict.value.actual_revision == 2
    assert second.revision == 1
    restored = first_store.load_project(project.id)
    assert restored is not None
    assert restored.name == "newer writer"
    assert restored.revision == 2
    first_store.close()
    second_store.close()


def test_manual_retry_does_not_mutate_global_attempt_limit_or_other_shots(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "retry-isolation.db", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=2, budget=50)
    project = orchestrator.run(project)
    baseline_limit = orchestrator.max_attempts
    target, untouched = project.active_plan.shots
    target_attempts_before = len([attempt for attempt in project.attempts if attempt.shot_id == target.id])
    untouched_attempts_before = len([attempt for attempt in project.attempts if attempt.shot_id == untouched.id])

    project = orchestrator.retry_shot(project, target.id, actor="reviewer")
    assert orchestrator.max_attempts == baseline_limit
    assert len([attempt for attempt in project.attempts if attempt.shot_id == target.id]) == target_attempts_before + 1
    assert len([attempt for attempt in project.attempts if attempt.shot_id == untouched.id]) == untouched_attempts_before
    assert all(attempt.number == 1 for attempt in project.attempts if attempt.shot_id == untouched.id)


def test_late_success_callback_does_not_mutate_failed_attempt(tmp_path):
    provider = MockVideoProvider(fail_first_attempts=0)
    orchestrator = DirectorOrchestrator(
        video_provider=provider,
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(tmp_path / "callback-late-success.db"),
    )
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    attempt = Attempt(
        shot.id,
        1,
        shot.prompt_bundle,
        provider_job=ProviderJob(provider.name, "external-failed", status="failed"),
        status="provider_failed",
    )
    project.attempts.append(attempt)
    orchestrator.store.save_project(project, event_type="fixture.failed-terminal")
    callback = {
        "request_id": "external-failed",
        "status": "succeeded",
        "output": {"video_url": "mock://late-success.mp4"},
        "cost_usd": 7.5,
        "event_id": "late-success",
    }
    orchestrator.reconcile_provider_callback(project, provider.name, callback)
    assert attempt.provider_job.status == "failed"
    assert attempt.status == "provider_failed"
    assert attempt.artifacts == []
    assert attempt.cost is None
    assert project.total_cost_usd == 0
    assert any(event.event_type == "provider.callback.stale" for event in orchestrator.store.events(project.id))


def test_corrupt_nested_snapshot_recovers_ids_and_history(tmp_path):
    store = EventStore(tmp_path / "corrupt-snapshot.db")
    orchestrator = build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=1, budget=20)
    shot = project.active_plan.shots[0]
    attempt = Attempt(
        shot.id,
        1,
        shot.prompt_bundle,
        provider_job=ProviderJob("mock-video", "job-preserve", status="succeeded"),
        artifacts=[ArtifactRef(AssetKind.VIDEO, "mock://preserve.mp4", metadata={"keep": True})],
        status="passed",
    )
    project.attempts.append(attempt)
    project.artifacts.append(ArtifactRef(AssetKind.VIDEO, "mock://delivery.mp4", metadata={"artifact_role": "delivery", "active": True}))
    store.save_project(project, event_type="fixture.full")
    row = store.connection.execute("SELECT snapshot FROM projects WHERE id=?", (project.id,)).fetchone()
    import json

    snapshot = json.loads(row[0])
    snapshot["clarification_turns"] = ["not-an-object"]
    snapshot["plans"][0]["audio_cues"] = [None, {"id": "cue-preserve", "kind": "music", "start_seconds": "bad"}]
    snapshot["attempts"][0]["judge_result"] = {"criterion_results": "bad-shape"}
    snapshot["attempts"][0]["diagnosis"] = {"failure_codes": "bad-shape"}
    snapshot["attempts"][0]["repair_action"] = {"changes": "bad-shape"}
    snapshot["artifacts"][0]["metadata"] = "bad-metadata"
    store.connection.execute("UPDATE projects SET snapshot=? WHERE id=?", (json.dumps(snapshot), project.id))
    store.connection.commit()
    restored = store.load_project(project.id)
    assert restored is not None
    assert restored.id == project.id
    assert restored.active_plan.id == project.active_plan.id
    assert restored.active_plan.shots[0].id == shot.id
    assert restored.active_plan.audio_cues[0].id == "cue-preserve"
    assert restored.attempts[0].id == attempt.id
    assert restored.attempts[0].provider_job.external_id == "job-preserve"
    assert restored.attempts[0].artifacts[0].metadata == {"keep": True}
    assert restored.artifacts[0].id == project.artifacts[0].id
    store.close()


def test_split_shot_repairs_full_plan_topology_and_unique_ids(tmp_path):
    orchestrator = build_mock_orchestrator(store_path=tmp_path / "split-topology.db", fail_first_attempts=0)
    project = ready_project(orchestrator, shots=5, budget=30)
    plan = project.active_plan
    original = list(plan.shots)
    target = original[2]
    predecessor, successor = original[1], original[3]
    attempt = Attempt(target.id, 1, target.prompt_bundle)
    new_shot = orchestrator._split_shot(project, target, attempt)

    assert new_shot.previous_shot_id == target.id
    assert new_shot.next_shot_id == successor.id
    assert target.next_shot_id == new_shot.id
    assert successor.previous_shot_id == new_shot.id
    assert target.id in new_shot.depends_on_shot_ids
    assert target.id not in successor.depends_on_shot_ids
    assert predecessor.next_shot_id == target.id
    assert [shot.sequence for shot in plan.shots] == list(range(1, len(plan.shots) + 1))
    assert len({shot.id for shot in plan.shots}) == len(plan.shots)
    assert len({shot.prompt_bundle.id for shot in plan.shots}) == len(plan.shots)
    criterion_ids = [criterion.id for shot in plan.shots for criterion in shot.acceptance_criteria]
    assert len(criterion_ids) == len(set(criterion_ids))
    assert any(new_shot.id in scene.shot_ids for scene in plan.scenes)
    assert plan.validate() == []

    second_new = orchestrator._split_shot(project, new_shot, Attempt(new_shot.id, 1, new_shot.prompt_bundle))
    assert second_new.id not in {shot.id for shot in plan.shots if shot is not second_new}
    assert [shot.sequence for shot in plan.shots] == list(range(1, len(plan.shots) + 1))
    assert plan.validate() == []


def test_plan_validation_is_fail_closed_for_malformed_criterion_and_duration():
    brief = make_brief(shots=1)
    character = CharacterBible("Lin", "Lin")
    location = LocationBible("City", "City")
    scene = Scene("Scene", "Summary")
    shot = Shot(1, scene.id, "Shot", "Description", duration_seconds=0, character_ids=[character.id], location_id=location.id, acceptance_criteria=[AcceptanceCriterion("valid", CriterionCategory.SUBJECT)], prompt_bundle=PromptBundle("prompt"))
    shot.acceptance_criteria[0].statement = 7  # type: ignore[assignment]
    errors = PlanVersion(1, brief, [scene], [shot], [character], [location]).validate()
    assert any("malformed acceptance criterion" in error for error in errors)
    assert any("duration_seconds must be positive" in error for error in errors)


def test_plan_uses_ceiling_to_cover_requested_duration():
    brief = make_brief(shots=3)
    brief.duration_seconds = 31
    brief.max_shots = 3
    orchestrator = build_mock_orchestrator(store_path=":memory:", fail_first_attempts=0)
    project = orchestrator.create_project(brief)
    if project.clarification_turns:
        orchestrator.answer_clarifications(project, {turn.id: "Approved" for turn in project.clarification_turns})
    orchestrator.plan(project)
    assert len(project.active_plan.shots) == 3
