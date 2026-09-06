"""Provider-neutral Creative IR and quality contracts."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class ProjectStatus(str, Enum):
    CLARIFYING = "clarifying"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    PLANNED = "planned"
    GENERATING = "generating"
    JUDGING = "judging"
    REPAIRING = "repairing"
    AWAITING_HUMAN = "awaiting_human"
    ASSEMBLING = "assembling"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Explicit domain transitions keep framework handlers from accidentally
# skipping a human gate or reopening a terminal project.  The orchestrator
# still records every transition in the EventStore for auditability.
PROJECT_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.CLARIFYING: frozenset({ProjectStatus.AWAITING_PLAN_APPROVAL, ProjectStatus.AWAITING_HUMAN, ProjectStatus.CANCELLED, ProjectStatus.FAILED}),
    ProjectStatus.AWAITING_PLAN_APPROVAL: frozenset({ProjectStatus.CLARIFYING, ProjectStatus.PLANNED, ProjectStatus.AWAITING_HUMAN, ProjectStatus.CANCELLED, ProjectStatus.FAILED}),
    ProjectStatus.PLANNED: frozenset({ProjectStatus.GENERATING, ProjectStatus.AWAITING_PLAN_APPROVAL, ProjectStatus.AWAITING_HUMAN, ProjectStatus.CANCELLED, ProjectStatus.FAILED}),
    ProjectStatus.GENERATING: frozenset({ProjectStatus.JUDGING, ProjectStatus.REPAIRING, ProjectStatus.AWAITING_HUMAN, ProjectStatus.ASSEMBLING, ProjectStatus.FAILED, ProjectStatus.CANCELLED}),
    ProjectStatus.JUDGING: frozenset({ProjectStatus.GENERATING, ProjectStatus.REPAIRING, ProjectStatus.ASSEMBLING, ProjectStatus.AWAITING_HUMAN, ProjectStatus.FAILED, ProjectStatus.CANCELLED}),
    ProjectStatus.REPAIRING: frozenset({ProjectStatus.GENERATING, ProjectStatus.AWAITING_HUMAN, ProjectStatus.FAILED, ProjectStatus.CANCELLED}),
    ProjectStatus.AWAITING_HUMAN: frozenset({ProjectStatus.CLARIFYING, ProjectStatus.AWAITING_PLAN_APPROVAL, ProjectStatus.GENERATING, ProjectStatus.ASSEMBLING, ProjectStatus.PLANNED, ProjectStatus.DELIVERED, ProjectStatus.CANCELLED, ProjectStatus.FAILED}),
    ProjectStatus.ASSEMBLING: frozenset({ProjectStatus.AWAITING_HUMAN, ProjectStatus.DELIVERED, ProjectStatus.FAILED, ProjectStatus.CANCELLED}),
    ProjectStatus.DELIVERED: frozenset(),
    ProjectStatus.FAILED: frozenset({ProjectStatus.GENERATING, ProjectStatus.AWAITING_HUMAN, ProjectStatus.CANCELLED}),
    ProjectStatus.CANCELLED: frozenset(),
}


class CriterionCategory(str, Enum):
    SUBJECT = "subject"
    ACTION = "action"
    CAMERA = "camera"
    CONTINUITY = "continuity"
    STYLE = "style"
    AUDIO = "audio"
    TECHNICAL = "technical"
    SAFETY = "safety"


class Severity(str, Enum):
    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class AcceptanceMode(str, Enum):
    AUTO = "auto"
    LOW = "low"
    STANDARD = "standard"
    STRICT = "strict"
    CUSTOM = "custom"
    NONE = "none"


class RepairKind(str, Enum):
    PROMPT = "prompt"
    PARAMETERS = "parameters"
    REFERENCE = "reference"
    PROVIDER = "provider"
    SPLIT_SHOT = "split_shot"
    HUMAN = "human"


class AssetKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    EVIDENCE = "evidence"
    OTHER = "other"


class AudioCueKind(str, Enum):
    DIALOGUE = "dialogue"
    NARRATION = "narration"
    MUSIC = "music"
    SFX = "sfx"
    SUBTITLE = "subtitle"


@dataclass(slots=True)
class CreativeBrief:
    request: str
    title: str = "Untitled project"
    target_audience: str | None = None
    duration_seconds: float = 150.0
    aspect_ratio: str = "16:9"
    fps: int = 24
    style: str | None = None
    language: str = "zh-CN"
    content_constraints: list[str] = field(default_factory=list)
    audio_required: bool = True
    shot_duration_seconds: float = 15.0
    max_shots: int = 10
    budget_usd: float = 75.0
    parallelism_mode: str = "auto"
    parallelism: int | None = None
    resolution_mode: str = "auto"
    resolution_width: int | None = None
    resolution_height: int | None = None
    acceptance_mode: str = AcceptanceMode.STANDARD.value
    acceptance_custom: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not isinstance(self.request, str) or not self.request.strip():
            errors.append("request must not be empty")
        if not isinstance(self.title, str) or not self.title.strip():
            errors.append("title must not be empty")
        if not isinstance(self.duration_seconds, (int, float)) or isinstance(self.duration_seconds, bool) or not math.isfinite(float(self.duration_seconds)) or self.duration_seconds <= 0:
            errors.append("duration_seconds must be positive")
        if not isinstance(self.shot_duration_seconds, (int, float)) or isinstance(self.shot_duration_seconds, bool) or not math.isfinite(float(self.shot_duration_seconds)) or self.shot_duration_seconds <= 0:
            errors.append("shot_duration_seconds must be positive")
        if not isinstance(self.max_shots, int) or isinstance(self.max_shots, bool) or self.max_shots < 1:
            errors.append("max_shots must be at least 1")
        if not isinstance(self.fps, int) or isinstance(self.fps, bool) or self.fps < 1:
            errors.append("fps must be positive")
        if not isinstance(self.budget_usd, (int, float)) or isinstance(self.budget_usd, bool) or not math.isfinite(float(self.budget_usd)) or self.budget_usd < 0:
            errors.append("budget_usd cannot be negative")
        if not isinstance(self.audio_required, bool):
            errors.append("audio_required must be a boolean")
        if not isinstance(self.aspect_ratio, str) or not re.fullmatch(r"[1-9]\d*:[1-9]\d*", self.aspect_ratio.strip()):
            errors.append("aspect_ratio must use the form WIDTH:HEIGHT")
        if self.parallelism_mode not in {"auto", "preset", "custom"}:
            errors.append("parallelism_mode must be auto, preset or custom")
        if self.parallelism is not None and (isinstance(self.parallelism, bool) or not isinstance(self.parallelism, int) or self.parallelism < 1):
            errors.append("parallelism must be a positive integer when provided")
        if self.parallelism_mode == "preset" and self.parallelism not in {1, 2, 4, 8}:
            errors.append("preset parallelism must be one of 1, 2, 4 or 8")
        if self.parallelism_mode == "custom" and self.parallelism is None:
            errors.append("custom parallelism requires a value")
        if self.resolution_mode not in {"auto", "preset", "custom"}:
            errors.append("resolution_mode must be auto, preset or custom")
        for name, value in (("resolution_width", self.resolution_width), ("resolution_height", self.resolution_height)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                errors.append(f"{name} must be a positive integer when provided")
        if self.resolution_mode == "custom" and (self.resolution_width is None or self.resolution_height is None):
            errors.append("custom resolution requires width and height")
        if self.resolution_mode == "preset" and (self.resolution_width, self.resolution_height) not in {(1280, 720), (1920, 1080), (3840, 2160)}:
            errors.append("preset resolution must be 1280x720, 1920x1080 or 3840x2160")
        if self.acceptance_mode not in {item.value for item in AcceptanceMode}:
            errors.append("acceptance_mode is invalid")
        if self.acceptance_mode == AcceptanceMode.CUSTOM.value and not (self.acceptance_custom or "").strip():
            errors.append("custom acceptance mode requires acceptance_custom")
        # A plan cannot satisfy a requested duration when the configured shot
        # ceiling is too small. Fail at the brief boundary instead of silently
        # producing a shorter film.
        duration_valid = (
            isinstance(self.duration_seconds, (int, float))
            and not isinstance(self.duration_seconds, bool)
            and math.isfinite(float(self.duration_seconds))
            and self.duration_seconds > 0
        )
        shot_duration_valid = (
            isinstance(self.shot_duration_seconds, (int, float))
            and not isinstance(self.shot_duration_seconds, bool)
            and math.isfinite(float(self.shot_duration_seconds))
            and self.shot_duration_seconds > 0
        )
        max_shots_valid = isinstance(self.max_shots, int) and not isinstance(self.max_shots, bool) and self.max_shots >= 1
        if duration_valid and shot_duration_valid and max_shots_valid and self.max_shots * float(self.shot_duration_seconds) < float(self.duration_seconds):
            errors.append("max_shots and shot_duration_seconds must cover duration_seconds")
        return errors


@dataclass(slots=True)
class ClarificationTurn:
    question: str
    answer: str | None = None
    required: bool = True
    source: str = "agent"
    confidence: float = 0.0
    confirmed: bool = False
    options: list[ClarificationOption] = field(default_factory=list)
    skipped: bool = False
    id: str = field(default_factory=lambda: new_id("clar"))


@dataclass(slots=True)
class ClarificationOption:
    label: str
    value: str
    explanation: str
    id: str = field(default_factory=lambda: new_id("option"))


@dataclass(slots=True)
class CharacterBible:
    name: str
    identity: str
    visual_traits: list[str] = field(default_factory=list)
    wardrobe: list[str] = field(default_factory=list)
    voice_traits: list[str] = field(default_factory=list)
    reference_asset_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("char"))


@dataclass(slots=True)
class StyleBible:
    name: str
    description: str
    palette: list[str] = field(default_factory=list)
    lighting: str | None = None
    camera_language: str | None = None
    id: str = field(default_factory=lambda: new_id("style"))


@dataclass(slots=True)
class LocationBible:
    name: str
    description: str
    geography: str | None = None
    continuity_notes: list[str] = field(default_factory=list)
    reference_asset_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("loc"))


@dataclass(slots=True)
class ReferenceAsset:
    kind: AssetKind
    uri: str
    sha256: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("asset"))

    def content_key(self) -> str:
        return self.sha256 or hashlib.sha256(self.uri.encode()).hexdigest()


@dataclass(slots=True)
class AcceptanceCriterion:
    statement: str
    category: CriterionCategory
    severity: Severity = Severity.BLOCKING
    evidence_types: list[str] = field(default_factory=lambda: ["frame"])
    threshold: str | None = None
    blocking: bool = True
    id: str = field(default_factory=lambda: new_id("criterion"))


@dataclass(slots=True)
class PromptBundle:
    positive: str
    negative: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    reference_asset_ids: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    version: int = 1
    id: str = field(default_factory=lambda: new_id("prompt"))


@dataclass(slots=True)
class AudioCue:
    """A timed, provider-neutral audio instruction in the project timeline."""
    kind: str
    text: str = ""
    start_seconds: float = 0.0
    duration_seconds: float | None = None
    voice: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("cue"))


@dataclass(slots=True)
class Scene:
    title: str
    summary: str
    location_id: str | None = None
    time_of_day: str | None = None
    shot_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("scene"))


@dataclass(slots=True)
class Shot:
    sequence: int
    scene_id: str
    title: str
    description: str
    duration_seconds: float = 15.0
    character_ids: list[str] = field(default_factory=list)
    location_id: str | None = None
    previous_shot_id: str | None = None
    next_shot_id: str | None = None
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    prompt_bundle: PromptBundle | None = None
    depends_on_shot_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("shot"))

    def is_ready_to_generate(self) -> bool:
        return bool(
            isinstance(self.acceptance_criteria, list)
            and self.acceptance_criteria
            and all(
                isinstance(criterion, AcceptanceCriterion)
                and isinstance(criterion.statement, str)
                and bool(criterion.statement.strip())
                and isinstance(criterion.evidence_types, list)
                and bool(criterion.evidence_types)
                for criterion in self.acceptance_criteria
            )
            and isinstance(self.prompt_bundle, PromptBundle)
            and isinstance(self.prompt_bundle.positive, str)
            and bool(self.prompt_bundle.positive.strip())
        )


@dataclass(slots=True)
class PlanVersion:
    version: int
    brief: CreativeBrief
    scenes: list[Scene]
    shots: list[Shot]
    characters: list[CharacterBible] = field(default_factory=list)
    locations: list[LocationBible] = field(default_factory=list)
    style_bible: StyleBible | None = None
    reference_assets: list[ReferenceAsset] = field(default_factory=list)
    audio_cues: list[AudioCue] = field(default_factory=list)
    status: str = "draft"
    approved_by: str | None = None
    resolved_settings: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("plan"))

    def validate(self) -> list[str]:
        errors = self.brief.validate() if isinstance(self.brief, CreativeBrief) else ["plan has a malformed brief"]
        if not isinstance(self.scenes, list) or not all(isinstance(scene, Scene) for scene in self.scenes):
            errors.append("plan contains malformed scenes")
            return errors
        if not isinstance(self.shots, list) or not all(isinstance(shot, Shot) for shot in self.shots):
            errors.append("plan contains malformed shots")
            return errors
        if not isinstance(self.reference_assets, list) or not all(isinstance(asset, ReferenceAsset) for asset in self.reference_assets):
            errors.append("plan contains malformed reference assets")
            return errors
        if not isinstance(self.characters, list) or not all(isinstance(character, CharacterBible) for character in self.characters):
            errors.append("plan contains malformed characters")
            return errors
        if not isinstance(self.locations, list) or not all(isinstance(location, LocationBible) for location in self.locations):
            errors.append("plan contains malformed locations")
            return errors
        shot_ids = {shot.id for shot in self.shots}
        scene_ids = {scene.id for scene in self.scenes}
        reference_ids = {asset.id for asset in self.reference_assets}
        character_ids = {character.id for character in self.characters}
        location_ids = {location.id for location in self.locations}
        criterion_ids: set[str] = set()
        prompt_ids: set[str] = set()
        if len(shot_ids) != len(self.shots):
            errors.append("plan contains duplicate shot IDs")
        if len(scene_ids) != len(self.scenes):
            errors.append("plan contains duplicate scene IDs")
        if len(reference_ids) != len(self.reference_assets):
            errors.append("plan contains duplicate reference asset IDs")
        if len(character_ids) != len(self.characters):
            errors.append("plan contains duplicate character IDs")
        if len(location_ids) != len(self.locations):
            errors.append("plan contains duplicate location IDs")
        for character in self.characters:
            asset_ids = character.reference_asset_ids if isinstance(character.reference_asset_ids, list) else []
            if not isinstance(character.reference_asset_ids, list):
                errors.append(f"character {character.id} has malformed reference asset IDs")
            missing = [asset_id for asset_id in asset_ids if asset_id not in reference_ids]
            if missing:
                errors.append(f"character {character.id} references missing assets: {missing}")
        for location in self.locations:
            asset_ids = location.reference_asset_ids if isinstance(location.reference_asset_ids, list) else []
            if not isinstance(location.reference_asset_ids, list):
                errors.append(f"location {location.id} has malformed reference asset IDs")
            missing = [asset_id for asset_id in asset_ids if asset_id not in reference_ids]
            if missing:
                errors.append(f"location {location.id} references missing assets: {missing}")
        if not self.shots:
            errors.append("plan must contain at least one shot")
        sequences = [shot.sequence for shot in self.shots if isinstance(shot.sequence, int) and not isinstance(shot.sequence, bool)]
        if len(sequences) != len(self.shots):
            errors.append("plan contains an invalid shot sequence number")
        elif len(set(sequences)) != len(sequences):
            errors.append("plan contains duplicate shot sequence numbers")
        elif sequences and sorted(sequences) != list(range(1, len(sequences) + 1)):
            errors.append("plan shot sequences must be contiguous and start at 1")
        scene_membership: dict[str, str] = {}
        for scene in self.scenes:
            if scene.location_id is not None and scene.location_id not in location_ids:
                errors.append(f"scene {scene.id} references missing location")
            scene_shot_ids = scene.shot_ids if isinstance(scene.shot_ids, list) else []
            if not isinstance(scene.shot_ids, list):
                errors.append(f"scene {scene.id} has malformed shot IDs")
            for scene_shot_id in scene_shot_ids:
                if scene_shot_id not in shot_ids:
                    errors.append(f"scene {scene.id} references missing shot: {scene_shot_id}")
                elif scene_shot_id in scene_membership:
                    errors.append(f"shot {scene_shot_id} belongs to multiple scenes")
                else:
                    scene_membership[scene_shot_id] = scene.id
        missing_scene_membership = shot_ids - set(scene_membership)
        for missing_shot_id in sorted(missing_scene_membership):
            errors.append(f"shot {missing_shot_id} is not listed in any scene")
        for shot in self.shots:
            if not isinstance(shot.sequence, int) or isinstance(shot.sequence, bool) or shot.sequence < 1:
                errors.append(f"shot {shot.id} sequence must be a positive integer")
            if shot.scene_id not in scene_ids:
                errors.append(f"shot {shot.id} references missing scene")
            elif scene_membership.get(shot.id) not in (None, shot.scene_id):
                errors.append(f"shot {shot.id} scene membership disagrees with scene_id")
            if (
                isinstance(shot.duration_seconds, bool)
                or not isinstance(shot.duration_seconds, (int, float))
                or not math.isfinite(float(shot.duration_seconds))
                or float(shot.duration_seconds) <= 0
            ):
                errors.append(f"shot {shot.id} duration_seconds must be positive")
            criteria = shot.acceptance_criteria if isinstance(shot.acceptance_criteria, list) else []
            if not isinstance(shot.acceptance_criteria, list):
                errors.append(f"shot {shot.id} has malformed acceptance criteria")
            if not criteria:
                errors.append(f"shot {shot.id} has no acceptance criteria")
            if not shot.prompt_bundle:
                errors.append(f"shot {shot.id} has no prompt bundle")
            elif not isinstance(shot.prompt_bundle, PromptBundle):
                errors.append(f"shot {shot.id} has a malformed prompt bundle")
            else:
                reference_asset_ids = shot.prompt_bundle.reference_asset_ids if isinstance(shot.prompt_bundle.reference_asset_ids, list) else []
                if not isinstance(shot.prompt_bundle.reference_asset_ids, list):
                    errors.append(f"shot {shot.id} has malformed reference asset IDs")
                missing_refs = [ref for ref in reference_asset_ids if ref not in reference_ids]
                if missing_refs:
                    errors.append(f"shot {shot.id} references missing assets: {missing_refs}")
                if not isinstance(shot.prompt_bundle.positive, str) or not shot.prompt_bundle.positive.strip():
                    errors.append(f"shot {shot.id} has an empty prompt")
            if shot.prompt_bundle is not None and isinstance(shot.prompt_bundle, PromptBundle):
                if shot.prompt_bundle.id in prompt_ids:
                    errors.append(f"plan contains duplicate prompt IDs: {shot.prompt_bundle.id}")
                prompt_ids.add(shot.prompt_bundle.id)
            malformed_statements = [
                criterion
                for criterion in criteria
                if not isinstance(criterion, AcceptanceCriterion) or not isinstance(criterion.statement, str)
            ]
            if malformed_statements:
                errors.append(f"shot {shot.id} has a malformed acceptance criterion")
            elif any(not criterion.statement.strip() for criterion in criteria):
                errors.append(f"shot {shot.id} has an empty acceptance criterion")
            if any(
                not isinstance(criterion, AcceptanceCriterion)
                or not isinstance(criterion.evidence_types, list)
                or not criterion.evidence_types
                or any(not isinstance(kind, str) or not kind.strip() for kind in criterion.evidence_types)
                for criterion in criteria
            ):
                errors.append(f"shot {shot.id} has malformed criterion evidence types")
            for criterion in criteria:
                if not isinstance(criterion, AcceptanceCriterion):
                    continue
                if not isinstance(criterion.category, CriterionCategory):
                    errors.append(f"shot {shot.id} has an invalid criterion category")
                if not isinstance(criterion.severity, Severity):
                    errors.append(f"shot {shot.id} has an invalid criterion severity")
                if not isinstance(criterion.blocking, bool):
                    errors.append(f"shot {shot.id} has an invalid criterion blocking flag")
                if criterion.id in criterion_ids:
                    errors.append(f"plan contains duplicate criterion IDs: {criterion.id}")
                criterion_ids.add(criterion.id)
            character_ids_for_shot = shot.character_ids if isinstance(shot.character_ids, list) else []
            if not isinstance(shot.character_ids, list):
                errors.append(f"shot {shot.id} has malformed character IDs")
            missing_characters = [character_id for character_id in character_ids_for_shot if character_id not in character_ids]
            if missing_characters:
                errors.append(f"shot {shot.id} references missing characters: {missing_characters}")
            if shot.location_id is not None and shot.location_id not in location_ids:
                errors.append(f"shot {shot.id} references missing location")
            dependencies = shot.depends_on_shot_ids if isinstance(shot.depends_on_shot_ids, list) else []
            if not isinstance(shot.depends_on_shot_ids, list):
                errors.append(f"shot {shot.id} has malformed dependencies")
            missing = [dep for dep in dependencies if dep not in shot_ids]
            if missing:
                errors.append(f"shot {shot.id} has missing dependencies: {missing}")
            if shot.id in dependencies:
                errors.append(f"shot {shot.id} cannot depend on itself")
            for relation, related_id in (("previous_shot_id", shot.previous_shot_id), ("next_shot_id", shot.next_shot_id)):
                if related_id is not None and related_id not in shot_ids:
                    errors.append(f"shot {shot.id} references missing {relation}: {related_id}")
            if shot.previous_shot_id == shot.id or shot.next_shot_id == shot.id:
                errors.append(f"shot {shot.id} cannot link to itself")
        by_id = {shot.id: shot for shot in self.shots}
        for shot in self.shots:
            dependencies = shot.depends_on_shot_ids if isinstance(shot.depends_on_shot_ids, list) else []
            for dependency_id in dependencies:
                dependency = by_id.get(dependency_id)
                if dependency is not None and dependency.sequence >= shot.sequence:
                    errors.append(f"shot {shot.id} dependencies must point to an earlier sequence")
        # Dependencies form a DAG in the execution graph. Sequence ordering
        # catches forward edges; DFS catches cycles in custom plans.
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(shot_id: str) -> None:
            if shot_id in visited or shot_id not in by_id:
                return
            if shot_id in visiting:
                errors.append(f"shot dependency cycle includes {shot_id}")
                return
            visiting.add(shot_id)
            dependencies = by_id[shot_id].depends_on_shot_ids if isinstance(by_id[shot_id].depends_on_shot_ids, list) else []
            for dependency_id in dependencies:
                visit(dependency_id)
            visiting.remove(shot_id)
            visited.add(shot_id)

        for shot in self.shots:
            visit(shot.id)
        for shot in self.shots:
            if shot.previous_shot_id:
                previous = by_id.get(shot.previous_shot_id)
                if previous is not None:
                    if previous.sequence >= shot.sequence:
                        errors.append(f"shot {shot.id} previous_shot_id must point to an earlier sequence")
                    if previous.next_shot_id != shot.id:
                        errors.append(f"shot {shot.id} previous/next links are inconsistent")
            if shot.next_shot_id:
                following = by_id.get(shot.next_shot_id)
                if following is not None:
                    if following.sequence <= shot.sequence:
                        errors.append(f"shot {shot.id} next_shot_id must point to a later sequence")
                    if following.previous_shot_id != shot.id:
                        errors.append(f"shot {shot.id} next/previous links are inconsistent")
        return errors


@dataclass(slots=True)
class ArtifactRef:
    kind: AssetKind
    uri: str
    sha256: str | None = None
    mime_type: str | None = None
    duration_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("artifact"))


@dataclass(slots=True)
class CostRecord:
    amount_usd: float
    currency: str = "USD"
    provider: str | None = None
    model: str | None = None
    estimated: bool = False
    id: str = field(default_factory=lambda: new_id("cost"))


@dataclass(slots=True)
class ProviderJob:
    provider: str
    external_id: str
    status: str = "queued"
    idempotency_key: str | None = None
    submitted_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderError:
    code: str
    message: str
    retryable: bool = False
    provider: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Evidence:
    kind: str
    uri: str
    timestamp_seconds: float | None = None
    excerpt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CriterionResult:
    criterion_id: str
    verdict: Verdict
    evidence: list[Evidence] = field(default_factory=list)
    failure_code: str | None = None
    reason: str | None = None
    confidence: float = 0.0
    repair_suggestions: list[RepairKind] = field(default_factory=list)
    skipped: bool = False

    def fail_closed(self) -> CriterionResult:
        evidence_items = self.evidence if isinstance(self.evidence, (list, tuple)) else []
        valid_evidence: list[Evidence] = []
        for item in evidence_items:
            if (
                not isinstance(item, Evidence)
                or not isinstance(item.kind, str)
                or not item.kind.strip()
                or not isinstance(item.uri, str)
                or not item.uri.strip()
                or not isinstance(item.metadata, Mapping)
                or not isinstance(item.excerpt, (str, type(None)))
            ):
                continue
            timestamp = item.timestamp_seconds
            if timestamp is not None:
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                    continue
                try:
                    if not math.isfinite(float(timestamp)) or timestamp < 0:
                        continue
                except (OverflowError, TypeError, ValueError):
                    continue
            valid_evidence.append(item)
        if not valid_evidence:
            if self.skipped:
                return replace(self, evidence=[])
            return CriterionResult(
                criterion_id=self.criterion_id,
                verdict=Verdict.FAIL,
                evidence=[],
                failure_code="missing_evidence",
                reason=self.reason or "Judge did not provide valid evidence",
                # Missing evidence is terminal for this criterion. Keep the
                # persisted confidence finite even when the judge returned
                # NaN/Infinity alongside the missing evidence.
                confidence=0.0,
                repair_suggestions=self.repair_suggestions or [RepairKind.HUMAN],
            )
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            confidence = None
        else:
            try:
                confidence = float(self.confidence)
            except (OverflowError, TypeError, ValueError):
                confidence = None
        if confidence is None or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return CriterionResult(
                criterion_id=self.criterion_id,
                verdict=Verdict.FAIL,
                evidence=valid_evidence,
                failure_code="invalid_confidence",
                reason="Judge confidence must be a finite number between 0 and 1",
                confidence=0.0,
                repair_suggestions=self.repair_suggestions or [RepairKind.HUMAN],
            )
        if self.verdict not in (Verdict.PASS, Verdict.FAIL):
            return CriterionResult(
                criterion_id=self.criterion_id,
                verdict=Verdict.FAIL,
                evidence=valid_evidence,
                failure_code=self.failure_code or "missing_evidence",
                reason=self.reason or "Judge did not provide valid evidence",
                confidence=self.confidence,
                repair_suggestions=self.repair_suggestions or [RepairKind.HUMAN],
                skipped=self.skipped,
            )
        if valid_evidence is not self.evidence:
            return CriterionResult(
                criterion_id=self.criterion_id,
                verdict=self.verdict,
                evidence=valid_evidence,
                failure_code=self.failure_code,
                reason=self.reason,
                confidence=confidence,
                repair_suggestions=self.repair_suggestions,
            )
        return self


@dataclass(slots=True)
class JudgeResult:
    shot_id: str
    verdict: Verdict
    criterion_results: list[CriterionResult]
    judge_provider: str
    judge_model: str | None = None
    evaluated_at: datetime = field(default_factory=utc_now)
    summary: str = ""

    @property
    def failed_criteria(self) -> list[CriterionResult]:
        return [r for r in self.criterion_results if r.verdict == Verdict.FAIL]

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS and not self.failed_criteria


@dataclass(slots=True)
class Diagnosis:
    shot_id: str
    failure_codes: list[str]
    root_causes: list[str]
    recommended_repairs: list[RepairKind]
    confidence: float = 0.0
    id: str = field(default_factory=lambda: new_id("diagnosis"))


@dataclass(slots=True)
class RepairAction:
    shot_id: str
    kind: RepairKind
    changes: dict[str, Any]
    reason: str
    estimated_cost_usd: float = 0.0
    attempt_number: int = 1
    id: str = field(default_factory=lambda: new_id("repair"))


@dataclass(slots=True)
class Attempt:
    shot_id: str
    number: int
    prompt_bundle: PromptBundle
    provider_job: ProviderJob | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)
    cost: CostRecord | None = None
    judge_result: JudgeResult | None = None
    diagnosis: Diagnosis | None = None
    repair_action: RepairAction | None = None
    status: str = "created"
    id: str = field(default_factory=lambda: new_id("attempt"))


@dataclass(slots=True)
class Project:
    name: str
    brief: CreativeBrief
    status: ProjectStatus = ProjectStatus.CLARIFYING
    clarification_turns: list[ClarificationTurn] = field(default_factory=list)
    plans: list[PlanVersion] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    total_cost_usd: float = 0.0
    id: str = field(default_factory=lambda: new_id("project"))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    # Monotonic snapshot version assigned by the EventStore.  Keeping this at
    # the end preserves the positional constructor shape used by early
    # integrations while allowing storage adapters to perform CAS writes.
    revision: int = 0
    # Logical project lineage. Delivered snapshots remain immutable while a
    # new version receives its own project id and can run independently.
    root_project_id: str | None = None
    parent_project_id: str | None = None
    version: int = 1

    @property
    def active_plan(self) -> PlanVersion | None:
        for plan in reversed(self.plans):
            if getattr(plan, "status", "draft") != "obsolete":
                return plan
        return None

    def add_cost(self, cost: CostRecord) -> None:
        self.total_cost_usd += cost.amount_usd
        self.updated_at = utc_now()

    def over_budget(self) -> bool:
        return self.total_cost_usd >= self.brief.budget_usd

    def transition(self, status: ProjectStatus, *, force: bool = False) -> None:
        """Move through an explicit domain transition.

        ``force`` is reserved for an operator-approved re-open of a delivered
        project before a manual retry; ordinary callers should leave it false.
        """
        if status == self.status:
            return
        if not force and status not in PROJECT_TRANSITIONS.get(self.status, frozenset()):
            raise ValueError(f"Illegal project transition: {self.status.value} -> {status.value}")
        self.status = status
        self.updated_at = utc_now()


def as_jsonable(value: Any) -> Any:
    """Serialize enums, dataclasses and datetimes for event/log storage."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {k: as_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): as_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [as_jsonable(v) for v in value]
    return value


def stable_hash(value: Any) -> str:
    payload = json.dumps(as_jsonable(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
