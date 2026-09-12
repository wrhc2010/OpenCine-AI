"""Command line entry points for local demos and worker bootstrap."""
from __future__ import annotations

import argparse
import json
import os

from .execution import DirectorOrchestrator
from .artifacts import FFmpegAssembler, LocalArtifactStore
from .providers.http import (
    AgnesVideoProvider,
    ComfyUIProvider,
    FalLikeAsyncVideoProvider,
    OpenAICompatibleLLMProvider,
    OpenAICompatibleVLMJudgeProvider,
    RotatingJSONHTTPClient,
    TemplateHTTPVideoProvider,
)
from .providers.mock import (
    MockAssembler,
    MockAudioProvider,
    MockJudgeProvider,
    MockVideoProvider,
)
from .queue import DirectorWorker, make_job_queue
from .scheduler import ProjectJobHandler
from .schemas import CreativeBrief, as_jsonable
from .store import make_event_store


def _parallelism_from_env() -> int:
    """Read and validate the bounded worker parallelism setting."""
    raw = os.getenv("DIRECTOR_PARALLELISM", "1").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("DIRECTOR_PARALLELISM must be a positive integer") from error
    if value < 1:
        raise ValueError("DIRECTOR_PARALLELISM must be a positive integer")
    return value


def build_mock_orchestrator(
    *,
    store_path: str = ":memory:",
    fail_first_attempts: int = 1,
    parallelism: int | None = None,
) -> DirectorOrchestrator:
    return DirectorOrchestrator(
        video_provider=MockVideoProvider(fail_first_attempts=fail_first_attempts),
        judge_provider=MockJudgeProvider(),
        assembler=MockAssembler(),
        audio_provider=MockAudioProvider(),
        store=make_event_store(store_path),
        parallelism=_parallelism_from_env() if parallelism is None else parallelism,
    )


def _configured(store, key: str, *environment_names: str, default: str | None = None) -> str | None:
    value = store.get_setting(key)
    if value is not None and str(value).strip():
        return str(value).strip()
    for name in environment_names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _build_llm(store):
    provider_name = (_configured(store, "LLM_PROVIDER", "LLM_PROVIDER", default="openai-compatible-llm") or "").lower()
    keys = _provider_keys(store)
    api_key = keys[0] if keys else None
    if not api_key or provider_name in {"mock", "none", "disabled"}:
        return None
    return OpenAICompatibleLLMProvider(
        _configured(store, "OPENAI_BASE_URL", "OPENAI_BASE_URL", "DIRECTOR_LLM_BASE_URL", "AGNES_BASE_URL", default="https://api.openai.com/v1") or "https://api.openai.com/v1",
        api_key=api_key,
        client=RotatingJSONHTTPClient(keys, interval_seconds=float(os.getenv("DIRECTOR_PROVIDER_INTERVAL_SECONDS", "60"))) if keys else None,
        model=_configured(store, "DIRECTOR_LLM_MODEL", "DIRECTOR_LLM_MODEL", default="gpt-4o-mini") or "gpt-4o-mini",
    )


def _build_vlm(store):
    provider_name = (_configured(store, "VLM_PROVIDER", "VLM_PROVIDER", default="openai-compatible-vlm-judge") or "").lower()
    keys = _provider_keys(store)
    api_key = keys[0] if keys else None
    if not api_key or provider_name in {"mock", "none", "disabled"}:
        return None
    return OpenAICompatibleVLMJudgeProvider(
        _configured(store, "OPENAI_BASE_URL", "OPENAI_BASE_URL", "DIRECTOR_VLM_BASE_URL", "AGNES_BASE_URL", default="https://api.openai.com/v1") or "https://api.openai.com/v1",
        api_key=api_key,
        client=RotatingJSONHTTPClient(keys, interval_seconds=float(os.getenv("DIRECTOR_PROVIDER_INTERVAL_SECONDS", "60"))) if keys else None,
        model=_configured(store, "DIRECTOR_VLM_MODEL", "DIRECTOR_VLM_MODEL", default="gpt-4o-mini") or "gpt-4o-mini",
    )


