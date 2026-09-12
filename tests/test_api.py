from __future__ import annotations

from fastapi.testclient import TestClient

from video_director.artifacts import FFmpegAssembler
from video_director.api import app, queue
from video_director.cli import build_mock_orchestrator
from video_director.schemas import ArtifactRef, AssetKind, ProjectStatus
from video_director.store import SnapshotConflict


def test_api_clarify_plan_run_and_delivery():
    client = TestClient(app)
    response = client.post(
        "/v1/projects",
        json={
            "title": "API fixture",
            "request": "A short film about a character named Lin in a city who returns a letter at dawn.",
            "duration_seconds": 15,
            "shot_duration_seconds": 15,
            "max_shots": 1,
            "budget_usd": 10,
        },
    )
    assert response.status_code == 201
    project = response.json()
    assert project["status"] == "clarifying"
    answers = {turn["id"]: "Approved by API fixture" for turn in project["clarification_turns"]}
    response = client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    assert response.status_code == 200
    response = client.post(f"/v1/projects/{project['id']}/plan")
    assert response.status_code == 200
    response = client.post(f"/v1/projects/{project['id']}/run", json={"approve_plan": True})
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_human"
    response = client.post(f"/v1/projects/{project['id']}/deliver", json={"actor": "api-test"})
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    events = client.get(f"/v1/projects/{project['id']}/events").json()
    assert any(event["event_type"] == "project.delivered" for event in events)


def test_api_async_run_exposes_job_and_cancel():
    client = TestClient(app)
    response = client.post(
        "/v1/projects",
        json={
            "title": "Async fixture",
            "request": "A short film about Lin in a city returning a letter at dawn with narration and music.",
            "duration_seconds": 15,
            "shot_duration_seconds": 15,
            "max_shots": 1,
            "budget_usd": 10,
        },
    )
    project = response.json()
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    client.post(f"/v1/projects/{project['id']}/plan")
    queued = client.post(f"/v1/projects/{project['id']}/run", json={"async": True, "approve_plan": True})
    assert queued.status_code == 200
    job_id = queued.json()["job_id"]
    assert client.get(f"/v1/jobs/{job_id}").status_code == 200
    assert client.post(f"/v1/jobs/{job_id}/cancel").status_code == 200
    assert queue.get(job_id).status == "cancelled"


def test_api_rejects_invalid_brief_and_normalizes_string_booleans():
    client = TestClient(app)
    response = client.post("/v1/projects", json={"title": "Missing request"})
    assert response.status_code == 400
    response = client.post("/v1/projects", json={"request": "", "duration_seconds": 0})
    assert response.status_code == 400

    project = client.post(
        "/v1/projects",
        json={
            "title": "Boolean fixture",
            "request": "Lin returns a letter in a city with narration and music.",
            "duration_seconds": 15,
            "shot_duration_seconds": 15,
            "max_shots": 1,
            "budget_usd": 10,
        },
    ).json()
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    client.post(f"/v1/projects/{project['id']}/plan")
    queued = client.post(f"/v1/projects/{project['id']}/run", json={"async": "true", "approve_plan": "false"})
    assert queued.status_code == 200
    assert queued.json()["queued"] is True
    assert client.post(f"/v1/jobs/{queued.json()['job_id']}/cancel").status_code == 200


