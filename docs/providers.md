# Provider Development

Providers are adapters, not domain objects. Implement the Protocol in
`src/video_director/providers/base.py` and keep SDK response shapes inside the
adapter. The orchestrator only consumes `ProviderCapabilities`, `ProviderJob`,
`PollResult`, `ArtifactRef`, `CostRecord` and `ProviderError`.

## Video provider checklist

1. Expose `capabilities()` with models, duration, aspect ratio, audio and async
   support.
2. Implement `estimate_cost`, `submit`, `poll`, `cancel`, `fetch_artifacts` and
   `normalize_error`.
3. Pass the context idempotency key to the vendor when supported.
4. Normalize vendor statuses to `queued`, `running`, `succeeded`, `failed` or
   `cancelled`.
5. Preserve external request ID, model version, provider and output hashes in
   metadata.
6. Treat malformed output and missing URLs as failures; do not return a fake
   success artifact.

Use the HTTP adapter as the Fal/Replicate-style reference and ComfyUI as the
local queue reference. Add a fake HTTP client contract test for submit, polling,
error normalization, cancellation and duplicate/idempotent submission before
merging a provider.

## OpenAI-compatible LLM

OpenAICompatibleLLMProvider is an SDK-free adapter for chat servers exposing
POST /v1/chat/completions. Pass a base URL, optional bearer token, model,
temperature and max_tokens; the adapter normalizes the endpoint whether the
base URL already ends in /v1. complete() returns only message text, while an
optional JSON Schema is sent as a strict response format. Malformed responses
raise an explicit error at the boundary instead of leaking vendor response
objects into the domain layer.

Example bootstrap (values should come from secret management):

    from video_director.providers import OpenAICompatibleLLMProvider

    planner_llm = OpenAICompatibleLLMProvider(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ.get("OPENAI_API_KEY"),
        model=os.getenv("DIRECTOR_LLM_MODEL", "gpt-4o-mini"),
    )

The current deterministic PlanAgent remains the default bootstrap. Injecting
an LLM provider into a planner/clarifier is an explicit application decision;
the core contracts do not require a particular model or SDK.

## OpenAI-compatible VLM Judge

OpenAICompatibleVLMJudgeProvider uses the same chat endpoint and emits a
provider-neutral JudgeResult. It sends the Shot description, every planned
criterion (category, severity, threshold and evidence types), project context,
and artifact references as multimodal content. Image artifacts are sent as
image_url blocks; video/audio URIs are included as typed text references so a
deployment can replace this with a server-specific media block when supported.

The requested JSON shape contains shot_id, top-level verdict, and exactly one
result per criterion. Each result includes criterion_id, PASS/FAIL, evidence
objects, confidence, failure code/reason and repair suggestions. The adapter
preserves malformed or missing entries as visible failures where possible;
QualityController still enforces the final fail-closed policy for unknown
criteria, missing evidence, low confidence and judge exceptions.

The default API/CLI uses MockJudgeProvider so local runs are free. A live VLM
requires reachable, authorized media URLs and an integration test for the
chosen server's JSON-schema and multimodal behavior.

## Async video adapters in practice

FalLikeAsyncVideoProvider models the common submit/poll/webhook shape used by
Fal-like and Replicate-like services. It normalizes status, cost, model version,
external request ID, output URLs and retryable errors; it does not pretend to
know a vendor's exact queue paths or payload fields. Subclass it (or write a
thin sibling adapter) when the service differs, and keep those fields inside
the adapter.

ComfyUIProvider submits a workflow payload and maps history outputs to content
URLs. It assumes the workflow server is reachable and that output filenames can
be served through /view; authentication, workflow templates, GPU scheduling
and artifact upload remain deployment-specific.

Both adapters satisfy the same contract tests, but neither has been live-tested
in this checkout with paid credentials or a running ComfyUI instance.

## Thread safety and pooling

The parallel scheduler may call a Provider instance concurrently. The reference
Mock adapters protect mutable counters with locks, but the Protocol does not
guarantee thread safety. HTTP adapters are safest with a thread-safe client or
one client per worker thread; stateful GPU adapters should use a bounded pool or
route jobs through the provider's own queue. Document the choice in the
provider manifest and include concurrent submit/poll tests before enabling
DIRECTOR_PARALLELISM greater than 1.

## Judge and audio providers

Judges must return criterion IDs that exist in the Shot and evidence for each
criterion. Audio providers receive timed `AudioRequest` objects and return
`ArtifactRef` values tagged with cue kind, start time and duration. Lip-sync is
optional in MVP and must never be a hidden requirement of assembly.
