from __future__ import annotations

from fastapi.testclient import TestClient

from video_director.api import app, queue
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