def test_api_sse_honors_last_event_id():
    client = TestClient(app)
    project = client.post(
        "/v1/projects",
        json={
            "title": "SSE fixture",
            "request": "Lin returns a letter in a city with narration and music.",
            "duration_seconds": 15,
            "shot_duration_seconds": 15,
            "max_shots": 1,
            "budget_usd": 10,
        },
    ).json()
    events = client.get(f"/v1/projects/{project['id']}/events").json()
    assert events
    last_id = events[-1]["id"]
    response = client.get(f"/v1/projects/{project['id']}/events/stream", headers={"Last-Event-ID": str(last_id)})
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_api_can_cancel_project_and_reconcile_provider_callback():
    client = TestClient(app)
    response = client.post(
        "/v1/projects",
        json={"title": "Callback fixture", "request": "Lin returns a letter in a city with narration and music.", "duration_seconds": 15, "shot_duration_seconds": 15, "max_shots": 1, "budget_usd": 10},
    )
    project = response.json()
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    client.post(f"/v1/projects/{project['id']}/plan")
    cancelled = client.post(f"/v1/projects/{project['id']}/cancel", json={"reason": "test"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_api_exposes_plan_shot_budget_provider_and_pause_controls():
    client = TestClient(app)
    project = client.post(
        "/v1/projects",
        json={"title": "Control fixture", "request": "Lin returns a letter in a city with narration and music.", "duration_seconds": 15, "shot_duration_seconds": 15, "max_shots": 1, "budget_usd": 10},
    ).json()
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    planned = client.post(f"/v1/projects/{project['id']}/plan").json()
    version = planned["plans"][-1]["version"]
    listed = client.get(f"/v1/projects/{project['id']}/plans")
    assert listed.status_code == 200 and listed.json()[0]["version"] == version
    full_plan = client.get(f"/v1/projects/{project['id']}/plans/{version}")
    assert full_plan.status_code == 200
    shot_id = full_plan.json()["shots"][0]["id"]
    assert client.get(f"/v1/projects/{project['id']}/shots/{shot_id}").status_code == 200
    assert client.get(f"/v1/projects/{project['id']}/budget").json()["hard_stop"] is False
    assert client.get("/v1/providers").status_code == 200
    paused = client.post(f"/v1/projects/{project['id']}/pause", json={"reason": "operator review"})
    assert paused.status_code == 200 and paused.json()["status"] == "awaiting_human"
    rolled = client.post(f"/v1/projects/{project['id']}/plans/{version}/rollback")
    assert rolled.status_code == 200 and rolled.json()["plans"][-1]["status"] == "draft"


def test_api_returns_bad_request_for_malformed_command_payload():
    client = TestClient(app)
    project = client.post(
        "/v1/projects",
        json={"title": "Malformed fixture", "request": "Lin returns a letter in a city with narration and music.", "duration_seconds": 15, "shot_duration_seconds": 15, "max_shots": 1, "budget_usd": 10},
    ).json()
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    client.post(f"/v1/projects/{project['id']}/plan")
    response = client.post(f"/v1/projects/{project['id']}/approve-plan", json={"actor": []})
    assert response.status_code == 400


def test_api_async_single_shot_generation_uses_generate_command():
    client = TestClient(app)
    project = client.post(
        "/v1/projects",
        json={
            "title": "Single shot fixture",
            "request": "Lin returns a letter in a city with narration and music.",
            "duration_seconds": 15,
            "shot_duration_seconds": 15,
            "max_shots": 1,
            "budget_usd": 10,
        },
    ).json()
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    planned = client.post(f"/v1/projects/{project['id']}/plan").json()
    shot_id = planned["plans"][-1]["shots"][0]["id"]
    queued = client.post(
        f"/v1/projects/{project['id']}/shots/{shot_id}/generate",
        json={"async": True, "actor": "api-test"},
    )
    assert queued.status_code == 200
    job = queue.get(queued.json()["job_id"])
    assert job is not None and job.kind == "shot.generate"
    queue.cancel(job.id)


def test_api_returns_conflict_contract_for_stale_snapshot(monkeypatch):
    client = TestClient(app)
    project = client.post(
        "/v1/projects",
        json={
            "title": "Conflict fixture",
            "request": "Lin returns a letter in a city with narration and music.",
            "duration_seconds": 15,
            "shot_duration_seconds": 15,
            "max_shots": 1,
            "budget_usd": 10,
        },
    ).json()

    def raise_conflict(*_args, **_kwargs):
        raise SnapshotConflict(project["id"], expected_revision=1, actual_revision=2)

    monkeypatch.setattr(
        "video_director.api.orchestrator.answer_clarifications",
        raise_conflict,
    )
    answers = {turn["id"]: "Approved" for turn in project["clarification_turns"]}
    response = client.post(f"/v1/projects/{project['id']}/clarifications", json=answers)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "snapshot_conflict"
    assert body["project_id"] == project["id"]
    assert body["expected_revision"] == 1
    assert body["actual_revision"] == 2
    assert body["retryable"] is True


def test_api_project_lineage_rewind_settings_and_custom_provider(monkeypatch, tmp_path):
    replacement = build_mock_orchestrator(store_path=tmp_path / "workflow.db", fail_first_attempts=0)
    monkeypatch.setattr("video_director.api.orchestrator", replacement)
    monkeypatch.setattr("video_director.api.projects", {})
    client = TestClient(app)
    brief = {
        "title": "Workflow fixture",
        "request": "Lin returns a letter in a city at dawn.",
        "duration_seconds": 15,
        "shot_duration_seconds": 15,
        "max_shots": 1,
        "budget_usd": 20,
    }

    source = client.post("/v1/projects", json=brief).json()
    answers = {turn["id"]: "Approved" for turn in source["clarification_turns"]}
    client.post(f"/v1/projects/{source['id']}/clarifications", json=answers)
    client.post(f"/v1/projects/{source['id']}/plan")
    running = client.post(f"/v1/projects/{source['id']}/run", json={"approve_plan": True})
    assert running.status_code == 200
    delivered = client.post(f"/v1/projects/{source['id']}/deliver")
    assert delivered.status_code == 200
    delivered_project = delivered.json()

    listed = client.get("/v1/projects")
    assert listed.status_code == 200
    assert listed.json()[0]["root_project_id"] == source["id"]

    version = client.post(f"/v1/projects/{source['id']}/versions", json={"actor": "api-test"})
    assert version.status_code == 201
    version_project = version.json()
    assert version_project["version"] == 2
    assert version_project["parent_project_id"] == source["id"]
    assert version_project["plans"][0]["id"] != delivered_project["plans"][0]["id"]
    assert version_project["plans"][0]["shots"][0]["id"] != delivered_project["plans"][0]["shots"][0]["id"]

    rewound = client.post(
        f"/v1/projects/{source['id']}/rewind",
        json={"target_phase": "plan", "expected_revision": delivered_project["revision"], "reason": "revise"},
    )
    assert rewound.status_code == 200
    assert rewound.json()["status"] == "awaiting_plan_approval"
    assert rewound.json()["artifacts"][-1]["metadata"]["active"] is False

    basic = client.get("/v1/settings/basic")
    advanced = client.get("/v1/settings/advanced")
    assert basic.status_code == 200 and "VIDEO_PROVIDER" in basic.json()["settings"]
    assert advanced.status_code == 200 and "DIRECTOR_DATABASE_URL" in advanced.json()["settings"]
    assert client.patch("/v1/settings/basic", json={"DIRECTOR_DEFAULT_DURATION_SECONDS": 45}).status_code == 200
    assert client.patch("/v1/settings/advanced", json={"DIRECTOR_MAX_ATTEMPTS": 4}).status_code == 200

    provider = client.post(
        "/v1/providers/custom",
        json={
            "id": "workflow-provider",
            "name": "Workflow Provider",
            "capability": "video",
            "submit_url": "https://provider.test/submit",
            "api_key": "super-secret",
            "body_template": {"prompt": "{{prompt}}"},
        },
    )
    assert provider.status_code == 201
    assert provider.json()["api_key"] == "********"
    assert client.get("/v1/providers/custom").json()[0]["api_key"] == "********"
    updated = client.patch("/v1/providers/custom/workflow-provider", json={"api_key": "********", "model": "v2"})
    assert updated.status_code == 200 and updated.json()["api_key"] == "********"
    assert client.patch("/v1/settings/basic", json={"VIDEO_PROVIDER": "custom:workflow-provider"}).status_code == 200
    assert replacement.video_provider.name == "workflow-provider"
    assert isinstance(replacement.assembler, FFmpegAssembler)
    assert client.patch("/v1/settings/basic", json={"VIDEO_PROVIDER": "mock"}).status_code == 200
    assert replacement.video_provider.name == "mock-video"
    assert replacement.assembler.name == "mock-assembler"
    assert client.delete("/v1/providers/custom/workflow-provider").json()["deleted"] is True

    replacement.store.close()


def test_api_delivery_stream_supports_range_and_download(monkeypatch, tmp_path):
    replacement = build_mock_orchestrator(store_path=tmp_path / "media-api.db", fail_first_attempts=0)
    monkeypatch.setattr("video_director.api.orchestrator", replacement)
    monkeypatch.setattr("video_director.api.projects", {})
    monkeypatch.setenv("DIRECTOR_MEDIA_ROOT", str(tmp_path / "media"))
    client = TestClient(app)

    project = client.post(
        "/v1/projects",
        json={"title": "Media fixture", "request": "A short delivery.", "duration_seconds": 3, "shot_duration_seconds": 3, "max_shots": 1, "budget_usd": 10},
    ).json()
    stored = replacement.store.load_project(project["id"])
    assert stored is not None
    source = tmp_path / "source.mp4"
    source.write_bytes(b"0123456789")
    stored.artifacts.append(
        ArtifactRef(
            AssetKind.VIDEO,
            str(source),
            mime_type="video/mp4",
            metadata={"artifact_role": "delivery", "active": True},
        )
    )
    stored.transition(ProjectStatus.AWAITING_HUMAN, force=True)
    replacement.store.save_project(stored, event_type="fixture.media")
    artifact_id = stored.artifacts[-1].id

    ranged = client.get(f"/v1/projects/{stored.id}/artifacts/{artifact_id}/stream", headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206
    assert ranged.content == b"2345"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    downloaded = client.get(f"/v1/projects/{stored.id}/artifacts/{artifact_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == b"0123456789"
    assert "attachment" in downloaded.headers["content-disposition"]
    replacement.store.close()
