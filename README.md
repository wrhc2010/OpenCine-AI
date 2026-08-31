# AI Video Director

An open-source, provider-agnostic **AI Video Director**. Give it a creative brief and it turns the brief into a versioned production plan, generates shots, evaluates every acceptance criterion with evidence, diagnoses failures, repairs prompts or generation strategy, retries within a budget, checks continuity, assembles audio/video, and pauses for a human when a decision is material.

The project is intentionally **Agent-first**: the timeline is an output, not the control surface. The domain state, event log, provenance and quality gates belong to this repository; model providers are adapters behind stable protocols.

## Current status

This repository contains the Phase 0/1 foundation and a deterministic end-to-end reference loop:

- Creative IR for briefs, plans, scenes, shots, bibles, references and acceptance criteria
- Clarification detection and approval gates
- Provider protocols for LLM, VLM judge, video, reference, speech, music, SFX, lip-sync and assembly
- Fal/Replicate-style asynchronous HTTP adapter and ComfyUI adapter
- SQLite event-sourced development store with a Postgres-ready repository boundary
- Retry, budget, lease, idempotency and checkpoint primitives
- Monotonic project snapshot revisions with compare-and-swap conflict detection
- Durable SQLite queue plus Redis Streams consumer-group adapter with worker leases and recovery
- Fail-closed criterion-level judging, structured diagnosis and repair actions
- Cross-shot continuity checks and deterministic mock providers
- Optional FastAPI API and React/Vite operator console
- Docker Compose services for API, worker, PostgreSQL, Redis and MinIO

The mock provider is deliberately useful: it lets contributors run the entire Plan → Generate → Judge → Diagnose → Refine → Regenerate → Verify → Assemble loop without a paid model account.

## Quick start

### Core demo (Python only)

```bash
python -m venv .venv
.venv\Scripts\activate       # PowerShell on Windows
pip install -e ".[dev]"
python -m video_director.cli demo --shots 3
pytest
```

### API and web console

```bash
pip install -e ".[api]"
uvicorn video_director.api:app --reload
cd web
npm install
npm run dev
```

The API defaults to a local SQLite file (`.data/director.db`) and mock providers. Production deployments should set `DIRECTOR_DATABASE_URL`, `REDIS_URL`, `OBJECT_STORAGE_ENDPOINT`, and provider credentials.

### Compose

```bash
docker compose up --build
```

## Architecture

```text
Creative Brief
      │
Clarification Agent ── human gate ── Plan Agent ── human gate
      │                                      │
      └──────────── Creative IR / bibles / acceptance criteria
                                             │
             Scheduler ── Provider adapters ── Video/Audio artifacts
                 │                              │
                 └── Judge (criterion evidence) ── Diagnose/Repair
                                                      │
                                  continuity ── Assemble ── human delivery gate
```

The public contracts live in `src/video_director/providers/base.py` and the domain objects live in `src/video_director/schemas.py`. They do not expose LangGraph, FastAPI, SQLAlchemy or a model vendor. Frameworks can be replaced without rewriting the production state machine.

## Design principles

1. **Fail closed.** Missing evidence, malformed judge output, provider timeout or an unknown criterion is a failure that must be diagnosed or escalated.
2. **Every decision is provenance.** Prompt, parameters, references, provider/model version, request id, cost, evidence and repair action are append-only records.
3. **Bounded autonomy.** The Agent can choose a repair, but retry count, budget and human gates are hard policy.
4. **Local context, global memory.** A shot receives the relevant scene/bible context; the project keeps compact artifacts and event snapshots rather than replaying an unbounded transcript.
5. **Adapters over forks.** Only MIT/Apache-2.0 code is eligible for direct reuse. Research repositories without a clear license inform design but are not copied.

## Research decision

The implementation follows a **new Apache-2.0 core plus selective composition**. ViMax and showvi informed runtime/checkpoint patterns; open-video informed capability-aware engine contracts and stitching; PenShot and ARIS informed continuity, repair, fail-closed evaluation and human escalation. None of those projects owns the complete Judge → Diagnose → Refine loop required here, so a direct fork would create more migration debt than it removes. See [`docs/research.md`](docs/research.md) for the 16-capability matrix and license boundary. The implementation boundary is described in [`docs/architecture.md`](docs/architecture.md), and provider authors can start with [`docs/providers.md`](docs/providers.md).

