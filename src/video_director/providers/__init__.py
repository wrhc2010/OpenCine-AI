"""Provider protocols and reference implementations."""

from .base import (
    AssemblerProvider,
    AudioProvider,
    AudioRequest,
    JudgeInput,
    LipSyncProvider,
    LLMProvider,
    MusicProvider,
    ProviderCapabilities,
    ProviderContext,
    ReferenceProvider,
    SfxProvider,
    SpeechProvider,
    VideoGeneratorProvider,
    VideoRequest,
    VLMJudgeProvider,
)
from .http import (
    ComfyUIProvider,
    FalLikeAsyncVideoProvider,
    OpenAICompatibleLLMProvider,
    OpenAICompatibleVLMJudgeProvider,
)

__all__ = [
    "AssemblerProvider",
    "AudioProvider",
    "AudioRequest",
    "ComfyUIProvider",
    "FalLikeAsyncVideoProvider",
    "JudgeInput",
    "LLMProvider",
    "LipSyncProvider",
    "MusicProvider",
    "OpenAICompatibleLLMProvider",
    "OpenAICompatibleVLMJudgeProvider",
    "ProviderCapabilities",
    "ProviderContext",
    "ReferenceProvider",
    "SfxProvider",
    "SpeechProvider",
    "VLMJudgeProvider",
    "VideoGeneratorProvider",
    "VideoRequest",
]
