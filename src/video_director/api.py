"""HTTP API for the operator console.

FastAPI is an optional dependency.  Importing the core package never requires
the API stack, while installing `.[api]` exposes a ready-to-run app.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from fastapi import FastAPI, Header, HTTPException, Request, Response
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as error:  # pragma: no cover - exercised when API extras are absent
    raise RuntimeError("FastAPI is optional; install ai-video-director[api] to run the API") from error

from . import __version__
from .cli import build_mock_orchestrator, build_orchestrator
from .execution import BudgetExceeded, HumanGate
from .providers.http import (
    ComfyUIProvider,
    FalLikeAsyncVideoProvider,
    JSONHTTPClient,
    TemplateHTTPVideoProvider,
)
from .providers.mock import MockVideoProvider
from .queue import make_job_queue
from .scheduler import ProjectJobHandler, queue_project_run
from .schemas import CreativeBrief, Project, ProjectStatus, as_jsonable, stable_hash
from .store import SnapshotConflict


def _brief(payload: dict[str, Any]) -> CreativeBrief:
    if not isinstance(payload, dict):
        raise TypeError("creative brief must be a JSON object")
    allowed = {field for field in CreativeBrief.__dataclass_fields__}
    values = {key: value for key, value in payload.items() if key in allowed}
    defaults = {
        "duration_seconds": 150.0,
        "shot_duration_seconds": 15.0,
        "max_shots": 10,
        "budget_usd": 75.0,
        "acceptance_mode": "standard",
    }
    for key, fallback in defaults.items():
        if key not in values:
            setting_key = {
                "duration_seconds": "DIRECTOR_DEFAULT_DURATION_SECONDS",
                "shot_duration_seconds": "DIRECTOR_DEFAULT_SHOT_DURATION_SECONDS",
                "max_shots": "DIRECTOR_DEFAULT_MAX_SHOTS",
                "budget_usd": "DIRECTOR_PROJECT_BUDGET_USD",
                "acceptance_mode": "DIRECTOR_DEFAULT_ACCEPTANCE_MODE",
            }[key]
            raw = orchestrator.store.get_setting(setting_key)
            if raw is None:
                raw = os.getenv(setting_key)
            if raw is not None:
                try:
                    values[key] = float(raw) if key in {"duration_seconds", "shot_duration_seconds", "budget_usd"} else int(raw) if key == "max_shots" else str(raw)
                except (TypeError, ValueError):
                    values[key] = fallback
            else:
                values[key] = fallback
    try:
        return CreativeBrief(**values)
    except TypeError as error:
        raise ValueError(f"Invalid creative brief: {error}") from error


app = FastAPI(title="AI Video Director", version=__version__)
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


def _custom_providers() -> list[dict[str, Any]]:
    raw = orchestrator.store.get_setting(CUSTOM_PROVIDER_SETTING_KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _masked_provider(provider: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(provider)
    for key in ("api_key", "token", "secret"):
        if result.get(key):
            result[key] = "********"
    return result


def _masked_custom_providers() -> list[dict[str, Any]]:
    return [_masked_provider(item) for item in _custom_providers() if isinstance(item, Mapping)]


def _save_custom_providers(value: list[dict[str, Any]]) -> None:
    orchestrator.store.set_setting(CUSTOM_PROVIDER_SETTING_KEY, json.dumps(value, ensure_ascii=False))
    _reload_custom_video_provider()


def _reload_custom_video_provider() -> None:
    selected = str(orchestrator.store.get_setting("VIDEO_PROVIDER") or os.getenv("VIDEO_PROVIDER", "mock")).strip()
    normalized = selected.casefold()
    provider = None
    if normalized.startswith("custom:") or any(
        str(item.get("id", "")).casefold() == normalized
        for item in _custom_providers()
        if isinstance(item, Mapping)
    ):
        provider_id = normalized.removeprefix("custom:")
        config = next(
            (
                item
                for item in _custom_providers()
                if isinstance(item, Mapping)
                and str(item.get("id", "")).casefold() == provider_id
                and item.get("capability", "video") == "video"
            ),
            None,
        )
        if config is None:
            raise ValueError(f"Unknown custom VIDEO_PROVIDER: {provider_id}")
        provider = TemplateHTTPVideoProvider(config)
    elif normalized in {"mock", "mock-video"}:
        provider = orchestrator.video_providers.get("mock-video") or MockVideoProvider(
            fail_first_attempts=0,
            cost_usd=float(os.getenv("MOCK_VIDEO_COST_USD", "1.25")),
        )
    elif normalized in {"fal", "fal-like", "replicate"}:
        provider = next(
            (candidate for name, candidate in orchestrator.video_providers.items() if name.casefold() in {"fal", "fal-like", "fal-like-video"}),
            None,
        )
        if provider is None:
            base_url = os.getenv("FAL_BASE_URL") or os.getenv("REPLICATE_BASE_URL")
            if not base_url:
                raise ValueError("FAL_BASE_URL or REPLICATE_BASE_URL is required for the cloud provider")
            provider = FalLikeAsyncVideoProvider(
                base_url,
                api_key=orchestrator.store.get_setting("FAL_API_KEY") or os.getenv("FAL_API_KEY") or os.getenv("REPLICATE_API_TOKEN"),
                model=orchestrator.store.get_setting("VIDEO_MODEL") or os.getenv("VIDEO_MODEL", "video-model"),
                cost_per_second_usd=float(os.getenv("VIDEO_COST_PER_SECOND_USD", "0.25")),
            )
    elif normalized == "comfyui":
        provider = orchestrator.video_providers.get("comfyui") or ComfyUIProvider(os.getenv("COMFYUI_BASE_URL", "http://localhost:8188"))
    else:
        provider = next((candidate for name, candidate in orchestrator.video_providers.items() if name.casefold() == normalized), None)
    if provider is None:
        raise ValueError(f"Unsupported VIDEO_PROVIDER: {selected}")
    orchestrator.video_provider = provider
    orchestrator.video_providers[provider.name] = provider


def _validate_custom_provider(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload.get("id"), str) or not payload["id"].strip():
        raise HTTPException(400, "Provider id 不能为空")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise HTTPException(400, "Provider 名称不能为空")
    capability = payload.get("capability", "video")
    if capability not in {"video", "llm", "vlm", "audio", "reference"}:
        raise HTTPException(400, "不支持的 Provider 能力类型")
    endpoint = payload.get("submit_url") or payload.get("base_url")
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        raise HTTPException(400, "Provider URL 必须使用 http:// 或 https://")
    result = dict(payload)
    result["id"] = payload["id"].strip()
    result["name"] = payload["name"].strip()
    result["capability"] = capability
    result.setdefault("method", "POST")
    result.setdefault("headers", {})
    result.setdefault("body_template", {})
    result.setdefault("poll", {})
    result.setdefault("result", {})
    return result


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
    "DIRECTOR_DEFAULT_DURATION_SECONDS": 150.0,
    "DIRECTOR_DEFAULT_SHOT_DURATION_SECONDS": 15.0,
    "DIRECTOR_DEFAULT_MAX_SHOTS": 10,
    "DIRECTOR_DEFAULT_ACCEPTANCE_MODE": "standard",
    "LLM_PROVIDER": "openai-compatible-llm",
    "VLM_PROVIDER": "openai-compatible-vlm-judge",
}
SETTING_KEYS = set(SETTING_DEFAULTS) | {"OPENAI_API_KEY", "FAL_API_KEY", "REPLICATE_API_TOKEN"}
SECRET_SETTING_KEYS = {"OPENAI_API_KEY", "FAL_API_KEY", "REPLICATE_API_TOKEN"}
CONNECTION_SETTING_KEYS = {"DIRECTOR_DATABASE_URL", "REDIS_URL", "OBJECT_STORAGE_ENDPOINT"}
SETTING_KEYS |= CONNECTION_SETTING_KEYS
CUSTOM_PROVIDER_SETTING_KEY = "CUSTOM_PROVIDERS"
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


BASIC_SETTING_KEYS = {
    "VIDEO_PROVIDER", "VIDEO_MODEL", "DIRECTOR_LLM_MODEL", "DIRECTOR_VLM_MODEL",
    "LLM_PROVIDER", "VLM_PROVIDER", "DIRECTOR_PROJECT_BUDGET_USD", "DIRECTOR_PARALLELISM",
    "DIRECTOR_DEFAULT_DURATION_SECONDS", "DIRECTOR_DEFAULT_SHOT_DURATION_SECONDS",
    "DIRECTOR_DEFAULT_MAX_SHOTS", "DIRECTOR_DEFAULT_ACCEPTANCE_MODE",
    *SECRET_SETTING_KEYS,
}
ADVANCED_SETTING_KEYS = SETTING_KEYS - BASIC_SETTING_KEYS


def _settings_subset(keys: set[str]) -> dict[str, Any]:
    full = get_settings()
    return {key: full["settings"].get(key, "") for key in sorted(keys) if key in full["settings"]}


@app.get("/v1/settings/basic")
def get_basic_settings() -> dict[str, Any]:
    return {"settings": _settings_subset(BASIC_SETTING_KEYS), "providers": _masked_custom_providers()}


@app.patch("/v1/settings/basic")
def update_basic_settings(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - BASIC_SETTING_KEYS)
    if unknown:
        raise HTTPException(400, f"不支持的普通设置：{', '.join(unknown)}")
    return update_settings(payload)


@app.get("/v1/settings/advanced")
def get_advanced_settings() -> dict[str, Any]:
    return {"settings": _settings_subset(ADVANCED_SETTING_KEYS), "requires_restart": sorted(RESTART_SETTING_KEYS)}


@app.patch("/v1/settings/advanced")
def update_advanced_settings(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - ADVANCED_SETTING_KEYS)
    if unknown:
        raise HTTPException(400, f"不支持的高级设置：{', '.join(unknown)}")
    return update_settings(payload)


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
    _reload_custom_video_provider()
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


@app.get("/v1/projects")
def list_projects() -> list[dict[str, Any]]:
    return [
        {
            "id": project.id,
            "name": project.name,
            "status": project.status.value,
            "version": project.version,
            "root_project_id": project.root_project_id or project.id,
            "parent_project_id": project.parent_project_id,
            "updated_at": project.updated_at.isoformat(),
            "total_cost_usd": project.total_cost_usd,
        }
        for project in orchestrator.store.list_projects()
    ]


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


@app.post("/v1/projects/{project_id}/versions", status_code=201)
def create_project_version(project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    source = _get(project_id)
    try:
        return project_view(orchestrator.create_project_version(source, actor=str((payload or {}).get("actor", "human"))))
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error


@app.post("/v1/projects/{project_id}/rewind")
def rewind_project(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = _get(project_id)
    target = payload.get("target_phase") if isinstance(payload, dict) else None
    if not isinstance(target, str):
        raise HTTPException(400, "target_phase is required")
    expected = payload.get("expected_revision") if isinstance(payload, dict) else None
    if expected is not None and expected != project.revision:
        raise SnapshotConflict(project.id, int(expected), project.revision)
    try:
        return project_view(orchestrator.rewind(project, target, actor=str(payload.get("actor", "human")), reason=str(payload.get("reason", "operator rewind"))))
    except HumanGate as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


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


@app.get("/v1/providers/custom")
def custom_providers() -> list[dict[str, Any]]:
    return _masked_custom_providers()


@app.post("/v1/providers/custom", status_code=201)
def create_custom_provider(payload: dict[str, Any]) -> dict[str, Any]:
    provider = _validate_custom_provider(payload)
    providers_value = _custom_providers()
    if any(item.get("id") == provider["id"] for item in providers_value):
        raise HTTPException(409, "Provider id 已存在")
    providers_value.append(provider)
    _save_custom_providers(providers_value)
    return _masked_provider(provider)


@app.patch("/v1/providers/custom/{provider_id}")
def update_custom_provider(provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    providers_value = _custom_providers()
    current = next((item for item in providers_value if item.get("id") == provider_id), None)
    if current is None:
        raise HTTPException(404, "custom provider not found")
    merged = {**current, **payload, "id": provider_id}
    if payload.get("api_key") in {"********", "已配置"}:
        merged["api_key"] = current.get("api_key", "")
    provider = _validate_custom_provider(merged)
    providers_value[providers_value.index(current)] = provider
    _save_custom_providers(providers_value)
    return _masked_provider(provider)


@app.delete("/v1/providers/custom/{provider_id}")
def delete_custom_provider(provider_id: str) -> dict[str, Any]:
    providers_value = _custom_providers()
    filtered = [item for item in providers_value if item.get("id") != provider_id]
    if len(filtered) == len(providers_value):
        raise HTTPException(404, "custom provider not found")
    _save_custom_providers(filtered)
    return {"id": provider_id, "deleted": True}


@app.post("/v1/providers/custom/{provider_id}/test")
def test_custom_provider(provider_id: str) -> dict[str, Any]:
    provider = next((item for item in _custom_providers() if item.get("id") == provider_id), None)
    if provider is None:
        raise HTTPException(404, "custom provider not found")
    url = provider.get("test_url") or provider.get("submit_url") or provider.get("base_url")
    try:
        response = JSONHTTPClient(timeout_seconds=10).request(str(provider.get("method", "GET")), str(url), headers=provider.get("headers") or {}, payload=provider.get("test_body") or None)
        return {"ok": True, "provider_id": provider_id, "response": response if isinstance(response, (dict, list, str, int, float, bool)) else str(response)}
    except Exception as error:  # noqa: BLE001 - surface a safe connection test result
        return {"ok": False, "provider_id": provider_id, "error": str(error)}


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
