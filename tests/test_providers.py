from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from video_director.artifacts import LocalArtifactStore
from video_director.providers.base import (
    AudioRequest,
    JudgeInput,
    ProviderContext,
    VideoRequest,
)
from video_director.providers.http import (
    ComfyUIProvider,
    FalLikeAsyncVideoProvider,
    OpenAICompatibleLLMProvider,
    OpenAICompatibleVLMJudgeProvider,
    ProviderHTTPError,
)
from video_director.schemas import (
    AcceptanceCriterion,
    ArtifactRef,
    AssetKind,
    CriterionCategory,
    Shot,
    Verdict,
)


@dataclass
class FakeHTTPClient:
    responses: list[object]

    def __post_init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url, dict(payload or {})))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def request() -> VideoRequest:
    shot = Shot(1, "scene", "shot", "A person walks through the city")
    return VideoRequest(shot, "cinematic person walking", negative_prompt="blur", parameters={"fps": 24}, reference_uris=["mock://ref"])


def test_openai_compatible_llm_posts_schema_and_extracts_text():
    client = FakeHTTPClient([{"choices": [{"message": {"content": "{\"ok\":true}"}}]}])
    provider = OpenAICompatibleLLMProvider("https://llm.test/v1", api_key="secret", model="planner", client=client, max_tokens=128)
    result = provider.complete("make a plan", schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    assert result == "{\"ok\":true}"
    method, url, payload = client.calls[0]
    assert method == "POST" and url == "https://llm.test/v1/chat/completions"
    assert payload["model"] == "planner" and payload["max_tokens"] == 128
    assert payload["response_format"]["type"] == "json_schema"
    assert client.calls[0][2]["messages"][0]["content"] == "make a plan"


def test_openai_compatible_llm_rejects_malformed_response():
    provider = OpenAICompatibleLLMProvider("https://llm.test", client=FakeHTTPClient([{"choices": []}]))
    with pytest.raises(ValueError, match="missing choices"):
        provider.complete("hello")


def test_openai_compatible_vlm_converts_structured_criteria_and_multimodal_input():
    shot = Shot(1, "scene", "shot", "A person walks", acceptance_criteria=[AcceptanceCriterion("subject visible", CriterionCategory.SUBJECT)])
    response = {
        "choices": [{"message": {"content": __import__("json").dumps({
            "shot_id": shot.id,
            "verdict": "PASS",
            "criterion_results": [{
                "criterion_id": shot.acceptance_criteria[0].id,
                "verdict": "PASS",
                "evidence": [{"kind": "frame", "uri": "https://cdn/frame.jpg", "timestamp_seconds": 1.0}],
                "confidence": 0.93,
            }],
        })}}]
    }
    client = FakeHTTPClient([response])
    provider = OpenAICompatibleVLMJudgeProvider("https://vlm.test/v1", api_key="secret", model="judge", client=client)
    result = provider.judge(JudgeInput(shot, [ArtifactRef(AssetKind.VIDEO, "https://cdn/video.mp4")], {"project_id": "p1"}))
    assert result.shot_id == shot.id and result.verdict == Verdict.PASS
    assert result.criterion_results[0].evidence[0].uri.endswith("frame.jpg")
    payload = client.calls[0][2]
    user_message = next(message for message in payload["messages"] if message["role"] == "user")
    multimodal = user_message["content"]
    assert any(block["type"] == "text" and "Artifact (video)" in block["text"] for block in multimodal)
    assert client.calls[0][0] == "POST"


def test_openai_compatible_vlm_invalid_json_is_explicit_error():
    shot = Shot(1, "scene", "shot", "A person walks", acceptance_criteria=[AcceptanceCriterion("subject visible", CriterionCategory.SUBJECT)])
    provider = OpenAICompatibleVLMJudgeProvider("https://vlm.test", client=FakeHTTPClient([{"choices": [{"message": {"content": "not-json"}}]}]))
    with pytest.raises(ValueError, match="invalid JSON"):
        provider.judge(JudgeInput(shot, []))


def test_fal_like_submit_poll_is_idempotent_at_contract_boundary():
    client = FakeHTTPClient([
        {"request_id": "req-1", "status": "queued"},
        {"status": "running"},
        {"status": "completed", "output": {"video_url": "https://cdn/video.mp4"}, "cost_usd": 1.5},
    ])
    provider = FalLikeAsyncVideoProvider("https://provider.test", client=client)
    context = ProviderContext("project", "shot", "idem-1")
    job = provider.submit(request(), context)
    assert job.external_id == "req-1"
    first = provider.poll(job)
    second = provider.poll(first.job)
    assert first.job.status == "running"
    assert second.job.status == "succeeded"
    assert second.artifacts[0].uri.endswith("video.mp4")
    assert second.cost and second.cost.amount_usd == 1.5
    assert client.calls[0][2]["idempotency_key"] == "idem-1"


def test_fal_like_preserves_zero_cost_and_rejects_malformed_success_output():
    client = FakeHTTPClient([
        {"request_id": "req-zero"},
        {"status": "succeeded", "output": {"video_url": "https://cdn/video.mp4"}, "cost_usd": 0},
    ])
    provider = FalLikeAsyncVideoProvider("https://provider.test", client=client)
    job = provider.submit(request(), ProviderContext("project", "shot", "idem-zero"))
    result = provider.poll(job)
    assert result.cost is not None and result.cost.amount_usd == 0.0

    malformed = FalLikeAsyncVideoProvider("https://provider.test", client=FakeHTTPClient([{"request_id": "req-bad"}, {"status": "succeeded", "output": {"video_url": 7}}]))
    bad_job = malformed.submit(request(), ProviderContext("project", "shot", "idem-bad"))
    bad_result = malformed.poll(bad_job)
    assert bad_result.job.status == "failed" and bad_result.error is not None


def test_fal_like_normalizes_http_and_network_errors():
    provider = FalLikeAsyncVideoProvider("https://provider.test")
    retryable = provider.normalize_error(ProviderHTTPError(503, "down"))
    permanent = provider.normalize_error(ProviderHTTPError(400, "bad request"))
    assert retryable.code == "http_503" and retryable.retryable
    assert permanent.code == "http_400" and not permanent.retryable


def test_comfyui_history_maps_outputs_to_artifacts():
    client = FakeHTTPClient([
        {"prompt_id": "prompt-1"},
        {"prompt-1": {"outputs": {"9": {"gifs": [{"filename": "clip.mp4", "subfolder": "out", "type": "output"}]}}}},
    ])
    provider = ComfyUIProvider("http://comfy.test", client=client)
    job = provider.submit(request(), ProviderContext("project", "shot", "idem"))
    result = provider.poll(job)
    assert result.job.status == "succeeded"
    assert result.artifacts[0].uri == "http://comfy.test/view?filename=clip.mp4&subfolder=out&type=output"
    assert result.cost and result.cost.amount_usd == 0.0


def test_local_artifact_store_round_trip_and_path_escape(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(b"hello", kind=AssetKind.VIDEO, mime_type="video/mp4")
    assert store.get_bytes(artifact) == b"hello"
    try:
        store.get_bytes(tmp_path / "outside.bin")
    except ValueError as error:
        assert "local store" in str(error)
    else:
        raise AssertionError("path escape must be rejected")


def test_fal_like_rejects_unknown_status_and_empty_submit_id():
    provider = FalLikeAsyncVideoProvider("https://provider.test", client=FakeHTTPClient([{"request_id": "req-1", "status": "mystery"}]))
    with pytest.raises(ValueError, match="unsupported job status"):
        provider.submit(request(), ProviderContext("project", "shot", "idem-status"))

    provider = FalLikeAsyncVideoProvider("https://provider.test", client=FakeHTTPClient([{"request_id": ""}]))
    with pytest.raises(ValueError, match="non-empty request_id"):
        provider.submit(request(), ProviderContext("project", "shot", "idem-empty"))


def test_fal_like_poll_fails_closed_for_unknown_status_output_and_cost():
    cases = [
        ({"status": "mystery"}, "mystery"),
        ({"status": "succeeded", "output": 7}, "output"),
        ({"status": "succeeded", "output": {"video_url": "https://cdn/video.mp4"}, "cost_usd": -1}, "cost"),
        ({"status": "succeeded", "output": {"video_url": "https://cdn/video.mp4"}, "cost_usd": math.nan}, "cost"),
        ({"status": "succeeded", "output": ["https://cdn/video.mp4", 7]}, "artifact"),
    ]
    for response, marker in cases:
        client = FakeHTTPClient([{"request_id": "req-contract"}, response])
        provider = FalLikeAsyncVideoProvider("https://provider.test", client=client)
        job = provider.submit(request(), ProviderContext("project", "shot", "idem-contract"))
        result = provider.poll(job)
        assert result.job.status == "failed"
        assert result.error is not None
        assert marker in result.error.message


def test_fal_like_rejects_submit_and_poll_model_mismatch():
    provider = FalLikeAsyncVideoProvider("https://provider.test", model="model-a", client=FakeHTTPClient([{"request_id": "req-model", "model": "model-b"}]))
    with pytest.raises(ValueError, match="different model"):
        provider.submit(request(), ProviderContext("project", "shot", "idem-model"))

    client = FakeHTTPClient([
        {"request_id": "req-model-poll", "model": "model-a"},
        {"status": "succeeded", "model": "model-b", "output": {"video_url": "https://cdn/video.mp4"}, "cost_usd": 1},
    ])
    provider = FalLikeAsyncVideoProvider("https://provider.test", model="model-a", client=client)
    job = provider.submit(request(), ProviderContext("project", "shot", "idem-model-poll"))
    result = provider.poll(job)
    assert result.job.status == "failed"
    assert result.error is not None and "different model" in result.error.message


@pytest.mark.parametrize(
    ("history", "marker"),
    [
        ({"prompt-1": []}, "history entry"),
        ({"prompt-1": {"outputs": []}}, "outputs"),
        ({"prompt-1": {"outputs": {"9": []}}}, "output node"),
        ({"prompt-1": {"outputs": {"9": {"videos": {}}}}}, "must be an array"),
        ({"prompt-1": {"outputs": {"9": {"videos": [{}]}}}}, "missing filename"),
        ({"prompt-1": {"outputs": {"9": {"videos": [{"filename": "clip.mp4", "subfolder": 7}]}}}}, "must be strings"),
    ],
)
def test_comfyui_poll_rejects_malformed_history(history, marker):
    client = FakeHTTPClient([{"prompt_id": "prompt-1"}, history])
    provider = ComfyUIProvider("http://comfy.test", client=client)
    job = provider.submit(request(), ProviderContext("project", "shot", "idem-comfy-bad"))
    result = provider.poll(job)
    assert result.job.status == "failed"
    assert result.error is not None and marker in result.error.message


def test_audio_provider_contract_returns_typed_artifacts():
    from video_director.providers.mock import MockAudioProvider

    provider = MockAudioProvider()
    context = ProviderContext("project", metadata={"start_seconds": 2.5})
    speech = provider.synthesize(AudioRequest("hello", kind="dialogue", duration_seconds=2), context)
    music = provider.synthesize(AudioRequest("underscore", kind="music", duration_seconds=4), context)
    sfx = provider.synthesize(AudioRequest("rain", kind="sfx", duration_seconds=1), context)
    assert speech.kind == AssetKind.AUDIO and music.kind == AssetKind.AUDIO and sfx.kind == AssetKind.AUDIO
    assert speech.metadata["start_seconds"] == 2.5