def _provider_keys(store) -> list[str]:
    primary = _configured(store, "AGNES_API_KEY", "AGNES_API_KEY", "OPENAI_API_KEY") or _configured(store, "OPENAI_API_KEY", "OPENAI_API_KEY")
    backup = _configured(store, "AGNES_BACKUP_API_KEY", "AGNES_BACKUP_API_KEY", "OPENAI_BACKUP_API_KEY")
    return [key for key in (primary, backup) if key]


def build_orchestrator(
    *,
    store_path: str | None = None,
    fail_first_attempts: int = 0,
    parallelism: int | None = None,
) -> DirectorOrchestrator:
    """Build the configured adapter set without leaking vendor details."""
    store = make_event_store(store_path)
    configured_custom = store.get_setting("CUSTOM_PROVIDERS")
    try:
        custom_providers = json.loads(configured_custom) if configured_custom else []
    except (TypeError, ValueError, json.JSONDecodeError):
        custom_providers = []
    provider_name = (_configured(store, "VIDEO_PROVIDER", "VIDEO_PROVIDER", default="mock") or "mock").strip().lower()
    if provider_name == "mock":
        video_provider = MockVideoProvider(
            fail_first_attempts=fail_first_attempts,
            cost_usd=float(os.getenv("MOCK_VIDEO_COST_USD", "1.25")),
        )
    elif provider_name in {"agnes", "agnes-video", "agnes-video-v2.0"}:
        keys = _provider_keys(store)
        if not keys:
            raise ValueError("AGNES_API_KEY or OPENAI_API_KEY is required for Agnes Video")
        video_provider = AgnesVideoProvider(
            api_keys=keys,
            client=RotatingJSONHTTPClient(
                keys,
                timeout_seconds=120,
                interval_seconds=float(os.getenv("DIRECTOR_PROVIDER_INTERVAL_SECONDS", "60")),
                cooldown_seconds=900,
            ),
            model=_configured(store, "VIDEO_MODEL", "VIDEO_MODEL", default="agnes-video-v2.0") or "agnes-video-v2.0",
        )
    elif provider_name in {"fal", "fal-like", "replicate"}:
        base_url = os.getenv("FAL_BASE_URL") or os.getenv("REPLICATE_BASE_URL")
        if not base_url:
            raise ValueError("FAL_BASE_URL or REPLICATE_BASE_URL is required for the cloud provider")
        video_provider = FalLikeAsyncVideoProvider(
            base_url,
            api_key=os.getenv("FAL_API_KEY") or os.getenv("REPLICATE_API_TOKEN"),
            model=os.getenv("VIDEO_MODEL", "video-model"),
            cost_per_second_usd=float(os.getenv("VIDEO_COST_PER_SECOND_USD", "0.25")),
        )
    elif provider_name == "comfyui":
        video_provider = ComfyUIProvider(os.getenv("COMFYUI_BASE_URL", "http://localhost:8188"))
    elif provider_name.startswith("custom:") or any(item.get("id", "").lower() == provider_name for item in custom_providers if isinstance(item, dict)):
        custom_id = provider_name.removeprefix("custom:")
        config = next((item for item in custom_providers if isinstance(item, dict) and item.get("id", "").lower() == custom_id), None)
        if config is None:
            raise ValueError(f"Unknown custom VIDEO_PROVIDER: {custom_id}")
        video_provider = TemplateHTTPVideoProvider(config)
    else:
        raise ValueError(f"Unsupported VIDEO_PROVIDER: {provider_name}")
    llm_provider = _build_llm(store)
    vlm_provider = _build_vlm(store)
    assembler = MockAssembler() if provider_name == "mock" else FFmpegAssembler(artifact_store=LocalArtifactStore(os.getenv("DIRECTOR_MEDIA_ROOT", ".data/media")))
    return DirectorOrchestrator(
        video_provider=video_provider,
        judge_provider=vlm_provider or MockJudgeProvider(),
        assembler=assembler,
        audio_provider=MockAudioProvider(),
        llm_provider=llm_provider,
        store=store,
        max_attempts=int(os.getenv("DIRECTOR_MAX_ATTEMPTS", "3")),
        parallelism=_parallelism_from_env() if parallelism is None else parallelism,
    )


