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
    ClarificationOption,
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

    QUESTIONS_ZH = (
        ("characters", "主要人物有哪些？哪些人物特征必须在所有镜头中保持一致？"),
        ("setting", "故事发生在什么时间和地点？整体视觉氛围是什么？"),
        ("arc", "从开场到结尾，故事和人物必须发生什么变化？"),
        ("audio", "需要使用什么语言？对白、旁白、音乐氛围和字幕有哪些要求？"),
    )

    def inspect(self, brief: CreativeBrief) -> list[ClarificationTurn]:
        text = brief.request.lower()
        turns: list[ClarificationTurn] = []
        questions = self.QUESTIONS_ZH if brief.language.lower().startswith("zh") else self.QUESTIONS
        for key, question in questions:
            markers = {
                "characters": ("character", "protagonist", "hero", "girl", "boy", "woman", "man", "人物", "主角"),
                "setting": ("in ", "at ", "city", "forest", "room", "街", "城市", "森林", "场景"),
                "arc": ("then", "finally", "journey", "discovers", "救", "最后", "然后", "故事"),
                "audio": ("dialogue", "voice", "narration", "music", "sound", "对白", "旁白", "音乐", "音效"),
            }[key]
            if not any(marker in text for marker in markers):
                turns.append(ClarificationTurn(question=question, required=True, options=self._options(key, chinese=questions is self.QUESTIONS_ZH)))
        return turns

    @staticmethod
    def _options(key: str, *, chinese: bool) -> list[ClarificationOption]:
        values = {
            "characters": [
                ("沿用一位主角", "聚焦单一人物，跨镜头一致性最好。"),
                ("两位核心人物", "增加关系张力，但需要更严格控制人物身份。"),
                ("群像叙事", "画面更丰富，生成成本和一致性风险也更高。"),
            ],
            "setting": [
                ("现代城市", "便于使用常见场景素材，节奏更利落。"),
                ("自然环境", "氛围感更强，但天气和空间连续性要求更高。"),
                ("架空世界", "想象空间最大，需要更完整的世界观设定。"),
            ],
            "arc": [
                ("完成一次明确任务", "故事目标清晰，适合短片快速收束。"),
                ("人物完成内心转变", "情绪更细腻，需要更稳定的表演和节奏。"),
                ("留下开放式结尾", "余味更长，但结局不会完全解释。"),
            ],
            "audio": [
                ("中文旁白 + 字幕", "信息传达最稳定，适合中文观众。"),
                ("对白驱动 + 字幕", "人物互动更强，需要更严格的口型和时序控制。"),
                ("纯音乐与音效", "减少语言限制，把重点放在画面和氛围上。"),
            ],
        }
        if not chinese:
            return [ClarificationOption(label, label, explanation) for label, explanation in values[key]]
        return [ClarificationOption(label, label, explanation) for label, explanation in values[key]]

    def apply_answers(self, turns: list[ClarificationTurn], answers: dict[str, str]) -> list[ClarificationTurn]:
        if not isinstance(answers, dict):
            raise TypeError("clarification answers must be an object")
        invalid = [key for key, value in answers.items() if not isinstance(key, str) or not isinstance(value, (str, dict))]
        if invalid:
            raise TypeError("clarification answers must map ids to strings or {answer, skip} objects")
        updated: list[ClarificationTurn] = []
        for turn in turns:
            answer = answers.get(turn.id) or answers.get(turn.question)
            if answer is None:
                updated.append(turn)
            elif isinstance(answer, dict) and answer.get("skip"):
                updated.append(replace(turn, answer=None, confirmed=True, skipped=True, confidence=1.0))
            else:
                text = answer if isinstance(answer, str) else str(answer.get("answer", ""))
                updated.append(replace(turn, answer=text, confirmed=bool(text.strip()), skipped=False, confidence=1.0))
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
        default_parallelism: int = 1,
    ) -> PlanVersion:
        errors = brief.validate()
        if errors:
            raise ValueError("Invalid creative brief: " + "; ".join(errors))
        answers = " ".join(turn.answer or "" for turn in clarifications)
        source = f"{brief.request} {answers}".strip()
        chinese = brief.language.lower().startswith("zh")
        character_text = self._extract_character(source)
        setting_text = self._extract_setting(source)
        arc_text = self._extract_arc(source)
        if chinese and arc_text:
            arc_text = arc_text.rstrip("。！？!?；;，, ")
        character = CharacterBible(
            name=character_text or ("主角" if chinese else "Lead"),
            identity=character_text or ("故事的核心人物" if chinese else "The central character of the story"),
            visual_traits=(
                ["面部和轮廓保持一致", "年龄与服装在所有镜头中保持不变"]
                if chinese
                else ["consistent face and silhouette", "age and wardrobe remain unchanged"]
            ),
            wardrobe=["沿用已批准的参考素材中的服装"] if chinese else ["wardrobe from the approved reference"],
            voice_traits=["所有镜头使用同一声音身份"] if chinese else ["same voice identity across shots"],
        )
        location = LocationBible(
            name=setting_text or ("主要场景" if chinese else "Primary location"),
            description=setting_text or ("连贯统一的故事场景" if chinese else "A coherent story location"),
            continuity_notes=(
                ["保持空间关系、天气、光线方向和关键道具一致"]
                if chinese
                else ["preserve geography, weather, light direction and hero props"]
            ),
        )
        style = StyleBible(
            name=brief.style or ("电影感连续风格" if chinese else "Cinematic continuity style"),
            description=brief.style or ("连贯统一的电影化视觉语言，自然可信的运动表现" if chinese else "Cinematic, coherent visual language with natural motion"),
            palette=["深蓝色", "温暖的实景光", "自然肤色"] if chinese else ["deep blue", "warm practical light", "neutral skin tones"],
            lighting="方向统一、动机明确的柔和主光" if chinese else "motivated soft key with consistent direction",
            camera_language="镜头覆盖有明确意图，并保持画面方向一致" if chinese else "deliberate coverage; preserve screen direction",
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
                    title=f"场景 {len(scenes) + 1}" if chinese else f"Scene {len(scenes) + 1}",
                    summary=arc_text or ("主角故事中的一个情节点" if chinese else "A beat in the protagonist's story"),
                    location_id=location.id,
                    time_of_day="连续" if chinese else "continuous",
                ))
            scene = scenes[scene_index]
            beat = self._beat(index, shot_count, arc_text, chinese=chinese)
            description = (
                f"{beat}；{character.identity}；地点：{location.description}。"
                if chinese
                else f"{beat}; {character.identity}; in {location.description}."
            )
            criteria = self._criteria(brief, character, location, style, beat, chinese=chinese)
            prompt = PromptBundle(
                positive=(
                    f"{description} 电影化连续性，{style.description}，"
                    f"镜头语言：{style.camera_language}。保持已批准的人物与场景参考。"
                    if chinese
                    else f"{description} Cinematic continuity, {style.description}, "
                    f"camera language: {style.camera_language}. Maintain approved character and location references."
                ),
                negative=(
                    "人物身份漂移、服装变化、多余肢体、空间关系错误、画面闪烁、文字不可读、画面裁切"
                    if chinese
                    else "identity drift, wardrobe change, extra limbs, broken geography, flicker, unreadable text, clipping"
                ),
                parameters={"duration_seconds": brief.shot_duration_seconds, "fps": brief.fps, "aspect_ratio": brief.aspect_ratio, "attempt": 1},
                reference_asset_ids=[reference.id],
                model="auto",
            )
            previous = shots[-1].id if shots else None
            shot = Shot(
                sequence=index + 1,
                scene_id=scene.id,
                title=(
                    f"镜头 {index + 1}：{beat.split('（', 1)[0]}"
                    if chinese
                    else f"Shot {index + 1}: {beat.split(';')[0]}"
                ),
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
        resolved = self.resolve_settings(brief, default_parallelism=default_parallelism)
        for shot in shots:
            if shot.prompt_bundle:
                shot.prompt_bundle.parameters.update(resolved)
        plan = PlanVersion(version, brief, scenes, shots, [character], [location], style, [reference], cues, resolved_settings=resolved)
        validation = plan.validate()
        if validation:
            raise ValueError("Generated invalid plan: " + "; ".join(validation))
        return plan

    @staticmethod
    def resolve_settings(brief: CreativeBrief, *, default_parallelism: int = 1) -> dict[str, object]:
        if brief.parallelism_mode in {"preset", "custom"} and brief.parallelism:
            parallelism = brief.parallelism
        elif brief.parallelism_mode == "auto":
            parallelism = max(1, int(default_parallelism))
        else:
            parallelism = max(1, int(default_parallelism))
        if brief.resolution_mode == "custom" and brief.resolution_width and brief.resolution_height:
            width, height = brief.resolution_width, brief.resolution_height
        elif brief.resolution_mode == "preset":
            width, height = (1280, 720) if brief.resolution_width == 1280 else (3840, 2160) if brief.resolution_width == 3840 else (1920, 1080)
        else:
            width, height = (1920, 1080)
        acceptance = brief.acceptance_mode
        if acceptance == "auto":
            acceptance = "standard"
        return {
            "parallelism": parallelism,
            "resolution_width": width,
            "resolution_height": height,
            "acceptance_mode": acceptance,
            "shot_duration_seconds": brief.shot_duration_seconds,
            "duration_seconds": brief.duration_seconds,
        }

    @staticmethod
    def _audio_cues(brief: CreativeBrief, source: str, shot_count: int) -> list[AudioCue]:
        if not brief.audio_required:
            return []
        duration = brief.duration_seconds
        chinese = brief.language.lower().startswith("zh")
        return [
            AudioCue("narration", source, 0.0, duration, voice="narrator", parameters={"language": brief.language}),
            AudioCue("music", "连续的电影感配乐" if chinese else "continuous cinematic underscore", 0.0, duration, parameters={"mix_db": -18, "loop": True}),
            AudioCue("sfx", "雨声与拆信动作音效" if chinese else "rain and letter handling accents", 0.0, duration, parameters={"mix_db": -24}),
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

    def _beat(self, index: int, total: int, arc: str | None, *, chinese: bool = False) -> str:
        if total == 1:
            phase = "完整故事情节点得到收束" if chinese else "the complete story beat resolves"
        elif index == 0:
            phase = "开场交代人物与地点" if chinese else "the opening establishes the character and place"
        elif index == total - 1:
            phase = "结尾完成核心转变" if chinese else "the ending resolves the central change"
        elif index < total / 2:
            phase = "触发事件建立明确目标" if chinese else "the inciting action creates a clear objective"
        else:
            phase = "人物承接后果并走向解决" if chinese else "the character acts on the consequence and moves toward resolution"
        return f"{phase}（故事意图：{arc or '一条聚焦的电影叙事弧线'}）" if chinese else f"{phase}; story intent: {arc or 'a focused cinematic arc'}"

    def _criteria(self, brief: CreativeBrief, character: CharacterBible, location: LocationBible, style: StyleBible, beat: str, *, chinese: bool = False) -> list[AcceptanceCriterion]:
        criteria = [
            AcceptanceCriterion(
                f"已批准的人物“{character.name}”在整个镜头中清晰可见且易于识别。" if chinese else f"The approved subject {character.name} is visible and recognizable throughout the shot.",
                CriterionCategory.SUBJECT,
                evidence_types=["frame", "track"],
            ),
            AcceptanceCriterion(
                f"动作需要清楚传达：{beat}。" if chinese else f"The action clearly communicates: {beat}.",
                CriterionCategory.ACTION,
                evidence_types=["frame", "temporal"],
            ),
            AcceptanceCriterion(
                "构图与镜头运动有明确意图、保持稳定，并维持画面方向一致。" if chinese else "Camera composition and motion are intentional, stable, and preserve screen direction.",
                CriterionCategory.CAMERA,
                evidence_types=["frame", "temporal"],
            ),
            AcceptanceCriterion(
                f"镜头需要遵循人物设定 Bible 与场景设定 Bible：{location.name}。" if chinese else f"The shot preserves the character bible and the location bible: {location.name}.",
                CriterionCategory.CONTINUITY,
                evidence_types=["frame", "embedding"],
            ),
            AcceptanceCriterion(
                f"视觉语言需要符合风格 Bible：{style.name}。" if chinese else f"The visual language matches the style bible: {style.name}.",
                CriterionCategory.STYLE,
                evidence_types=["frame", "color"],
            ),
            AcceptanceCriterion(
                "生成产物需要满足时长、画幅、帧率和安全策略约束。" if chinese else "The rendered artifact meets duration, aspect ratio, frame rate and safety constraints.",
                CriterionCategory.TECHNICAL,
                evidence_types=["metadata"],
            ),
        ]
        if brief.audio_required:
            criteria.extend([
                AcceptanceCriterion(
                    "对白、旁白、音乐和音效只在计划的位置出现，并且保持清晰可辨。" if chinese else "Dialogue, narration, music and effects are present only where planned and remain intelligible.",
                    CriterionCategory.AUDIO,
                    evidence_types=["audio", "transcript"],
                ),
                AcceptanceCriterion(
                    f"字幕与对白时序需要符合 {'中文' if chinese else brief.language} 脚本，并保持清晰易读。" if chinese else f"Subtitles and dialogue timing match the {brief.language} script and remain readable.",
                    CriterionCategory.AUDIO,
                    severity=Severity.MAJOR,
                    evidence_types=["subtitle", "transcript"],
                ),
            ])
        criteria.append(AcceptanceCriterion(
            "镜头不得包含项目策略禁止或不安全的内容。" if chinese else "The shot contains no unsafe or disallowed content under the project policy.",
            CriterionCategory.SAFETY,
            evidence_types=["frame", "policy"],
        ))
        return criteria
