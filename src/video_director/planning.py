"""Clarification and planning agents for the Creative IR."""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import replace

from .schemas import (
    AcceptanceCriterion,
    AssetKind,
    AudioCue,
    CharacterBible,
    ClarificationTurn,
    CreativeBrief,
    CriterionCategory,
    LocationBible,
    PlanVersion,
    PromptBundle,
    ReferenceAsset,
    Scene,
    Severity,
    Shot,
    StyleBible,
)


class ClarificationAgent:
    """Find high-impact missing facts before a plan is allowed to run."""

    QUESTIONS = (
        ("characters", "Who are the main characters, and what must remain consistent about them?"),
        ("setting", "Where and when does the story take place, including the key visual mood?"),
        ("arc", "What must change from the opening to the ending of the short film?"),
        ("audio", "What language, dialogue or narration, music mood, and subtitle requirements should be used?"),
    )

    def inspect(self, brief: CreativeBrief) -> list[ClarificationTurn]:
        text = brief.request.lower()
        turns: list[ClarificationTurn] = []
        for key, question in self.QUESTIONS:
            markers = {
                "characters": ("character", "protagonist", "hero", "girl", "boy", "woman", "man", "人物", "主角"),
                "setting": ("in ", "at ", "city", "forest", "room", "街", "城市", "森林", "场景"),
                "arc": ("then", "finally", "journey", "discovers", "救", "最后", "然后", "故事"),
                "audio": ("dialogue", "voice", "narration", "music", "sound", "对白", "旁白", "音乐", "音效"),
            }[key]
            if not any(marker in text for marker in markers):
                turns.append(ClarificationTurn(question=question, required=True))
        return turns

    def apply_answers(self, turns: list[ClarificationTurn], answers: dict[str, str]) -> list[ClarificationTurn]:
        if not isinstance(answers, dict):
            raise TypeError("clarification answers must be an object")
        invalid = [key for key, value in answers.items() if not isinstance(key, str) or not isinstance(value, str)]
        if invalid:
            raise TypeError("clarification answers must map strings to strings")
        updated: list[ClarificationTurn] = []
        for turn in turns:
            answer = answers.get(turn.id) or answers.get(turn.question)
            if answer is None:
                updated.append(turn)
            else:
                updated.append(replace(turn, answer=answer, confirmed=bool(answer.strip()), confidence=1.0))
        return updated

    def unresolved(self, turns: Iterable[ClarificationTurn]) -> list[ClarificationTurn]:
        return [turn for turn in turns if turn.required and not turn.confirmed]


