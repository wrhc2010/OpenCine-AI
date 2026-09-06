"""HTTP adapters for cloud async APIs and ComfyUI.

Only the adapter knows vendor response shapes.  The orchestrator sees
ProviderJob/PollResult and can therefore recover a job after a process restart.
The implementation uses the standard library so the deterministic core stays
installable without an HTTP framework.
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar

from ..schemas import (
    ArtifactRef,
    AssetKind,
    CostRecord,
    CriterionResult,
    Evidence,
    JudgeResult,
    ProviderError,
    ProviderJob,
    RepairKind,
    Verdict,
    new_id,
)
from .base import (
    JudgeInput,
    LLMProvider,
    PollResult,
    ProviderCapabilities,
    ProviderContext,
    VideoGeneratorProvider,
    VideoRequest,
    VLMJudgeProvider,
)


class ProviderHTTPError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _json_path(value: Any, path: str | None) -> Any:
    """Small, deterministic JSONPath subset used by WebUI providers."""
    if not path:
        return None
    tokens = [token for token in str(path).lstrip("$").lstrip(".").replace("[", ".").replace("]", "").split(".") if token]
    current = value
    for token in tokens:
        if isinstance(current, Mapping):
            current = current.get(token)
        elif isinstance(current, (list, tuple)) and token.isdigit():
            index = int(token)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _render_template(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _render_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, context) for item in value]
    if isinstance(value, str):
        rendered = value
        for key, item in context.items():
            rendered = rendered.replace("{{" + key + "}}", str(item))
        return rendered
    return value


class TemplateHTTPVideoProvider(VideoGeneratorProvider):
    """Config-driven HTTP adapter for OpenCine's custom Provider form."""

    def __init__(self, config: Mapping[str, Any], *, client: JSONHTTPClient | None = None) -> None:
        self.config = dict(config)
        self.name = str(self.config.get("id") or self.config.get("name") or "custom-http")
        self.client = client or JSONHTTPClient(timeout_seconds=float(self.config.get("timeout_seconds", 45)))

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider=self.name, models=[str(self.config.get("model", "custom"))], supports_async=bool(self.config.get("poll")), metadata={"protocol": "template-http"})

    @staticmethod
    def _normalize_status(value: Any, *, artifact_uri: Any = None, default: str = "queued") -> str:
        status = str(value).strip().lower() if value is not None else ""
        if status in {"completed", "success", "done", "succeeded"}:
            return "succeeded"
        if status in {"failed", "error", "cancelled", "canceled"}:
            return "cancelled" if status in {"cancelled", "canceled"} else "failed"
        if status in {"queued", "pending"}:
            return "queued"
        if status in {"submitted"}:
            return "submitted"
        if status in {"running", "processing", "in_progress", "in-progress"}:
            return "running"
        if artifact_uri:
            return "succeeded"
        return status or default

    def estimate_cost(self, request: VideoRequest) -> CostRecord:
        amount = float(self.config.get("cost_per_second_usd", 0.0)) * float(request.shot.duration_seconds)
        return CostRecord(amount, provider=self.name, model=request.model or self.config.get("model"), estimated=True)

    def _context(self, request: VideoRequest, context: ProviderContext) -> dict[str, Any]:
        return {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "parameters": request.parameters,
            "model": request.model or self.config.get("model"),
            "project_id": context.project_id,
            "shot_id": context.shot_id or request.shot.id,
            "callback_url": context.callback_url or "",
            "references": request.reference_uris,
        }

    def _request(self, spec: Mapping[str, Any], context: Mapping[str, Any], *, default_url: str, default_method: str = "POST") -> Any:
        url = str(spec.get("url") or default_url)
        for key, value in context.items():
            url = url.replace("{{" + key + "}}", str(value))
        headers = _render_template(spec.get("headers") or self.config.get("headers") or {}, context)
        if self.config.get("api_key") and not any(str(key).lower() == "authorization" for key in headers):
            headers["Authorization"] = f"Bearer {self.config['api_key']}"
        body = _render_template(spec.get("body_template") or {}, context)
        return self.client.request(str(spec.get("method", default_method)).upper(), url, headers=headers, payload=body if body else None)

    def submit(self, request: VideoRequest, context: ProviderContext) -> ProviderJob:
        response = self._request(self.config, self._context(request, context), default_url=str(self.config.get("submit_url") or self.config.get("base_url")))
        result = self.config.get("result") or {}
        external_id = str(_json_path(response, result.get("job_id_path")) or _json_path(response, "$.id") or context.idempotency_key or new_id("custom-job"))
        artifact_uri = _json_path(response, result.get("artifact_path"))
        status = self._normalize_status(
            _json_path(response, result.get("status_path")),
            artifact_uri=artifact_uri,
            default="queued",
        )
        metadata = {"response": response, "artifact_uri": artifact_uri, "result": result}
        return ProviderJob(self.name, external_id, status=status, idempotency_key=context.idempotency_key, metadata=metadata)

    def poll(self, job: ProviderJob) -> PollResult:
        job.status = self._normalize_status(job.status, artifact_uri=job.metadata.get("artifact_uri"), default="queued")
        if job.status == "succeeded" and job.metadata.get("artifact_uri"):
            return PollResult(job, artifacts=self.fetch_artifacts(job))
        poll = self.config.get("poll") or {}
        if not poll:
            return PollResult(job)
        response = self._request(poll, {"external_id": job.external_id}, default_url=str(poll.get("url") or self.config.get("poll_url")), default_method="GET")
        result = self.config.get("result") or {}
        artifact_uri = _json_path(response, result.get("artifact_path"))
        status = self._normalize_status(
            _json_path(response, result.get("status_path")),
            artifact_uri=artifact_uri,
            default="running",
        )
        job.metadata = {**job.metadata, "response": response, "artifact_uri": artifact_uri}
        job.status = status
        return PollResult(job, artifacts=self.fetch_artifacts(job) if job.status == "succeeded" else [])

    def fetch_artifacts(self, job: ProviderJob) -> list[ArtifactRef]:
        uri = job.metadata.get("artifact_uri")
        if not uri:
            return []
        values = uri if isinstance(uri, list) else [uri]
        return [
            ArtifactRef(AssetKind.VIDEO, item.strip(), metadata={"provider": self.name})
            for item in values
            if isinstance(item, str) and item.strip()
        ]

    def cancel(self, job: ProviderJob) -> None:
        cancel_url = self.config.get("cancel_url")
        if cancel_url:
            self.client.request("POST", str(cancel_url).replace("{{external_id}}", job.external_id), payload={"id": job.external_id})

    def normalize_error(self, error: Exception) -> ProviderError:
        if isinstance(error, ProviderHTTPError):
            retryable = error.status == 0 or error.status == 429 or error.status >= 500
            code = "network_error" if error.status == 0 else f"http_{error.status}"
            details = {"status": error.status}
            if error.body is not None:
                details["body"] = error.body
            return ProviderError(code, str(error), retryable=retryable, provider=self.name, details=details)
        return ProviderError("provider_error", str(error), retryable=True, provider=self.name)


