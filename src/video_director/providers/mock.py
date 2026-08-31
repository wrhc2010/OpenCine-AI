"""Deterministic providers used by tests, local demos and replay fixtures."""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import replace

from ..schemas import (
    ArtifactRef,
    AssetKind,
    CostRecord,
    CriterionResult,
    Evidence,
    JudgeResult,
    ProviderError,
    ProviderJob,
    Verdict,
)
from .base import (
    AssemblerProvider,
    AudioProvider,
    AudioRequest,
    JudgeInput,
    LipSyncProvider,
    PollResult,
    ProviderCapabilities,
    ProviderContext,
    ReferenceProvider,
    VideoGeneratorProvider,
    VideoRequest,
    VLMJudgeProvider,
)


class MockVideoProvider(VideoGeneratorProvider):
    name = "mock-video"

    def __init__(self, *, fail_first_attempts: int = 0, cost_usd: float = 1.25) -> None:
        self.fail_first_attempts = fail_first_attempts
        self.cost_usd = cost_usd
        self._jobs: dict[str, tuple[VideoRequest, ProviderJob, int]] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            models=["mock-v1"],
            supports_async=True,
            supports_cancel=True,
            supported_aspect_ratios=["16:9", "9:16", "1:1"],
            supported_audio=True,
        )

    def estimate_cost(self, request: VideoRequest) -> CostRecord:
        return CostRecord(self.cost_usd, provider=self.name, model=request.model or "mock-v1", estimated=True)

    def submit(self, request: VideoRequest, context: ProviderContext) -> ProviderJob:
        with self._lock:
            key = context.idempotency_key or f"{context.project_id}:{context.shot_id}:{request.shot.id}"
            for _, job, _ in self._jobs.values():
                if job.idempotency_key == key:
                    return job
            self._counter += 1
            job = ProviderJob(self.name, f"mock_job_{self._counter}", idempotency_key=key)
            self._jobs[job.external_id] = (request, job, 0)
            return job

    def poll(self, job: ProviderJob) -> PollResult:
        with self._lock:
            request, current, polls = self._jobs[job.external_id]
            polls += 1
            status = "running" if polls == 1 else "succeeded"
            current = replace(current, status=status)
            self._jobs[job.external_id] = (request, current, polls)
            if status != "succeeded":
                return PollResult(current)
            attempt = int(request.parameters.get("attempt", 1))
            continuity = dict(request.parameters.get("_continuity_signatures") or {})
            if attempt <= self.fail_first_attempts:
                artifact = ArtifactRef(
                    AssetKind.VIDEO,
                    f"mock://video/{request.shot.id}/attempt-{attempt}-bad.mp4",
                    duration_seconds=request.shot.duration_seconds,
                    metadata={"quality": "bad", "attempt": attempt, **continuity},
                )
            else:
                artifact = ArtifactRef(
                    AssetKind.VIDEO,
                    f"mock://video/{request.shot.id}/attempt-{attempt}.mp4",
                    duration_seconds=request.shot.duration_seconds,
                    metadata={"quality": "good", "attempt": attempt, **continuity},
                )
            return PollResult(current, [artifact], CostRecord(self.cost_usd, provider=self.name, model=request.model or "mock-v1"))

    def cancel(self, job: ProviderJob) -> None:
        with self._lock:
            if job.external_id in self._jobs:
                request, current, polls = self._jobs[job.external_id]
                self._jobs[job.external_id] = (request, replace(current, status="cancelled"), polls)

    def fetch_artifacts(self, job: ProviderJob) -> list[ArtifactRef]:
        with self._lock:
            result = self.poll(job)
            return result.artifacts

    def normalize_error(self, error: Exception) -> ProviderError:
        return ProviderError("mock_error", str(error), retryable=True, provider=self.name)