class PlanAgent:
    """Create a conservative, inspectable plan without binding to an LLM."""

    def create_plan(
        self,
        brief: CreativeBrief,
        *,
        version: int = 1,
        clarifications: Iterable[ClarificationTurn] = (),
    ) -> PlanVersion:
        errors = brief.validate()
        if errors:
            raise ValueError("Invalid creative brief: " + "; ".join(errors))
        answers = " ".join(turn.answer or "" for turn in clarifications)
        source = f"{brief.request} {answers}".strip()
        character_text = self._extract_character(source)
        setting_text = self._extract_setting(source)
        arc_text = self._extract_arc(source)
        character = CharacterBible(
            name=character_text or "Lead",
            identity=character_text or "The central character of the story",
            visual_traits=["consistent face and silhouette", "age and wardrobe remain unchanged"],
            wardrobe=["wardrobe from the approved reference"],
            voice_traits=["same voice identity across shots"],
        )
        location = LocationBible(
            name=setting_text or "Primary location",
            description=setting_text or "A coherent story location",
            continuity_notes=["preserve geography, weather, light direction and hero props"],
        )
        style = StyleBible(
            name=brief.style or "Cinematic continuity style",
            description=brief.style or "Cinematic, coherent visual language with natural motion",
            palette=["deep blue", "warm practical light", "neutral skin tones"],
            lighting="motivated soft key with consistent direction",
            camera_language="deliberate coverage; preserve screen direction",
        )
        reference = ReferenceAsset(
            kind=AssetKind.IMAGE,
            uri=f"project://reference/{brief.title.lower().replace(' ', '-')}/character-location",
            metadata={"role": "character_and_location_source_of_truth"},
        )
        # A plan must cover the requested duration.  ``round`` can silently
        # under-provision a project (for example 16s / 15s becomes one shot),
        # so use a ceiling and let the explicit max_shots budget cap it.
        shot_count = min(brief.max_shots, max(1, math.ceil(brief.duration_seconds / brief.shot_duration_seconds)))
        scenes: list[Scene] = []
        shots: list[Shot] = []
        for index in range(shot_count):
            scene_index = index // 3
            while len(scenes) <= scene_index:
                scenes.append(Scene(
                    title=f"Scene {len(scenes) + 1}",
                    summary=arc_text or "A beat in the protagonist's story",
                    location_id=location.id,
                    time_of_day="continuous",
                ))
            scene = scenes[scene_index]
            beat = self._beat(index, shot_count, arc_text)
            description = f"{beat}; {character.identity}; in {location.description}."
            criteria = self._criteria(brief, character, location, style, beat)
            prompt = PromptBundle(
                positive=(
                    f"{description} Cinematic continuity, {style.description}, "
                    f"camera language: {style.camera_language}. Maintain approved character and location references."
                ),
                negative="identity drift, wardrobe change, extra limbs, broken geography, flicker, unreadable text, clipping",
                parameters={"duration_seconds": brief.shot_duration_seconds, "fps": brief.fps, "aspect_ratio": brief.aspect_ratio, "attempt": 1},
                reference_asset_ids=[reference.id],
                model="auto",
            )
            previous = shots[-1].id if shots else None
            shot = Shot(
                sequence=index + 1,
                scene_id=scene.id,
                title=f"Shot {index + 1}: {beat.split(';')[0]}",
                description=description,
                duration_seconds=brief.shot_duration_seconds,
                character_ids=[character.id],
                location_id=location.id,
                previous_shot_id=previous,
                acceptance_criteria=criteria,
                prompt_bundle=prompt,
                depends_on_shot_ids=[previous] if previous else [],
            )
            if shots:
                shots[-1].next_shot_id = shot.id
            scene.shot_ids.append(shot.id)
            shots.append(shot)
        cues = self._audio_cues(brief, source, shot_count)
        plan = PlanVersion(version, brief, scenes, shots, [character], [location], style, [reference], cues)
        validation = plan.validate()
        if validation:
            raise ValueError("Generated invalid plan: " + "; ".join(validation))
        return plan

    @staticmethod
    def _audio_cues(brief: CreativeBrief, source: str, shot_count: int) -> list[AudioCue]:
        if not brief.audio_required:
            return []
        duration = brief.duration_seconds
        return [
            AudioCue("narration", source, 0.0, duration, voice="narrator", parameters={"language": brief.language}),
            AudioCue("music", "continuous cinematic underscore", 0.0, duration, parameters={"mix_db": -18, "loop": True}),
            AudioCue("sfx", "rain and letter handling accents", 0.0, duration, parameters={"mix_db": -24}),
            AudioCue("subtitle", source, 0.0, duration, parameters={"language": brief.language, "format": "srt", "shot_count": shot_count}),
        ]

    def _extract_character(self, source: str) -> str | None:
        match = re.search(r"(?:named|called|主角是|人物是)\s*([A-Za-z][A-Za-z0-9_-]{1,24}|[\u4e00-\u9fff]{2,8})", source, re.IGNORECASE)
        return match.group(1) if match else None

    def _extract_setting(self, source: str) -> str | None:
        match = re.search(r"(?:in|at|inside|在|于)\s+([^,.!?，。！？]{2,40})", source, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def _extract_arc(self, source: str) -> str | None:
        return source[:180] if source else None

    def _beat(self, index: int, total: int, arc: str | None) -> str:
        if total == 1:
            phase = "the complete story beat resolves"
        elif index == 0:
            phase = "the opening establishes the character and place"
        elif index == total - 1:
            phase = "the ending resolves the central change"
        elif index < total / 2:
            phase = "the inciting action creates a clear objective"
        else:
            phase = "the character acts on the consequence and moves toward resolution"
        return f"{phase}; story intent: {arc or 'a focused cinematic arc'}"

    def _criteria(self, brief: CreativeBrief, character: CharacterBible, location: LocationBible, style: StyleBible, beat: str) -> list[AcceptanceCriterion]:
        criteria = [
            AcceptanceCriterion(f"The approved subject {character.name} is visible and recognizable throughout the shot.", CriterionCategory.SUBJECT, evidence_types=["frame", "track"]),
            AcceptanceCriterion(f"The action clearly communicates: {beat}.", CriterionCategory.ACTION, evidence_types=["frame", "temporal"]),
            AcceptanceCriterion("Camera composition and motion are intentional, stable, and preserve screen direction.", CriterionCategory.CAMERA, evidence_types=["frame", "temporal"]),
            AcceptanceCriterion(f"The shot preserves the character bible and the location bible: {location.name}.", CriterionCategory.CONTINUITY, evidence_types=["frame", "embedding"]),
            AcceptanceCriterion(f"The visual language matches the style bible: {style.name}.", CriterionCategory.STYLE, evidence_types=["frame", "color"]),
            AcceptanceCriterion("The rendered artifact meets duration, aspect ratio, frame rate and safety constraints.", CriterionCategory.TECHNICAL, evidence_types=["metadata"]),
        ]
        if brief.audio_required:
            criteria.extend([
                AcceptanceCriterion("Dialogue, narration, music and effects are present only where planned and remain intelligible.", CriterionCategory.AUDIO, evidence_types=["audio", "transcript"]),
                AcceptanceCriterion(f"Subtitles and dialogue timing match the {brief.language} script and remain readable.", CriterionCategory.AUDIO, severity=Severity.MAJOR, evidence_types=["subtitle", "transcript"]),
            ])
        criteria.append(AcceptanceCriterion("The shot contains no unsafe or disallowed content under the project policy.", CriterionCategory.SAFETY, evidence_types=["frame", "policy"]))
        return criteria