## Roadmap

- Phase 0: contracts, persistence, compose and license/SBOM hygiene (implemented foundation)
- Phase 1: LLM-backed clarification and 10-shot planning
- Phase 2: cloud/ComfyUI workers, idempotent external jobs and cost accounting
- Phase 3: VLM evidence sampling, repair policy and continuity embeddings
- Phase 4: speech/music/SFX/subtitles, optional lip-sync and delivery UI
- Phase 5: 40-shot chaos/load tests, provider SDK documentation and contributor program

## Asynchronous execution

The API accepts `async: true` on a project run or Shot retry and returns a durable
`job_id`. Run `python -m video_director.cli worker --once` for one local queue
job, or use the Compose worker for Redis Streams. Query or cancel a job through
`GET /v1/jobs/{job_id}` and `POST /v1/jobs/{job_id}/cancel`.

## Controlled parallel execution

Set DIRECTOR_PARALLELISM to a positive integer to run independent Shots in
bounded dependency-ready waves. The default is 1, which is the deterministic
single-threaded mode used by the CLI demo and most development work. A value
greater than 1 is an experimental/controlled capability: each Shot runs from
an isolated project snapshot, completed attempts and topology changes are
merged incrementally, and the worker event trail is replayed into the main
EventStore. Dependencies are never skipped; a Shot waits until every
depends_on_shot_ids entry has passed. A repair that splits a Shot is added to a
later wave instead of being silently omitted.

Parallel workers share the configured Provider objects in the current reference
implementation. Use thread-safe clients or a Provider pool when enabling
parallelism in production. Start 40-Shot load and recovery tests with Mock or
Replay Providers so retries and callback recovery do not incur model charges.

## OpenAI-compatible LLM and VLM adapters

The SDK-free OpenAICompatibleLLMProvider and
OpenAICompatibleVLMJudgeProvider implement the provider protocols for servers
that expose /v1/chat/completions. They accept an optional API key, model,
temperature and token limit; structured calls use a strict JSON Schema response
format. The VLM adapter sends the Shot criteria and artifact references as
multimodal chat content and converts the response into criterion-level evidence.

These adapters are library components rather than an implicit vendor lock-in.
Construct them in a deployment-specific bootstrap and inject them into the
DirectorOrchestrator; the default CLI/API bootstrap intentionally keeps the
deterministic Mock Judge so a fresh checkout works without credentials. A
custom bootstrap can read OPENAI_BASE_URL, OPENAI_API_KEY, DIRECTOR_LLM_MODEL
and DIRECTOR_VLM_MODEL. Real model credentials, media URLs and provider policy
still need an integration test before production use.

## Known limitations

The repository is an extensible orchestration foundation and a deterministic
reference implementation, not a production long-film generator yet. In
particular:

- Fal/Replicate-compatible HTTP, ComfyUI, and OpenAI-compatible VLM adapters
  have contract tests, but have not been exercised here with live credentials
  and real media URLs.
- Continuity currently uses Creative IR metadata and signatures. Embedding or
  visual identity checks are extension points, not a claim of full visual
  consistency.
- Audio is provider-neutral/mock plumbing for dialogue, music, SFX, subtitles
  and caching. Live TTS, music, SFX and professional mixing are not bundled.
  Lip-sync remains optional and is never an assembly gate.
- FFmpegAssembler has complete behavior for local file paths; remote object
  storage requires a download/materialization layer in the deployment.
- PostgreSQL, Redis Streams, MinIO and the Compose topology are wired as
  adapters, but still need environment-specific end-to-end validation.
- The current parallel scheduler is bounded and recoverable, but Provider
  thread-safety and pooling are deployment responsibilities.
- The default automatic retry limit is three attempts per Shot, with budget
  soft warning/hard stop and human gates for material decisions.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