class JSONHTTPClient:
    def __init__(self, *, timeout_seconds: float = 45.0) -> None:
        self.timeout_seconds = timeout_seconds

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, payload: Mapping[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                body_json = json.loads(raw)
            except json.JSONDecodeError:
                body_json = raw
            raise ProviderHTTPError(error.code, f"HTTP {error.code} from provider", body_json) from error
        except urllib.error.URLError as error:
            raise ProviderHTTPError(0, f"Provider network error: {error.reason}") from error


class OpenAICompatibleLLMProvider(LLMProvider):
    """Small, SDK-free adapter for OpenAI-compatible chat endpoints.

    The adapter deliberately returns the model's text rather than an SDK
    response object.  Callers that need structured data can pass a JSON schema;
    parsing and validation stay at this boundary so the rest of the director
    never depends on vendor-specific message classes.
    """

    name = "openai-compatible-llm"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        client: JSONHTTPClient | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        name: str | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
            raise ValueError("temperature must be a finite number between 0 and 2")
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
            raise ValueError("max_tokens must be a positive integer")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.model = model.strip()
        self.client = client or JSONHTTPClient()
        self.temperature = float(temperature)
        self.max_tokens = max_tokens
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name must be a non-empty string")
            self.name = name.strip()

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions" if self.base_url.endswith("/v1") else f"{self.base_url}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _messages(
        self,
        prompt: str,
        *,
        system: str | None = None,
        content: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content if content is not None else prompt})
        return messages

    @staticmethod
    def _response_format(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if schema is None:
            return None
        if not isinstance(schema, Mapping) or not schema:
            raise ValueError("schema must be a non-empty JSON object")
        # json_schema is understood by current OpenAI-compatible servers.  A
        # server that only supports json_object can still accept the request by
        # overriding this method in a tiny adapter subclass.
        return {
            "type": "json_schema",
            "json_schema": {"name": "director_response", "strict": True, "schema": dict(schema)},
        }

    @staticmethod
    def _content_text(response: Any) -> str:
        if not isinstance(response, Mapping):
            raise ValueError("LLM response must be a JSON object")  # noqa: TRY004
        choices = response.get("choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ValueError("LLM response is missing choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("LLM response choice must be an object")  # noqa: TRY004
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("LLM response is missing message")  # noqa: TRY004
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("LLM response content is empty")
            return content
        # Some compatible servers return content blocks.  Preserve only text
        # blocks and reject an empty/unknown response rather than silently
        # accepting an incomplete completion.
        if isinstance(content, (list, tuple)):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            text = "".join(parts)
            if text.strip():
                return text
        raise ValueError("LLM response content must be a non-empty string")

    def _complete_text(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: Mapping[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        response_format = self._response_format(schema)
        if response_format is not None:
            payload["response_format"] = response_format
        try:
            response = self.client.request("POST", self.endpoint, headers=self._headers(), payload=payload)
        except ProviderHTTPError:
            raise
        except Exception as error:
            raise ProviderHTTPError(0, f"LLM request failed: {error}") from error
        return self._content_text(response)

    def complete(self, prompt: str, *, schema: Mapping[str, Any] | None = None) -> str:
        return self._complete_text(messages=self._messages(prompt), schema=schema)


class OpenAICompatibleVLMJudgeProvider(OpenAICompatibleLLMProvider, VLMJudgeProvider):
    """OpenAI-compatible multimodal judge for per-shot acceptance criteria.

    The model is instructed to emit a small, provider-neutral JSON document.
    The resulting domain object intentionally keeps omitted criteria/evidence
    visible; :class:`QualityController` then applies the project's fail-closed
    policy instead of allowing a permissive provider response through.
    """

    name = "openai-compatible-vlm-judge"

    _JUDGE_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "required": ["shot_id", "verdict", "criterion_results"],
        "properties": {
            "shot_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "summary": {"type": "string"},
            "criterion_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["criterion_id", "verdict", "evidence", "confidence"],
                    "properties": {
                        "criterion_id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                        "evidence": {"type": "array", "items": {"type": "object"}},
                        "failure_code": {"type": ["string", "null"]},
                        "reason": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "repair_suggestions": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
        "additionalProperties": True,
    }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            models=[self.model],
            supports_async=False,
            supported_audio=True,
            metadata={"protocol": "openai-compatible-chat", "modalities": ["text", "image", "video-uri"]},
        )

    @staticmethod
    def _judge_prompt(input: JudgeInput) -> tuple[str, list[dict[str, Any]]]:
        criteria = [
            {
                "id": criterion.id,
                "statement": criterion.statement,
                "category": criterion.category.value,
                "severity": criterion.severity.value,
                "evidence_types": criterion.evidence_types,
                "threshold": criterion.threshold,
            }
            for criterion in input.shot.acceptance_criteria
        ]
        context = dict(input.context) if isinstance(input.context, Mapping) else {}
        text = (
            "Evaluate this video shot against every planned acceptance criterion."
            " Return JSON only. Each criterion must appear exactly once."
            " A PASS requires concrete evidence with a URI and valid confidence;"
            " missing or uncertain evidence must be reported as FAIL.\n\n"
            f"Shot: {input.shot.id}\nDescription: {input.shot.description}\n"
            f"Duration seconds: {input.shot.duration_seconds}\n"
            f"Acceptance criteria: {json.dumps(criteria, ensure_ascii=False)}\n"
            f"Context: {json.dumps(context, ensure_ascii=False, default=str)}"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for artifact in input.artifacts:
            if not isinstance(artifact, ArtifactRef) or not isinstance(artifact.uri, str) or not artifact.uri.strip():
                continue
            if artifact.kind == AssetKind.IMAGE:
                content.append({"type": "image_url", "image_url": {"url": artifact.uri.strip()}})
            else:
                content.append({"type": "text", "text": f"Artifact ({artifact.kind.value}): {artifact.uri.strip()}"})
        return text, content

    @staticmethod
    def _json_object(text: str) -> Mapping[str, Any]:
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("VLM judge returned invalid JSON") from error
        if not isinstance(value, Mapping):
            raise ValueError("VLM judge JSON response must be an object")  # noqa: TRY004
        return value

    @staticmethod
    def _evidence(raw: Any) -> list[Evidence]:
        if not isinstance(raw, (list, tuple)):
            return []
        evidence: list[Evidence] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            uri = item.get("uri") or item.get("url")
            if not isinstance(uri, str) or not uri.strip():
                continue
            timestamp = item.get("timestamp_seconds")
            if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))):
                timestamp = None
            metadata = item.get("metadata")
            if not isinstance(metadata, Mapping):
                metadata = {}
            evidence.append(Evidence(str(item.get("kind", "frame")), uri.strip(), timestamp, item.get("excerpt"), dict(metadata)))
        return evidence

    @staticmethod
    def _repair_suggestions(raw: Any) -> list[RepairKind]:
        if not isinstance(raw, (list, tuple)):
            return []
        result: list[RepairKind] = []
        for value in raw:
            try:
                result.append(value if isinstance(value, RepairKind) else RepairKind(str(value)))
            except (TypeError, ValueError):
                result.append(RepairKind.HUMAN)
        return list(dict.fromkeys(result))

    def judge(self, input: JudgeInput) -> JudgeResult:
        if not isinstance(input, JudgeInput):
            raise TypeError("VLM judge requires a JudgeInput")
        if not input.shot.acceptance_criteria:
            raise ValueError("VLM judge requires planned acceptance criteria")
        _, content = self._judge_prompt(input)
        raw = self._json_object(
            self._complete_text(
                messages=self._messages(
                    "Judge each criterion independently and provide evidence-grounded JSON.",
                    system="You are a strict video quality judge. Never infer evidence that is not supplied.",
                    content=content,
                ),
                schema=self._JUDGE_SCHEMA,
            )
        )
        raw_results = raw.get("criterion_results")
        if not isinstance(raw_results, (list, tuple)):
            raise ValueError("VLM judge response is missing criterion_results array")  # noqa: TRY004
        results: list[CriterionResult] = []
        for item in raw_results:
            if not isinstance(item, Mapping):
                # Keep malformed entries visible to QualityController as a
                # deterministic failure rather than dropping the whole shot.
                results.append(CriterionResult("unknown", Verdict.FAIL, [], "malformed_criterion", "Criterion result is not an object", 0.0, [RepairKind.HUMAN]))
                continue
            confidence = item.get("confidence", 0.0)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError, OverflowError):
                confidence = float("nan")
            verdict = item.get("verdict", Verdict.FAIL)
            if isinstance(verdict, str):
                try:
                    verdict = Verdict(verdict.upper())
                except ValueError:
                    verdict = Verdict.FAIL
            if not isinstance(verdict, Verdict):
                verdict = Verdict.FAIL
            results.append(
                CriterionResult(
                    str(item.get("criterion_id", "")),
                    verdict,
                    self._evidence(item.get("evidence")),
                    item.get("failure_code") if isinstance(item.get("failure_code"), str) else None,
                    item.get("reason") if isinstance(item.get("reason"), str) else None,
                    confidence,
                    self._repair_suggestions(item.get("repair_suggestions")),
                )
            )
        shot_id = raw.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise ValueError("VLM judge response is missing shot_id")
        verdict = raw.get("verdict", Verdict.FAIL)
        if isinstance(verdict, str):
            try:
                verdict = Verdict(verdict.upper())
            except ValueError:
                verdict = Verdict.FAIL
        if not isinstance(verdict, Verdict):
            verdict = Verdict.FAIL
        return JudgeResult(
            shot_id.strip(),
            verdict,
            results,
            self.name,
            self.model,
            datetime.now(UTC),
            str(raw.get("summary", "")) if raw.get("summary") is not None else "",
        )


class FalLikeAsyncVideoProvider(VideoGeneratorProvider):
    """Adapter for Fal/Replicate-style submit-and-poll APIs.

    The default mapping follows the common queue shape (`request_id`, status
    endpoint, response artifacts).  Individual vendors can subclass and only
    override `build_payload`, `parse_submit`, or `parse_poll`.
    """

    name = "fal-like"

    def __init__(self, base_url: str, *, api_key: str | None = None, model: str = "video-model", client: JSONHTTPClient | None = None, cost_per_second_usd: float = 0.25) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("FAL_API_KEY") or os.getenv("REPLICATE_API_TOKEN")
        self.default_model = model
        self.client = client or JSONHTTPClient()
        self.cost_per_second_usd = cost_per_second_usd

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(self.name, [self.default_model], True, True, True, 120.0, ["16:9", "9:16", "1:1"], True, {"protocol": "fal-compatible"})

    def estimate_cost(self, request: VideoRequest) -> CostRecord:
        return CostRecord(max(request.shot.duration_seconds, 1.0) * self.cost_per_second_usd, provider=self.name, model=request.model or self.default_model, estimated=True)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self.api_key}"} if self.api_key else {}

    def build_payload(self, request: VideoRequest, context: ProviderContext) -> dict[str, Any]:
        return {
            "model": request.model or self.default_model,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "duration": request.shot.duration_seconds,
            "aspect_ratio": request.parameters.get("aspect_ratio", "16:9"),
            "fps": request.parameters.get("fps", 24),
            "parameters": request.parameters,
            "references": request.reference_uris,
            "webhook_url": context.callback_url,
        }

    def submit(self, request: VideoRequest, context: ProviderContext) -> ProviderJob:
        endpoint = f"{self.base_url}/queue/submit"
        payload = self.build_payload(request, context)
        if context.idempotency_key:
            payload["idempotency_key"] = context.idempotency_key
        response = self.client.request("POST", endpoint, headers=self._headers(), payload=payload)
        if not isinstance(response, Mapping):
            raise TypeError("provider submit returned a non-object JSON response")
        raw_external_id = response.get("request_id") if response.get("request_id") is not None else response.get("id")
        if not isinstance(raw_external_id, str) or not raw_external_id.strip():
            raise ValueError("provider submit must return a non-empty request_id or id")
        external_id = raw_external_id.strip()
        status_url = response.get("status_url") if response.get("status_url") is not None else f"{self.base_url}/queue/status/{external_id}"
        if not isinstance(status_url, str) or not status_url.strip():
            raise ValueError("provider submit returned an invalid status URL")
        status = self._normalize_status(response.get("status", "queued"))
        if status is None:
            raise ValueError(f"provider submit returned an unsupported job status: {response.get('status')!r}")
        response_model = response.get("model") if response.get("model") is not None else payload.get("model")
        if response_model is not None and not isinstance(response_model, str):
            raise ValueError("provider submit returned an invalid model")
        expected_model = payload.get("model")
        if response_model and expected_model and response_model != expected_model:
            raise ValueError("provider submit returned a different model")
        return ProviderJob(
            self.name,
            external_id,
            status,
            context.idempotency_key,
            metadata={"status_url": status_url, "response": dict(response), "model": response_model or payload.get("model")},
        )

    @staticmethod
    def _normalize_status(value: Any) -> str | None:
        status = str(value).strip().lower() if value is not None else ""
        return {
            "completed": "succeeded",
            "success": "succeeded",
            "done": "succeeded",
            "error": "failed",
        }.get(status, status) if status in {"queued", "submitted", "running", "succeeded", "failed", "cancelled", "completed", "success", "done", "error"} else None

    def parse_poll(self, response: Mapping[str, Any], job: ProviderJob) -> PollResult:
        if not isinstance(response, Mapping):
            raise TypeError("provider poll returned a non-object JSON response")
        raw_status = response.get("status", response.get("state", "queued"))
        status = self._normalize_status(raw_status)
        if status is None:
            raise ValueError(f"provider returned an unsupported job status: {raw_status!r}")
        updated = replace(job, status=status)
        if status in {"failed", "cancelled"}:
            return PollResult(updated, error=ProviderError(str(response.get("code", "provider_failed")), str(response.get("error", "Provider job failed")), retryable=status != "cancelled", provider=self.name, details=dict(response)))
        if status != "succeeded":
            return PollResult(updated)
        output = self._first_present(response, "output", "video", "artifacts", "data")
        urls: list[str] = []
        if isinstance(output, str):
            urls = [output]
        elif isinstance(output, Mapping):
            candidate = output.get("video_url") or output.get("url") or output.get("video")
            if candidate is not None and not isinstance(candidate, str):
                raise ValueError("provider output URL must be a string")
            urls = [candidate] if isinstance(candidate, str) and candidate.strip() else []
        elif isinstance(output, (list, tuple)):
            for item in output:
                if isinstance(item, str):
                    if item.strip():
                        urls.append(item.strip())
                    continue
                if not isinstance(item, Mapping):
                    raise TypeError("provider output list contains a non-object artifact")
                candidate = item.get("url") or item.get("video_url") or item.get("video")
                if candidate is not None and not isinstance(candidate, str):
                    raise ValueError("provider output URL must be a string")
                if isinstance(candidate, str) and candidate.strip():
                    urls.append(candidate.strip())
        elif output is not None:
            raise TypeError("provider output must be a string, object, or array")
        artifacts = [ArtifactRef(AssetKind.VIDEO, url, metadata={"provider": self.name, "external_id": job.external_id, "model": job.metadata.get("model", self.default_model)}) for url in urls if isinstance(url, str) and url.strip() and url.strip().lower() != "none"]
        cost_raw = response.get("cost_usd") if "cost_usd" in response else response.get("cost")
        cost = None
        if cost_raw is not None:
            try:
                amount = float(cost_raw)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("provider returned an invalid cost") from error
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("provider returned an invalid cost")
            response_model = response.get("model")
            expected_model = job.metadata.get("model", self.default_model) if isinstance(job.metadata, Mapping) else self.default_model
            if response_model is not None and (not isinstance(response_model, str) or (expected_model and response_model != expected_model)):
                raise ValueError("provider returned a cost for a different model")
            cost = CostRecord(amount, provider=self.name, model=expected_model)
        return PollResult(updated, artifacts, cost)

    @staticmethod
    def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        return None

    def poll(self, job: ProviderJob) -> PollResult:
        if not isinstance(job, ProviderJob):
            raise TypeError("provider poll requires a ProviderJob")
        try:
            if not isinstance(job.metadata, Mapping):
                raise TypeError("provider job metadata must be an object")
            status_url = job.metadata.get("status_url") or f"{self.base_url}/queue/status/{job.external_id}"
            response = self.client.request("GET", status_url, headers=self._headers())
            return self.parse_poll(response, job)
        except (ProviderHTTPError, OSError, TypeError, ValueError, OverflowError, AttributeError) as error:
            return PollResult(replace(job, status="failed"), error=self.normalize_error(error))

    def parse_callback(self, payload: Mapping[str, Any], job: ProviderJob) -> PollResult:
        """Parse a webhook using the same strict contract as polling."""
        return self.parse_poll(payload, job)

    def cancel(self, job: ProviderJob) -> None:
        try:
            self.client.request("POST", f"{self.base_url}/queue/cancel/{job.external_id}", headers=self._headers(), payload={})
        except (ProviderHTTPError, OSError, TypeError, ValueError, OverflowError):
            return

    def fetch_artifacts(self, job: ProviderJob) -> list[ArtifactRef]:
        return self.poll(job).artifacts

    def normalize_error(self, error: Exception) -> ProviderError:
        if isinstance(error, ProviderHTTPError):
            return ProviderError(f"http_{error.status}", str(error), retryable=error.status == 0 or error.status >= 500, provider=self.name, details={"body": error.body})
        return ProviderError("adapter_error", str(error), retryable=True, provider=self.name)


