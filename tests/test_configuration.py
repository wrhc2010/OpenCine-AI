from __future__ import annotations

from fastapi.testclient import TestClient

from video_director import api
from video_director.execution import DirectorOrchestrator
from video_director.planning import PlanAgent
from video_director.providers.mock import MockAssembler, MockAudioProvider, MockJudgeProvider, MockVideoProvider
from video_director.schemas import ArtifactRef, AssetKind, CreativeBrief, as_jsonable
from video_director.store import EventStore
from video_director.quality import QualityController


def make_brief(**overrides) -> CreativeBrief:
    values = {
        "request": "一位主角在城市中归还一封信。",
        "title": "配置测试",
        "duration_seconds": 15,
        "shot_duration_seconds": 15,
        "max_shots": 1,
        "budget_usd": 10,
    }
    values.update(overrides)
    return CreativeBrief(**values)


def make_orchestrator(path) -> DirectorOrchestrator:
    return DirectorOrchestrator(
        video_provider=MockVideoProvider(fail_first_attempts=0),
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=EventStore(path),
        parallelism=2,
    )


def test_auto_parallelism_uses_orchestrator_default():
    plan = PlanAgent().create_plan(make_brief(), default_parallelism=4)
    assert plan.resolved_settings["parallelism"] == 4


def test_configuration_modes_reject_incomplete_presets():
    assert any("preset parallelism" in error for error in make_brief(parallelism_mode="preset", parallelism=3).validate())
    assert any("custom parallelism" in error for error in make_brief(parallelism_mode="custom").validate())
    assert any("preset resolution" in error for error in make_brief(resolution_mode="preset").validate())


def test_clarification_object_answer_and_skip_are_supported():
    orchestrator = make_orchestrator(":memory:")
    project = orchestrator.create_project(make_brief(request="一个故事。"))
    assert project.clarification_turns
    first, *rest = project.clarification_turns
    answers = {first.id: {"answer": "沿用一位主角"}}
    answers.update({turn.id: {"skip": True} for turn in rest})
    orchestrator.answer_clarifications(project, answers)
    assert project.status.value == "awaiting_plan_approval"
    updated_first = project.clarification_turns[0]
    assert updated_first.answer == "沿用一位主角"
    assert all(turn.confirmed for turn in project.clarification_turns)
    assert all(turn.skipped for turn in project.clarification_turns[1:])
    orchestrator.store.close()


def test_none_acceptance_skips_semantic_criteria_but_keeps_technical_gate():
    orchestrator = make_orchestrator(":memory:")
    project = orchestrator.create_project(make_brief(request="Lin 在城市归还信件。"))
    if project.clarification_turns:
        orchestrator.answer_clarifications(project, {turn.id: "已确认" for turn in project.clarification_turns})
    orchestrator.plan(project)
    shot = project.active_plan.shots[0]
    result = QualityController(MockJudgeProvider()).evaluate(
        shot,
        [ArtifactRef(AssetKind.VIDEO, "mock://video.mp4", duration_seconds=15)],
        {"acceptance_mode": "none"},
    )
    assert result.passed
    assert any(item.skipped for item in result.criterion_results if item.criterion_id != "")
    missing = QualityController(MockJudgeProvider()).evaluate(shot, [], {"acceptance_mode": "none"})
    assert not missing.passed
    assert any(item.failure_code == "missing_artifact" for item in missing.failed_criteria)
    orchestrator.store.close()


def test_api_project_settings_and_auth_lifecycle(monkeypatch, tmp_path):
    replacement = make_orchestrator(tmp_path / "api.db")
    monkeypatch.setattr(api, "orchestrator", replacement)
    api.projects.clear()
    client = TestClient(api.app)

    status = client.get("/v1/auth/status").json()
    assert status["enabled"] is True and status["initialized"] is False
    assert client.post("/v1/auth/setup", json={"username": "admin", "password": "password123"}).status_code == 200
    assert client.post("/v1/auth/setup", json={"username": "admin2", "password": "password123"}).status_code == 409
    login = client.post("/v1/auth/login", json={"username": "admin", "password": "password123"})
    assert login.status_code == 200
    assert client.get("/v1/auth/me").json()["authenticated"] is True

    project = client.post("/v1/projects", json=as_jsonable(make_brief())).json()
    updated = client.patch(f"/v1/projects/{project['id']}/settings", json={"acceptance_mode": "none", "duration_seconds": 15})
    assert updated.status_code == 200
    settings = client.get(f"/v1/projects/{project['id']}/settings").json()
    assert settings["settings"]["duration_seconds"] == 15
    assert settings["settings"]["acceptance_mode"] == "none"

    saved = client.patch("/v1/settings", json={"OPENAI_API_KEY": "secret-value", "DIRECTOR_PARALLELISM": 3})
    assert saved.status_code == 200
    assert saved.json()["settings"]["OPENAI_API_KEY"] == "已配置"
    preserved = client.patch("/v1/settings", json={"OPENAI_API_KEY": "已配置"})
    assert preserved.status_code == 200
    assert client.post("/v1/auth/logout").status_code == 200
    assert client.get("/v1/auth/me").status_code == 401
    replacement.store.close()