def run_demo(shots: int = 3, *, json_output: bool = False) -> dict:
    brief = CreativeBrief(
        request="A cinematic story about a protagonist named Lin in a rain-lit city: Lin finds a lost letter, follows its clues, and returns it at dawn. Include Mandarin narration, music and readable subtitles.",
        title="The Last Letter",
        duration_seconds=shots * 15.0,
        max_shots=shots,
        style="cinematic blue-hour realism",
        language="zh-CN",
        budget_usd=max(75.0, shots * 5.0),
    )
    orchestrator = build_mock_orchestrator(fail_first_attempts=1)
    project = orchestrator.create_project(brief)
    if project.clarification_turns:
        project = orchestrator.answer_clarifications(project, {turn.id: "Approved by the creator; keep this consistent across the film." for turn in project.clarification_turns})
    project = orchestrator.run(project, approve_plan=True, actor="demo")
    project = orchestrator.deliver(project, actor="demo")
    output = {
        "project_id": project.id,
        "status": project.status.value,
        "shot_count": len(project.active_plan.shots if project.active_plan else []),
        "attempt_count": len(project.attempts),
        "total_cost_usd": project.total_cost_usd,
        "delivery": as_jsonable(project.artifacts[-1]) if project.artifacts else None,
        "events": [as_jsonable(event) for event in orchestrator.store.events(project.id)],
    }
    if json_output:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"Delivered {output['project_id']} with {output['shot_count']} shots, {output['attempt_count']} attempts, ${output['total_cost_usd']:.2f}")
        print(f"Artifact: {output['delivery']['uri']}")
    orchestrator.store.close()
    return output


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="video-director")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="run the deterministic end-to-end production loop")
    demo.add_argument("--shots", type=int, default=3)
    demo.add_argument("--json", action="store_true", dest="json_output")
    worker = subparsers.add_parser("worker", help="start the production worker bootstrap")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--max-jobs", type=int, default=None)
    worker.add_argument("--idle-cycles", type=int, default=3)
    worker.add_argument("--forever", action="store_true", help="keep polling until the process is stopped")
    args = parser.parse_args(argv)
    if args.command == "demo":
        run_demo(args.shots, json_output=args.json_output)
    elif args.command == "worker":
        configured_store = os.getenv("DIRECTOR_DATABASE_URL", "sqlite:///.data/director.db")
        orchestrator = build_orchestrator(store_path=configured_store, fail_first_attempts=0)
        queue = make_job_queue(os.getenv("DIRECTOR_QUEUE_URL") or (None if os.getenv("REDIS_URL") else ".data/api-queue.db"))
        handler = ProjectJobHandler(orchestrator)

        def refresh_worker_runtime() -> None:
            nonlocal orchestrator
            previous = orchestrator
            orchestrator = build_orchestrator(store_path=configured_store, fail_first_attempts=0)
            handler.orchestrator = orchestrator
            previous.store.close()

        handler.before_execute = refresh_worker_runtime
        worker = DirectorWorker(queue, handler, lease_seconds=float(os.getenv("DIRECTOR_WORKER_LEASE_SECONDS", "120")))
        idle_cycles = None if args.forever else (1 if args.once else args.idle_cycles)
        processed = worker.run(max_jobs=1 if args.once else args.max_jobs, idle_cycles=idle_cycles)
        print(f"video-director worker processed {processed} job(s)")


if __name__ == "__main__":
    main()
