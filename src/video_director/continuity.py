"""Cross-shot continuity checks that fail closed when evidence is absent."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from .schemas import ArtifactRef, Evidence, Project, Shot, Verdict


@dataclass(slots=True)
class ContinuityIssue:
    category: str
    severity: str
    message: str
    shot_ids: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class ContinuityReport:
    verdict: Verdict
    issues: list[ContinuityIssue]
    checked_shot_ids: list[str]

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS and not self.issues


class ContinuityGuardian:
    """Deterministic baseline checks; richer embeddings can be plugged in later."""

    def check(self, project: Project, shots: Sequence[Shot], artifacts_by_shot: dict[str, Sequence[ArtifactRef]]) -> ContinuityReport:
        issues: list[ContinuityIssue] = []
        ordered = sorted(shots, key=lambda shot: shot.sequence)
        if not ordered:
            issues.append(ContinuityIssue("coverage", "blocking", "No shots were supplied for continuity verification"))
        for previous, current in pairwise(ordered):
            if current.previous_shot_id and current.previous_shot_id != previous.id:
                issues.append(ContinuityIssue("screen_direction", "major", "Shot dependency does not match sequence order", [previous.id, current.id]))
            previous_artifacts = artifacts_by_shot.get(previous.id, ())
            current_artifacts = artifacts_by_shot.get(current.id, ())
            if not previous_artifacts or not current_artifacts:
                issues.append(ContinuityIssue("evidence", "blocking", "Adjacent shots are missing artifacts for continuity comparison", [previous.id, current.id]))
                continue
            prev_raw = previous_artifacts[-1].metadata
            curr_raw = current_artifacts[-1].metadata
            if not isinstance(prev_raw, Mapping) or not isinstance(curr_raw, Mapping):
                issues.append(ContinuityIssue("evidence", "blocking", "Shot artifact metadata is not an object", [previous.id, current.id]))
                continue
            prev_meta = prev_raw
            curr_meta = curr_raw
            for category, key, message, severity in (
                ("character", "character_signature", "Character signature drifted between adjacent shots", "blocking"),
                ("location", "location_signature", "Location signature drifted between adjacent shots", "major"),
                ("style", "style_signature", "Style signature drifted between adjacent shots", "major"),
            ):
                expected = self._signature_required(project, previous, current, category)
                previous_signature = prev_meta.get(key)
                current_signature = curr_meta.get(key)
                missing = previous_signature in (None, "") or current_signature in (None, "")
                invalid = (
                    (previous_signature not in (None, "") and (not isinstance(previous_signature, str) or not previous_signature.strip()))
                    or (current_signature not in (None, "") and (not isinstance(current_signature, str) or not current_signature.strip()))
                )
                if expected and missing:
                    issues.append(ContinuityIssue("evidence", "blocking", f"Missing {category} signature evidence for adjacent shots", [previous.id, current.id]))
                elif invalid:
                    issues.append(ContinuityIssue("evidence", "blocking", f"Invalid {category} signature evidence for adjacent shots", [previous.id, current.id]))
                elif previous_signature and current_signature and previous_signature != current_signature:
                    issues.append(ContinuityIssue(category, severity, message, [previous.id, current.id]))
        return ContinuityReport(Verdict.FAIL if issues else Verdict.PASS, issues, [shot.id for shot in ordered])

    @staticmethod
    def _signature_required(project: Project, previous: Shot, current: Shot, category: str) -> bool:
        if category == "character":
            return bool(previous.character_ids or current.character_ids)
        if category == "location":
            return bool(previous.location_id or current.location_id)
        if category == "style":
            return bool(project.active_plan and project.active_plan.style_bible)
        return False
