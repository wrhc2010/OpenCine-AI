"""Fail-closed quality loop and bounded repair policy."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace

from .providers.base import JudgeInput, VLMJudgeProvider
from .schemas import (
    AcceptanceCriterion,
    ArtifactRef,
    Attempt,
    CriterionResult,
    Diagnosis,
    Evidence,
    JudgeResult,
    RepairAction,
    RepairKind,
    Shot,
    Verdict,
)


class QualityController:
    def __init__(self, judge: VLMJudgeProvider, *, max_attempts: int = 3, min_confidence: float = 0.5) -> None:
        self.judge = judge
        self.max_attempts = max_attempts
        self.min_confidence = min(max(min_confidence, 0.0), 1.0)

    def evaluate(self, shot: Shot, artifacts: Sequence[ArtifactRef], context: Mapping[str, object] | None = None) -> JudgeResult:
        context = context or {}
        acceptance_mode = str(context.get("acceptance_mode", "standard")).lower()
        if acceptance_mode == "none":
            artifact = next((item for item in artifacts if getattr(item, "uri", "")), None)
            criteria: list[CriterionResult] = []
            for criterion in shot.acceptance_criteria:
                evidence = [] if artifact is None else [Evidence("metadata", artifact.uri, metadata={"mode": "none", "criterion_id": criterion.id})]
                technical = criterion.category.value in {"technical", "safety"}
                if technical and artifact is None:
                    criteria.append(CriterionResult(criterion.id, Verdict.FAIL, [], "missing_artifact", "未找到可验证的媒体产物", 0.0, [RepairKind.HUMAN]))
                else:
                    criteria.append(
                        CriterionResult(
                            criterion.id,
                            Verdict.PASS,
                            evidence,
                            reason="已关闭语义验收；该项由技术门禁或策略记录覆盖" if technical else "语义验收已跳过",
                            confidence=1.0,
                            skipped=not technical,
                        )
                    )
            verdict = Verdict.FAIL if any(item.verdict == Verdict.FAIL for item in criteria) else Verdict.PASS
            return JudgeResult(shot.id, verdict, criteria, "disabled", "none", summary="semantic acceptance disabled")
        min_confidence = {"low": 0.2, "standard": self.min_confidence, "strict": 0.8, "custom": self.min_confidence}.get(acceptance_mode, self.min_confidence)
        provider_name = getattr(self.judge, "name", self.judge.__class__.__name__)
        try:
            result = self.judge.judge(JudgeInput(shot, artifacts, context or {}))
        except Exception as error:  # noqa: BLE001 - a judge failure must fail closed
            return self._judge_failure(shot, provider_name, f"Judge provider error: {error}")
        if not isinstance(result, JudgeResult):
            return self._judge_failure(shot, provider_name, "Judge returned an invalid result object")
        if result.shot_id != shot.id:
            return self._judge_failure(shot, provider_name, "Judge returned a result for a different shot")
        expected = {criterion.id for criterion in shot.acceptance_criteria}
        expected_criteria = {criterion.id: criterion for criterion in shot.acceptance_criteria}
        normalized: list[CriterionResult] = []
        seen: set[str] = set()
        for raw in result.criterion_results if isinstance(result.criterion_results, list) else []:
            criterion = self._coerce_criterion(raw)
            if criterion is None:
                normalized.append(CriterionResult("unknown", Verdict.FAIL, [], "malformed_criterion", "Judge returned a malformed criterion result", 0.0, [RepairKind.HUMAN]))
                continue
            if criterion.criterion_id in seen:
                normalized.append(CriterionResult(criterion.criterion_id, Verdict.FAIL, [], "duplicate_criterion", "Judge returned the same criterion more than once", 0.0, [RepairKind.HUMAN]))
                continue
            seen.add(criterion.criterion_id)
            if criterion.criterion_id not in expected:
                normalized.append(CriterionResult(criterion.criterion_id, Verdict.FAIL, [], "unknown_criterion", "Judge returned a criterion that was not in the approved plan", 0.0, [RepairKind.HUMAN]))
                continue
            criterion = self._normalize_verdict(criterion)
            criterion = criterion.fail_closed()
            planned = expected_criteria.get(criterion.criterion_id)
            if planned is not None:
                criterion = self._validate_evidence(criterion, planned, shot.duration_seconds)
            if criterion.verdict == Verdict.PASS and criterion.confidence < min_confidence:
                criterion = CriterionResult(criterion.criterion_id, Verdict.FAIL, criterion.evidence, "low_confidence", "Judge confidence is below the configured acceptance threshold", criterion.confidence, criterion.repair_suggestions or [RepairKind.HUMAN])
            normalized.append(criterion)
        observed = {criterion.criterion_id for criterion in normalized}
        missing = expected - observed
        if missing:
            normalized.extend(CriterionResult(mid, Verdict.FAIL, [], "missing_criterion", "Judge omitted a planned acceptance criterion", 0.0, [RepairKind.HUMAN]) for mid in sorted(missing))
        failed = [criterion for criterion in normalized if criterion.verdict == Verdict.FAIL]
        return replace(result, criterion_results=normalized, verdict=Verdict.FAIL if failed else Verdict.PASS)

    @staticmethod
    def _validate_evidence(
        result: CriterionResult,
        criterion: AcceptanceCriterion,
        duration_seconds: float,
    ) -> CriterionResult:
        """Validate evidence against the acceptance criterion contract."""
        allowed = {
            str(kind).strip().lower()
            for kind in criterion.evidence_types
            if isinstance(kind, str) and kind.strip()
        }
        if not allowed:
            return CriterionResult(
                result.criterion_id,
                Verdict.FAIL,
                result.evidence,
                "invalid_evidence_policy",
                "Acceptance criterion does not declare a valid evidence type",
                0.0,
                result.repair_suggestions or [RepairKind.HUMAN],
            )

        normalized: list[Evidence] = []
        unsupported: set[str] = set()
        out_of_range = False
        malformed_range = False
        for evidence in result.evidence:
            kind = evidence.kind.strip().lower()
            if kind not in allowed:
                unsupported.add(kind)
            metadata = evidence.metadata
            if not isinstance(metadata, Mapping):
                malformed_range = True
            else:
                ranges: list[tuple[object, object]] = []
                if "time_range" in metadata:
                    value = metadata.get("time_range")
                    if isinstance(value, (list, tuple)) and len(value) == 2:
                        ranges.append((value[0], value[1]))
                    else:
                        malformed_range = True
                if "start_seconds" in metadata or "end_seconds" in metadata:
                    ranges.append((metadata.get("start_seconds", 0.0), metadata.get("end_seconds")))
                for start_raw, end_raw in ranges:
                    try:
                        start = float(start_raw)
                        end = duration_seconds if end_raw is None else float(end_raw)
                    except (TypeError, ValueError, OverflowError):
                        malformed_range = True
                        continue
                    if (
                        not math.isfinite(start)
                        or not math.isfinite(end)
                        or start < 0
                        or end < start
                        or end > duration_seconds
                    ):
                        out_of_range = True
            normalized.append(replace(evidence, kind=kind))

        if unsupported:
            kinds = ", ".join(sorted(unsupported))
            return CriterionResult(
                result.criterion_id,
                Verdict.FAIL,
                normalized,
                "evidence_type_mismatch",
                f"Evidence kind(s) {kinds} are not allowed for this criterion; expected one of {sorted(allowed)}",
                0.0,
                result.repair_suggestions or [RepairKind.HUMAN],
            )
        if malformed_range:
            return CriterionResult(
                result.criterion_id,
                Verdict.FAIL,
                normalized,
                "invalid_evidence_range",
                "Evidence time range metadata must contain finite numeric bounds",
                0.0,
                result.repair_suggestions or [RepairKind.HUMAN],
            )
        if out_of_range:
            return CriterionResult(
                result.criterion_id,
                Verdict.FAIL,
                normalized,
                "evidence_out_of_range",
                "Evidence references a time outside the shot duration",
                0.0,
                result.repair_suggestions or [RepairKind.HUMAN],
            )
        return replace(result, evidence=normalized)

    @staticmethod
    def _coerce_criterion(raw: object) -> CriterionResult | None:
        if isinstance(raw, CriterionResult):
            return raw
        if not isinstance(raw, Mapping):
            return None
        evidence: list[Evidence] = []
        for item in raw.get("evidence", []) if isinstance(raw.get("evidence", []), list) else []:
            if isinstance(item, Evidence):
                evidence.append(item)
            elif isinstance(item, Mapping) and item.get("uri"):
                uri = item.get("uri")
                if not isinstance(uri, str) or not uri.strip():
                    continue
                metadata = item.get("metadata")
                # Preserve a non-object metadata value so CriterionResult's
                # fail-closed validation can reject the evidence deterministically.
                if metadata is None:
                    metadata = {}
                evidence.append(Evidence(str(item.get("kind", "frame")), uri.strip(), item.get("timestamp_seconds"), item.get("excerpt"), metadata))
        suggestions: list[RepairKind] = []
        for value in raw.get("repair_suggestions", []) if isinstance(raw.get("repair_suggestions", []), list) else []:
            try:
                suggestions.append(value if isinstance(value, RepairKind) else RepairKind(str(value)))
            except ValueError:
                suggestions.append(RepairKind.HUMAN)
        confidence_raw = raw.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = float("nan")
        return CriterionResult(str(raw.get("criterion_id", "")), raw.get("verdict", Verdict.FAIL), evidence, raw.get("failure_code"), raw.get("reason"), confidence, suggestions, bool(raw.get("skipped", False)))

    @staticmethod
    def _normalize_verdict(criterion: CriterionResult) -> CriterionResult:
        verdict = criterion.verdict
        if isinstance(verdict, str):
            try:
                verdict = Verdict(verdict.upper())
            except ValueError:
                verdict = Verdict.FAIL
        if not isinstance(verdict, Verdict):
            verdict = Verdict.FAIL
        return replace(criterion, verdict=verdict)

    @staticmethod
    def _judge_failure(shot: Shot, provider: str, reason: str) -> JudgeResult:
        criteria = [CriterionResult(item.id, Verdict.FAIL, [], "judge_error", reason, 0.0, [RepairKind.HUMAN]) for item in shot.acceptance_criteria]
        return JudgeResult(shot.id, Verdict.FAIL, criteria, provider, summary=reason)

    def diagnose(self, result: JudgeResult) -> Diagnosis:
        failures = result.failed_criteria
        codes = sorted({failure.failure_code or "unknown_failure" for failure in failures})
        causes = [failure.reason or "No root cause supplied by judge" for failure in failures]
        repairs: list[RepairKind] = []
        for failure in failures:
            repairs.extend(failure.repair_suggestions)
        if not repairs:
            repairs = [RepairKind.PROMPT, RepairKind.PARAMETERS, RepairKind.REFERENCE, RepairKind.PROVIDER, RepairKind.SPLIT_SHOT, RepairKind.HUMAN]
        unique = list(dict.fromkeys(repairs))
        return Diagnosis(result.shot_id, codes, causes, unique, min((failure.confidence for failure in failures), default=0.0))

    def choose_repair(
        self,
        shot: Shot,
        attempt: Attempt,
        diagnosis: Diagnosis,
        *,
        provider_fallback: str | None = None,
        max_attempts: int | None = None,
    ) -> RepairAction | None:
        """Choose the next repair using the caller's effective retry wave.

        Manual retries may continue numbering attempts from a previous wave.
        Accepting an explicit limit keeps that policy local to the command and
        avoids mutating a shared controller while another worker is running.
        """
        limit = self.max_attempts if max_attempts is None else max(1, int(max_attempts))
        if attempt.number >= limit:
            return RepairAction(shot.id, RepairKind.HUMAN, {"reason": "maximum attempts reached"}, "Bounded retries exhausted; human decision required", attempt_number=attempt.number)
        kind = next((candidate for candidate in (RepairKind.PROMPT, RepairKind.PARAMETERS, RepairKind.REFERENCE, RepairKind.PROVIDER, RepairKind.SPLIT_SHOT, RepairKind.HUMAN) if candidate in diagnosis.recommended_repairs), RepairKind.HUMAN)
        prompt = shot.prompt_bundle
        if prompt is None:
            return RepairAction(shot.id, RepairKind.HUMAN, {}, "Shot has no prompt bundle", attempt_number=attempt.number)
        if kind == RepairKind.PROMPT:
            updated = replace(prompt, positive=prompt.positive + " Explicitly satisfy every failed acceptance criterion; preserve identity, geography and motion continuity.", version=prompt.version + 1, parameters={**prompt.parameters, "attempt": attempt.number + 1})
            return RepairAction(shot.id, kind, {"prompt_bundle": updated}, "Strengthen the prompt using failed criterion evidence", attempt_number=attempt.number + 1)
        if kind == RepairKind.PARAMETERS:
            params = {**prompt.parameters, "guidance_scale": prompt.parameters.get("guidance_scale", 7) + 1, "attempt": attempt.number + 1}
            return RepairAction(shot.id, kind, {"prompt_bundle": replace(prompt, parameters=params, version=prompt.version + 1)}, "Adjust generation parameters after quality failure", attempt_number=attempt.number + 1)
        if kind == RepairKind.REFERENCE:
            return RepairAction(shot.id, kind, {"reference_policy": "refresh_character_and_location_references", "attempt": attempt.number + 1}, "Refresh source-of-truth references", attempt_number=attempt.number + 1)
        if kind == RepairKind.PROVIDER:
            return RepairAction(shot.id, kind, {"provider": provider_fallback or "next-capable-provider", "attempt": attempt.number + 1}, "Switch provider after repeated generation failure", attempt_number=attempt.number + 1)
        if kind == RepairKind.SPLIT_SHOT:
            return RepairAction(shot.id, kind, {"split_at": [shot.duration_seconds / 2], "attempt": attempt.number + 1}, "Split the shot to reduce motion and continuity load", attempt_number=attempt.number + 1)
        return RepairAction(shot.id, RepairKind.HUMAN, {}, "Judge recommends human intervention", attempt_number=attempt.number + 1)
