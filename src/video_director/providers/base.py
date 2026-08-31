"""Stable provider contracts.

Protocols intentionally use the project's domain objects instead of SDK types.
An adapter may be synchronous internally, but it must expose a job that can be
polled, cancelled and recovered after a process restart.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schemas import (
    ArtifactRef,
    CostRecord,
    JudgeResult,
    ProviderError,
    ProviderJob,
    Shot,
)


@dataclass(slots=True)
class ProviderCapabilities:
    provider: str
    models: list[str] = field(default_factory=list)
    supports_async: bool = True
    supports_cancel: bool = False
    supports_webhook: bool = False
    max_duration_seconds: float | None = None
    supported_aspect_ratios: list[str] = field(default_factory=list)
    supported_audio: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderContext:
    project_id: str
    shot_id: str | None = None
    idempotency_key: str | None = None
    callback_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VideoRequest:
    shot: Shot
    prompt: str
    negative_prompt: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    reference_uris: list[str] = field(default_factory=list)
    model: str | None = None


@dataclass(slots=True)
class PollResult:
    job: ProviderJob
    artifacts: list[ArtifactRef] = field(default_factory=list)
    cost: CostRecord | None = None
    error: ProviderError | None = None


@dataclass(slots=True)
class JudgeInput:
    shot: Shot
    artifacts: Sequence[ArtifactRef]
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AudioRequest:
    text: str
    voice: str | None = None
    duration_seconds: float | None = None
    kind: str = "speech"
    parameters: dict[str, Any] = field(default_factory=dict)


class VideoGeneratorProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def estimate_cost(self, request: VideoRequest) -> CostRecord: ...

    def submit(self, request: VideoRequest, context: ProviderContext) -> ProviderJob: ...

    def poll(self, job: ProviderJob) -> PollResult: ...

    def cancel(self, job: ProviderJob) -> None: ...

    def fetch_artifacts(self, job: ProviderJob) -> list[ArtifactRef]: ...

    def normalize_error(self, error: Exception) -> ProviderError: ...


class VLMJudgeProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def judge(self, input: JudgeInput) -> JudgeResult: ...


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str, *, schema: Mapping[str, Any] | None = None) -> str: ...


class ReferenceProvider(Protocol):
    name: str

    def resolve(self, uri: str, *, project_id: str) -> ArtifactRef: ...


class AudioProvider(Protocol):
    name: str

    def synthesize(self, request: AudioRequest, context: ProviderContext) -> ArtifactRef: ...


class SpeechProvider(AudioProvider, Protocol):
    """Speech provider specialization for dialogue and narration cues."""


class MusicProvider(AudioProvider, Protocol):
    """Music provider specialization for score and underscore cues."""


class SfxProvider(AudioProvider, Protocol):
    """Sound-effects provider specialization for SFX cues."""


class AssemblerProvider(Protocol):
    name: str

    def assemble(
        self,
        video_artifacts: Sequence[ArtifactRef],
        audio_artifacts: Sequence[ArtifactRef] = (),
        *,
        output_uri: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRef: ...


class LipSyncProvider(Protocol):
    """Optional provider for aligning a rendered face to a speech track."""

    name: str

    def sync(
        self,
        video: ArtifactRef,
        audio: ArtifactRef,
        *,
        context: ProviderContext,
    ) -> ArtifactRef: ...