class ComfyUIProvider(FalLikeAsyncVideoProvider):
    """ComfyUI prompt-queue adapter using `/prompt` and `/history`."""

    name = "comfyui"

    def __init__(self, base_url: str | None = None, *, workflow: Mapping[str, Any] | None = None, client: JSONHTTPClient | None = None, cost_per_second_usd: float = 0.0) -> None:
        super().__init__(base_url or os.getenv("COMFYUI_BASE_URL", "http://localhost:8188"), model="comfy-workflow", client=client, cost_per_second_usd=cost_per_second_usd)
        self.workflow = dict(workflow or {})

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(self.name, ["comfy-workflow"], True, True, False, None, ["16:9", "9:16", "1:1"], True, {"queue_endpoint": "/prompt"})

    def build_payload(self, request: VideoRequest, context: ProviderContext) -> dict[str, Any]:
        workflow = json.loads(json.dumps(self.workflow))
        return {"prompt": workflow, "client_id": context.project_id, "extra_data": {"prompt": request.prompt, "negative_prompt": request.negative_prompt, "parameters": request.parameters, "idempotency_key": context.idempotency_key}}

    def submit(self, request: VideoRequest, context: ProviderContext) -> ProviderJob:
        response = self.client.request("POST", f"{self.base_url}/prompt", payload=self.build_payload(request, context))
        if not isinstance(response, Mapping):
            raise TypeError("ComfyUI submit returned a non-object JSON response")
        raw_external_id = response.get("prompt_id") if response.get("prompt_id") is not None else response.get("id")
        if not isinstance(raw_external_id, str) or not raw_external_id.strip():
            raise ValueError("ComfyUI submit must return a non-empty prompt_id or id")
        external_id = raw_external_id.strip()
        return ProviderJob(self.name, external_id, "queued", context.idempotency_key, metadata={"status_url": f"{self.base_url}/history/{external_id}"})

    def poll(self, job: ProviderJob) -> PollResult:
        if not isinstance(job, ProviderJob):
            raise TypeError("provider poll requires a ProviderJob")
        try:
            if not isinstance(job.metadata, Mapping):
                raise TypeError("provider job metadata must be an object")
            response = self.client.request("GET", job.metadata.get("status_url", f"{self.base_url}/history/{job.external_id}"))
            if not isinstance(response, Mapping):
                raise TypeError("ComfyUI poll returned a non-object JSON response")
            if job.external_id in response:
                response = response[job.external_id]
            if not isinstance(response, Mapping):
                raise TypeError("ComfyUI history entry must be an object")
            raw_status = response.get("status", {})
            if raw_status is None:
                raw_status = {}
            if not isinstance(raw_status, Mapping):
                raise TypeError("ComfyUI status must be an object")
            status = "succeeded" if response.get("outputs") is not None and response.get("outputs") != {} else str(raw_status.get("status_str", "running")).lower()
            status = {"completed": "succeeded", "success": "succeeded", "done": "succeeded", "error": "failed"}.get(status, status)
            if status not in {"running", "queued", "submitted", "succeeded", "failed", "cancelled"}:
                raise ValueError(f"ComfyUI returned an unsupported status: {status!r}")
            if status in {"error", "failed"}:
                return PollResult(replace(job, status="failed"), error=ProviderError("comfy_failed", "ComfyUI execution failed", retryable=True, provider=self.name, details=dict(response)))
            if status != "succeeded":
                return PollResult(replace(job, status="running"))
            urls: list[str] = []
            outputs = response.get("outputs") if response.get("outputs") is not None else {}
            if not isinstance(outputs, Mapping):
                raise TypeError("ComfyUI outputs must be an object")
            for node in outputs.values():
                if not isinstance(node, Mapping):
                    raise TypeError("ComfyUI output node must be an object")
                output_items: list[Any] = []
                for key in ("gifs", "videos", "images"):
                    values = node.get(key)
                    if values is None:
                        continue
                    if not isinstance(values, (list, tuple)):
                        raise TypeError(f"ComfyUI output node {key} must be an array")
                    output_items.extend(values)
                for item in output_items:
                    if not isinstance(item, Mapping):
                        raise TypeError("ComfyUI output item must be an object")
                    filename = item.get("filename")
                    if filename is not None and not isinstance(filename, str):
                        raise ValueError("ComfyUI output filename must be a string")
                    if isinstance(filename, str) and filename.strip():
                        subfolder = item.get("subfolder", "")
                        output_type = item.get("type", "output")
                        if not isinstance(subfolder, str) or not isinstance(output_type, str):
                            raise ValueError("ComfyUI output subfolder and type must be strings")
                        urls.append(f"{self.base_url}/view?filename={filename}&subfolder={subfolder}&type={output_type}")
                    elif filename is None:
                        raise ValueError("ComfyUI output item is missing filename")
            artifacts = [ArtifactRef(AssetKind.VIDEO, url, metadata={"provider": self.name, "external_id": job.external_id}) for url in urls]
            return PollResult(replace(job, status="succeeded"), artifacts, CostRecord(0.0, provider=self.name, model=self.default_model))
        except (ProviderHTTPError, OSError, TypeError, ValueError, OverflowError) as error:
            return PollResult(replace(job, status="failed"), error=self.normalize_error(error))
