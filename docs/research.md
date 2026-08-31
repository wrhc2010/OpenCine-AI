# Research and Reuse Boundary

This document records the repository survey that led to the current architecture.
The goal is to avoid copying a research demo into a long-lived production core.

## Decision

AI Video Director uses a new Apache-2.0 core plus selective composition. We directly
reuse implementation ideas and (where needed) compatible MIT/Apache-2.0 code only
after checking its license and dependency boundary. Repositories without a clear
root license are design references, not code dependencies. GPL/AGPL code is not
included in the distribution.

## Capability matrix

| Project | License / code reality | Useful boundary | Decision |
| --- | --- | --- | --- |
| `sjtuplayer/showvi` | MIT; Plan -> Generate -> Evaluate -> Rewrite, WorkUnit/Attempt/checkpoint and workers; planning criteria and cross-shot checks are weak | attempt history, checkpoint and media utilities | borrow design; rebuild quality policy |
| `HKUDS/ViMax` | MIT; runtime tools, streamed events, cancellation, context compression, session/artifact index | agent runtime/session/event patterns | do not fork fixed pipeline |
| `univa-agent/univa` | MIT; model factory, MCP tools, web export and timeline; brittle JSON parsing and hard-coded paths | provider factory and UI/export ideas | adapt interfaces, not execution core |
| `open-video-ai/open-video` | Apache-2.0; ModelBackend/EngineAdapter, capabilities, request/result contracts, ComfyUI and ffmpeg stitching | provider capability negotiation and assembly | strongest contract reference |
| `neopen/story-shot-agent` / PenShot | MIT; LangGraph, SQLite checkpoint, memory, parser, auditor, continuity and repair | graph/checkpoint and human gates | substantial adaptation required |
| `wanshuiyin/ARIS-Movie-Director` | MIT; source-of-truth bibles, fail-closed evidence, bounded retry, deterministic diff and escalation; image/comic pipeline | quality governance and audit trail | reuse policy concepts only |
| `video-db/Director` | MIT; generic reasoning engine, tool calling, DB/WebSocket UI; video path is sequential | session/UI abstractions | not a video base |
| `HKUDS/VideoAgent` | MIT; function registry and multi-agent search/edit QA; synchronous execution | tool schema/registry | reference only |
| `diffusionstudio/agent` | MIT; browser editor and visual feedback focused on editing alignment | visual feedback/editor affordances | optional UI inspiration |
| `approximatelylinear/story2video` | no root license; FastHTML/SSE, typed schema, SQLite, chapter/versioning and provider sketches | data model and versioning concepts | no code copy |
| `showlab/MovieAgent` | no root license; research multi-agent planner with hard-coded paths; README exceeds `run.py` | role/scene/shot planning | README is not an implementation contract |
| `JianhuiWei7/VideoWeaver` | no license; skills/evaluation/evolution benchmark, trace and resume | evaluation and trace ideas | no code copy |
| `Vchitect/Evaluation-Agent` | no license; VBench/T2I-CompBench evaluation with evidence explanations | evidence-grounded evaluation | no code copy |

No surveyed project completes the full loop of structured clarification,
criterion-level acceptance, diagnosis, repair policy, continuity, cost controls,
long-task recovery and final assembly. Stars and README claims therefore do not
override license, tests, provider boundaries or recovery behavior.

## 16-capability assessment

| Capability | Highest observed state | AI Video Director treatment |
| --- | --- | --- |
| Requirement understanding / clarification | partial | structured clarification turns and required facts |
| Agent planning | partial | versioned, approvable and rollback-friendly plans |
| Scene / Shot decomposition | partial | unified Creative IR schema |
| Prompt generation | partial | prompt bundle versioned with criteria/references |
| Video generation | implemented locally by adapters | stable async provider protocol |
| VLM/LLM acceptance | demo/partial | criterion-level PASS/FAIL, evidence required |
| Failure diagnosis | demo | failure codes, causes and repair suggestions |
| Automatic repair | partial | bounded RepairAction policy |
| Regeneration | partial | attempt history and provider/model switching boundary |
| Cross-shot continuity | demo | bibles, signatures, evidence and escalation |
| Long-video assembly | basic implemented | ordered artifacts plus audio/subtitle assembly contract |
| Human-in-the-loop | partial | clarification, budget, retry and delivery gates |
| Provider abstraction | local implementations exist | Protocol contracts with capability negotiation |
| Web UI | demo/partial | agent state, evidence, queue and approval control room |
| Persistence / resume | partial in ecosystem | event log, snapshots, idempotency and leases |
| Parallelism / cost | demo/partial | queue workers, leases, budgets and mock load tests |

## Reuse policy

Every imported third-party file must retain its SPDX identifier, copyright and
source notice. Prefer a small adapter over vendoring a framework. Add a license
review entry before merging a new provider or media utility.
