"""HTTP API for the operator console.

FastAPI is an optional dependency.  Importing the core package never requires
the API stack, while installing `.[api]` exposes a ready-to-run app.
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
from typing import Any

try:
    from fastapi import FastAPI, Header, HTTPException, Request, Response
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as error:  # pragma: no cover - exercised when API extras are absent
    raise RuntimeError("FastAPI is optional; install ai-video-director[api] to run the API") from error

from .cli import build_mock_orchestrator, build_orchestrator
from .execution import BudgetExceeded, HumanGate
from .queue import make_job_queue
from .scheduler import ProjectJobHandler, queue_project_run
from .schemas import CreativeBrief, Project, ProjectStatus, as_jsonable, stable_hash
from .store import SnapshotConflict


def _brief(payload: dict[str, Any]) -> CreativeBrief:
    if not isinstance(payload, dict):
        raise TypeError("creative brief must be a JSON object")
    allowed = {field for field in CreativeBrief.__dataclass_fields__}
    try:
        return CreativeBrief(**{key: value for key, value in payload.items() if key in allowed})
    except TypeError as error:
        raise ValueError(f"Invalid creative brief: {error}") from error


app = FastAPI(title="AI Video Director", version="0.1.0")
store_path = os.getenv("DIRECTOR_DATABASE_URL", "sqlite:///.data/api.db")
try:
    orchestrator = build_orchestrator(store_path=store_path, fail_first_attempts=1)
except ValueError:
    # A missing cloud URL should not prevent the local API from booting.
    orchestrator = build_mock_orchestrator(store_path=store_path, fail_first_attempts=1)
queue = make_job_queue(os.getenv("DIRECTOR_QUEUE_URL") or (None if os.getenv("REDIS_URL") else ".data/api-queue.db"))
job_handler = ProjectJobHandler(orchestrator)
projects: dict[str, Project] = {}


def project_view(project: Project) -> dict[str, Any]:
    return as_jsonable(project)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _auth_enabled() -> bool:
    stored = orchestrator.store.get_setting("DIRECTOR_AUTH_ENABLED")
    return _bool_env("DIRECTOR_AUTH_ENABLED", True) if stored is None else _as_bool(stored)


SETTING_DEFAULTS: dict[str, Any] = {
    "DIRECTOR_AUTH_ENABLED": True,
    "DIRECTOR_HOST_CHECK_ENABLED": False,
    "DIRECTOR_CORS_ENABLED": False,
    "DIRECTOR_RATE_LIMIT_ENABLED": False,
    "DIRECTOR_CONTENT_SAFETY_ENABLED": False,
    "DIRECTOR_PROVIDER_SAFETY_ENABLED": False,
    "DIRECTOR_MAX_ATTEMPTS": 3,
    "DIRECTOR_PARALLELISM": 1,
    "DIRECTOR_PROJECT_BUDGET_USD": 75.0,
    "VIDEO_PROVIDER": "mock",
    "VIDEO_MODEL": "video-model",
    "DIRECTOR_LLM_MODEL": "gpt-4o-mini",
    "DIRECTOR_VLM_MODEL": "",
}
SETTING_KEYS = set(SETTING_DEFAULTS) | {"OPENAI_API_KEY", "FAL_API_KEY", "REPLICATE_API_TOKEN"}
SECRET_SETTING_KEYS = {"OPENAI_API_KEY", "FAL_API_KEY", "REPLICATE_API_TOKEN"}
CONNECTION_SETTING_KEYS = {"DIRECTOR_DATABASE_URL", "REDIS_URL", "OBJECT_STORAGE_ENDPOINT"}
SETTING_KEYS |= CONNECTION_SETTING_KEYS
RESTART_SETTING_KEYS = {
    "DIRECTOR_DATABASE_URL",
    "REDIS_URL",
    "OBJECT_STORAGE_ENDPOINT",
    "OPENAI_API_KEY",
    "FAL_API_KEY",
    "REPLICATE_API_TOKEN",
}


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def _password_matches(password: str, encoded: str) -> bool:
    try:
        _, salt_raw, digest_raw = encoded.split("$", 2)
        salt = base64.urlsafe_b64decode(salt_raw.encode())
        expected = base64.urlsafe_b64decode(digest_raw.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _validate_credentials(username: Any, password: Any) -> tuple[str, str]:
    if not isinstance(username, str) or not username.strip():
        raise HTTPException(400, "用户名不能为空")
    if not isinstance(password, str) or len(password) < 8:
        raise HTTPException(400, "密码至少需要 8 个字符")
    return username.strip(), password


@app.get("/v1/auth/status")
def auth_status() -> dict[str, Any]:
    user = orchestrator.store.auth_user()
    return {"enabled": _auth_enabled(), "initialized": user is not None, "username": user[0] if user else None}


@app.post("/v1/auth/setup")
def auth_setup(payload: dict[str, Any]) -> dict[str, Any]:
    if not _auth_enabled():
        return {"enabled": False, "initialized": True}
    if orchestrator.store.auth_user() is not None:
        raise HTTPException(409, "管理员账号已经初始化")
    username, password = _validate_credentials(payload.get("username"), payload.get("password"))
    orchestrator.store.create_auth_user(username, _password_hash(password))
    return {"enabled": True, "initialized": True, "username": username}


@app.post("/v1/auth/login")
def auth_login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    if not _auth_enabled():
        return {"enabled": False, "authenticated": True}
    username, password = _validate_credentials(payload.get("username"), payload.get("password"))
    user = orchestrator.store.auth_user()
    if user is None:
        raise HTTPException(409, "请先完成首次管理员初始化")
    if user[0] != username or not _password_matches(password, user[1]):
        raise HTTPException(401, "用户名或密码不正确")
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=14)
    orchestrator.store.create_auth_session(token, username, expires.isoformat())
    response.set_cookie("director_session", token, httponly=True, samesite="lax", max_age=14 * 24 * 3600, path="/")
    return {"enabled": True, "authenticated": True, "username": username}


@app.post("/v1/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, bool]:
    token = request.cookies.get("director_session")
    if token:
        orchestrator.store.revoke_auth_session(token)
    response.delete_cookie("director_session", path="/")
    return {"ok": True}


@app.get("/v1/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    if not _auth_enabled():
        return {"enabled": False, "authenticated": True, "username": None}
    token = request.cookies.get("director_session")
    username = orchestrator.store.auth_session_username(token) if token else None
    if not username:
        raise HTTPException(401, "未登录")
    return {"enabled": True, "authenticated": True, "username": username}


@app.get("/v1/settings")
def get_settings() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, default in SETTING_DEFAULTS.items():
        value = orchestrator.store.get_setting(key)
        if value is None:
            value = os.getenv(key, str(default))
        if isinstance(default, bool):
            result[key] = _as_bool(value)
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                result[key] = int(value)
            except (TypeError, ValueError):
                result[key] = default
        elif isinstance(default, float):
            try:
                result[key] = float(value)
            except (TypeError, ValueError):
                result[key] = default
        else:
            result[key] = str(value)
    for key in SECRET_SETTING_KEYS:
        value = orchestrator.store.get_setting(key) or os.getenv(key, "")
        result[key] = "已配置" if value else ""
    for key in ("DIRECTOR_DATABASE_URL", "REDIS_URL", "OBJECT_STORAGE_ENDPOINT"):
        result[key] = "已配置" if orchestrator.store.get_setting(key) or os.getenv(key) else ""
    return {
        "settings": result,
        "requires_restart": sorted(RESTART_SETTING_KEYS),
        "llm_configured": bool(orchestrator.store.get_setting("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")),
        "vlm_configured": bool(orchestrator.store.get_setting("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")),
    }


@app.patch("/v1/settings")
def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "设置必须是 JSON 对象")
    for key, value in payload.items():
        if key not in SETTING_KEYS:
            raise HTTPException(400, f"不支持的设置项：{key}")
        if isinstance(value, (dict, list)):
            raise HTTPException(400, f"设置项必须是简单值：{key}")
        if key in SECRET_SETTING_KEYS and str(value).strip() in {"已配置", "********", "••••••••"}:
            continue
        if isinstance(SETTING_DEFAULTS.get(key), bool):
            if isinstance(value, str) and value.strip().lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                raise HTTPException(400, f"设置项必须是布尔值：{key}")
            value = "true" if _as_bool(value) else "false"
        elif key in {"DIRECTOR_MAX_ATTEMPTS", "DIRECTOR_PARALLELISM"}:
            try:
                value = int(value)
            except (TypeError, ValueError) as error:
                raise HTTPException(400, f"设置项必须是正整数：{key}") from error
            if value < 1:
                raise HTTPException(400, f"设置项必须是正整数：{key}")
        elif key == "DIRECTOR_PROJECT_BUDGET_USD":
            try:
                value = float(value)
            except (TypeError, ValueError) as error:
                raise HTTPException(400, "项目预算必须是非负数字") from error
            if value < 0:
                raise HTTPException(400, "项目预算必须是非负数字")
        elif not isinstance(value, (str, int, float, bool)):
            raise HTTPException(400, f"设置项必须是简单值：{key}")
        orchestrator.store.set_setting(key, str(value))
    return get_settings()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "ai-video-director"}


@app.exception_handler(ValueError)
async def value_error_handler(_request, error: ValueError):
    """Keep malformed command payloads as stable client errors."""
    return JSONResponse(status_code=400, content={"detail": str(error)})


@app.exception_handler(SnapshotConflict)
async def snapshot_conflict_handler(_request, error: SnapshotConflict):
    """Expose optimistic-concurrency conflicts as actionable 409 responses."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(error),
            "error": "snapshot_conflict",
            "project_id": error.project_id,
            "expected_revision": error.expected_revision,
            "actual_revision": error.actual_revision,
            "retryable": True,
        },
    )