class MockJudgeProvider(VLMJudgeProvider):
    name = "mock-judge"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider=self.name, models=["mock-v1"], supports_async=False)

    def judge(self, input: JudgeInput) -> JudgeResult:
        artifact = input.artifacts[0] if input.artifacts else None
        bad = not artifact or artifact.metadata.get("quality") == "bad"
        results: list[CriterionResult] = []
        for criterion in input.shot.acceptance_criteria:
            evidence = [] if artifact is None else [self._evidence_for(artifact.uri, criterion, input.shot.duration_seconds)]
            if bad:
                results.append(CriterionResult(
                    criterion.id,
                    Verdict.FAIL,
                    evidence,
                    failure_code="mock_quality_failure",
                    reason="Mock artifact intentionally fails the first attempt",
                    confidence=0.99,
                    repair_suggestions=[],
                ))
            else:
                results.append(CriterionResult(criterion.id, Verdict.PASS, evidence, confidence=0.99))
        verdict = Verdict.FAIL if any(r.verdict == Verdict.FAIL for r in results) else Verdict.PASS
        return JudgeResult(input.shot.id, verdict, results, self.name, "mock-v1", summary="deterministic mock evaluation")

    @staticmethod
    def _evidence_for(uri: str, criterion, duration_seconds: float) -> Evidence:
        """Emit deterministic evidence in a modality accepted by the criterion."""
        evidence_types = criterion.evidence_types if isinstance(criterion.evidence_types, list) else []
        kind = next((value.strip().lower() for value in evidence_types if isinstance(value, str) and value.strip()), "frame")
        timestamp = duration_seconds / 2
        return Evidence(kind, f"{uri}#evidence={kind}", timestamp_seconds=timestamp, metadata={"criterion_id": criterion.id})


class MockAudioProvider(AudioProvider):
    name = "mock-audio"

    def synthesize(self, request: AudioRequest, context: ProviderContext) -> ArtifactRef:
        digest = hashlib.sha256(request.text.encode()).hexdigest()[:12]
        kind = request.kind.lower()
        asset_kind = AssetKind.SUBTITLE if kind == "subtitle" else AssetKind.AUDIO
        extension = "srt" if asset_kind == AssetKind.SUBTITLE else "wav"
        return ArtifactRef(asset_kind, f"mock://audio/{digest}.{extension}", duration_seconds=request.duration_seconds, mime_type="text/plain" if asset_kind == AssetKind.SUBTITLE else "audio/wav", metadata={"kind": kind, "text": request.text, "start_seconds": context.metadata.get("start_seconds", 0.0), **request.parameters})


class MockReferenceProvider(ReferenceProvider):
    name = "mock-reference"

    def resolve(self, uri: str, *, project_id: str) -> ArtifactRef:
        return ArtifactRef(AssetKind.IMAGE, uri, metadata={"project_id": project_id})


class MockLipSyncProvider(LipSyncProvider):
    name = "mock-lipsync"

    def sync(self, video: ArtifactRef, audio: ArtifactRef, *, context) -> ArtifactRef:
        return ArtifactRef(
            AssetKind.VIDEO,
            f"mock://lipsync/{video.id}-{audio.id}.mp4",
            duration_seconds=video.duration_seconds,
            metadata={"source_video": video.uri, "source_audio": audio.uri, "project_id": context.project_id},
        )


class MockAssembler(AssemblerProvider):
    name = "mock-assembler"

    def assemble(self, video_artifacts: Sequence[ArtifactRef], audio_artifacts: Sequence[ArtifactRef] = (), *, output_uri: str | None = None, metadata: dict | None = None) -> ArtifactRef:
        if not video_artifacts:
            raise ValueError("at least one video artifact is required")
        duration = sum(a.duration_seconds or 0 for a in video_artifacts)
        uri = output_uri or "mock://delivery/final.mp4"
        audio_kinds = {str(item.metadata.get("kind", "audio")) for item in audio_artifacts}
        required_kinds = {"narration", "music", "sfx", "subtitle"}
        return ArtifactRef(AssetKind.VIDEO, uri, duration_seconds=duration, metadata={"video_count": len(video_artifacts), "audio_count": len(audio_artifacts), "audio_kinds": sorted(audio_kinds), "audio_complete": required_kinds.issubset(audio_kinds), **(metadata or {})})