@app.post("/v1/projects", status_code=201)
def create_project(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        brief = _brief(payload)
        project = orchestrator.create_project(brief)
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    projects[project.id] = project
    return project_view(project)


@app.get("/v1/projects/{project_id}/settings")
def get_project_settings(project_id: str) -> dict[str, Any]:
    project = _get(project_id)
    return {"project_id": project.id, "settings": as_jsonable(project.brief), "resolved": project.active_plan.resolved_settings if project.active_plan else {}}


@app.patch("/v1/projects/{project_id}/settings")
def update_project_settings(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = _get(project_id)
    allowed = set(CreativeBrief.__dataclass_fields__)
    merged = {**as_jsonable(project.brief), **{key: value for key, value in payload.items() if key in allowed}}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise HTTPException(400, f"不支持的项目设置：{', '.join(unknown)}")
    try:
        brief = CreativeBrief(**merged)
    except TypeError as error:
        raise HTTPException(400, str(error)) from error
    errors = brief.validate()
    if errors:
        raise HTTPException(400, "; ".join(errors))
    project.brief = brief
    if project.status not in {ProjectStatus.CLARIFYING, ProjectStatus.CANCELLED, ProjectStatus.DELIVERED}:
        project.transition(ProjectStatus.AWAITING_PLAN_APPROVAL, force=True)
    orchestrator.store.save_project(project, event_type="project.settings.updated", payload={"keys": sorted(payload)})
    return get_project_settings(project_id)


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = orchestrator.store.load_project(project_id) or projects.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    projects[project.id] = project
    return project_view(project)


@app.post("/v1/projects/{project_id}/clarifications")
def answer_clarifications(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = _get(project_id)
    try:
        return project_view(orchestrator.answer_clarifications(project, payload))
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/plan")
def create_plan(project_id: str) -> dict[str, Any]:
    project = _get(project_id)
    try:
        return project_view(orchestrator.plan(project))
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.get("/v1/projects/{project_id}/plans")
def list_plans(project_id: str) -> list[dict[str, Any]]:
    project = _get(project_id)
    return [
        {
            "id": plan.id,
            "version": plan.version,
            "status": plan.status,
            "approved_by": plan.approved_by,
            "shot_count": len(plan.shots),
            "hash": stable_hash(plan),
        }
        for plan in project.plans
    ]


@app.get("/v1/projects/{project_id}/plans/{version}")
def get_plan(project_id: str, version: int) -> dict[str, Any]:
    project = _get(project_id)
    plan = next((candidate for candidate in project.plans if candidate.version == version), None)
    if plan is None:
        raise HTTPException(404, "plan version not found")
    return as_jsonable(plan)


@app.post("/v1/projects/{project_id}/plans/{version}/rollback")
def rollback_plan(project_id: str, version: int, payload: dict[str, str] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    try:
        return project_view(orchestrator.rollback_plan(project, version, actor=(payload or {}).get("actor", "human")))
    except KeyError as error:
        raise HTTPException(404, "plan version not found") from error
    except (HumanGate, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@app.get("/v1/projects/{project_id}/shots/{shot_id}")
def get_shot(project_id: str, shot_id: str) -> dict[str, Any]:
    project = _get(project_id)
    plan = project.active_plan
    shot = next((candidate for candidate in (plan.shots if plan else []) if candidate.id == shot_id), None)
    if shot is None:
        raise HTTPException(404, "shot not found")
    attempts = [attempt for attempt in project.attempts if attempt.shot_id == shot_id]
    return {"shot": as_jsonable(shot), "attempts": as_jsonable(attempts)}


@app.post("/v1/projects/{project_id}/pause")
def pause_project(project_id: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    body = payload or {}
    try:
        return project_view(orchestrator.pause(project, actor=body.get("actor", "human"), reason=body.get("reason", "paused by operator")))
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.get("/v1/projects/{project_id}/budget")
def project_budget(project_id: str) -> dict[str, Any]:
    project = _get(project_id)
    budget = project.brief.budget_usd
    spent = project.total_cost_usd
    return {"budget_usd": budget, "spent_usd": spent, "remaining_usd": max(0.0, budget - spent), "utilization": (spent / budget if budget else 1.0), "hard_stop": spent >= budget}


@app.get("/v1/providers")
def providers() -> list[dict[str, Any]]:
    unique = {}
    for provider in orchestrator.video_providers.values():
        name = getattr(provider, "name", provider.__class__.__name__)
        try:
            capabilities = as_jsonable(provider.capabilities())
        except Exception as error:  # noqa: BLE001 - report unavailable adapters without breaking the control plane
            capabilities = {"provider": name, "error": str(error)}
        unique[name] = capabilities
    return list(unique.values())


@app.post("/v1/projects/{project_id}/approve-plan")
def approve_plan(project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    actor = (payload or {}).get("actor", "human")
    if not isinstance(actor, str):
        raise HTTPException(400, "actor must be a string")
    try:
        return project_view(orchestrator.approve_plan(project, actor=actor))
    except (HumanGate, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/run")
def run_project(project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    body = payload or {}
    if _as_bool(body.get("async", False)):
        revision = f"{project.status.value}:{len(project.plans)}:{len(project.attempts)}:{len(project.artifacts)}"
        job = queue_project_run(queue, project.id, approve_plan=_as_bool(body.get("approve_plan", False)), actor=str(body.get("actor", "api")), revision=revision)
        orchestrator.store.append_event(project.id, "generation.queued", {"job_id": job.id, "idempotency_key": job.idempotency_key})
        return {"queued": True, "job_id": job.id, "project": project_view(project)}
    try:
        return project_view(orchestrator.run(project, approve_plan=_as_bool(body.get("approve_plan", False)), actor=str(body.get("actor", "human"))))
    except BudgetExceeded as error:
        raise HTTPException(402, str(error)) from error
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/shots/{shot_id}/retry")
def retry_shot(project_id: str, shot_id: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    body = payload or {}
    if project.active_plan is None or not any(shot.id == shot_id for shot in project.active_plan.shots):
        raise HTTPException(404, "shot not found")
    force = _as_bool(body.get("force", False))
    if _as_bool(body.get("async", False)):
        prior_attempts = sum(attempt.shot_id == shot_id for attempt in project.attempts)
        revision = f"{project.status.value}:{prior_attempts}:{len(project.artifacts)}"
        job = queue.enqueue(project.id, "shot.retry", {"shot_id": shot_id, "actor": body.get("actor", "api"), "force": force}, idempotency_key=f"{project.id}:shot.retry:{shot_id}:{int(force)}:{revision}")
        orchestrator.store.append_event(project.id, "shot.retry.queued", {"job_id": job.id, "shot_id": shot_id})
        return {"queued": True, "job_id": job.id, "project": project_view(project)}
    try:
        return project_view(orchestrator.retry_shot(project, shot_id, actor=body.get("actor", "human"), force=force))
    except KeyError as error:
        raise HTTPException(404, "shot not found") from error
    except (HumanGate, BudgetExceeded, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/shots/{shot_id}/generate")
def generate_shot(project_id: str, shot_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate one shot while retaining the project-level audit trail."""
    project = _get(project_id)
    body = payload or {}
    plan = project.active_plan
    shot = next((candidate for candidate in (plan.shots if plan else []) if candidate.id == shot_id), None)
    if shot is None:
        raise HTTPException(404, "shot not found")
    if _as_bool(body.get("async", False)):
        prior_attempts = sum(attempt.shot_id == shot_id for attempt in project.attempts)
        revision = f"{project.status.value}:{prior_attempts}:{len(project.artifacts)}"
        job = queue.enqueue(project.id, "shot.generate", {"shot_id": shot_id, "actor": body.get("actor", "api"), "force": _as_bool(body.get("force", False))}, idempotency_key=f"{project.id}:shot.generate:{shot_id}:{revision}")
        orchestrator.store.append_event(project.id, "shot.generation.queued", {"job_id": job.id, "shot_id": shot_id})
        return {"queued": True, "job_id": job.id, "project": project_view(project)}
    try:
        return project_view(orchestrator.generate_shot(project, shot_id, actor=str(body.get("actor", "api")), force=_as_bool(body.get("force", False))))
    except BudgetExceeded as error:
        raise HTTPException(402, str(error)) from error
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/deliver")
def deliver_project(project_id: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    try:
        return project_view(orchestrator.deliver(project, actor=(payload or {}).get("actor", "human")))
    except (HumanGate, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/cancel")
def cancel_project(project_id: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
    project = _get(project_id)
    body = payload or {}
    try:
        return project_view(orchestrator.cancel(project, actor=body.get("actor", "human"), reason=body.get("reason", "cancelled by operator")))
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/providers/{provider}/callback")
def provider_callback(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    metadata_project_id = metadata.get("project_id") if isinstance(metadata, Mapping) else None
    project_id = str(payload.get("project_id") or metadata_project_id or "")
    if not project_id:
        raise HTTPException(400, "provider callback requires project_id")
    project = _get(project_id)
    try:
        attempt = orchestrator.reconcile_provider_callback(project, provider, payload)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"project_id": project.id, "attempt_id": attempt.id, "status": attempt.status, "provider_job": as_jsonable(attempt.provider_job)}


@app.get("/v1/projects/{project_id}/events")
def events(project_id: str, after: int = 0) -> list[dict[str, Any]]:
    _get(project_id)
    return [as_jsonable(event) for event in orchestrator.store.events(project_id, after_id=after)]


@app.get("/v1/projects/{project_id}/events/stream")
def event_stream(
    project_id: str,
    after: int = 0,
    follow: bool = False,
    max_seconds: int = 120,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
):
    _get(project_id)
    max_seconds = max(1, min(max_seconds, 3600))

    def stream():
        # Browsers send Last-Event-ID when EventSource reconnects.  The query
        # parameter remains useful for clients that cannot set request headers.
        cursor = max(0, after, last_event_id or 0)
        started = time.monotonic()
        while True:
            emitted = False
            for event in orchestrator.store.events(project_id, after_id=cursor):
                cursor = event.id
                emitted = True
                yield f"id: {event.id}\ndata: {json.dumps(as_jsonable(event), ensure_ascii=False)}\n\n"
            if not follow or time.monotonic() - started >= max_seconds:
                break
            if not emitted:
                yield ": keep-alive\n\n"
            time.sleep(1.0)
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return as_jsonable(job)


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
    if not queue.cancel(job_id, reason=(payload or {}).get("reason", "cancelled by operator")):
        raise HTTPException(404, "job not found or already terminal")
    return {"job_id": job_id, "status": "cancelled"}


def _get(project_id: str) -> Project:
    # Workers update durable snapshots in another process; always prefer the
    # store so a cached object cannot mask completed asynchronous work.
    project = orchestrator.store.load_project(project_id) or projects.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    projects[project.id] = project
    return project


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
