"""Resumable orchestration of generation, judging, repair and assembly."""
from __future__ import annotations

import copy
import math
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Any

from .continuity import ContinuityGuardian
from .dependencies import DependencyError, ready_shots
from .planning import ClarificationAgent, PlanAgent
from .providers.base import (
    AssemblerProvider,
    AudioProvider,
    AudioRequest,
    LipSyncProvider,
    PollResult,
    ProviderContext,
    ReferenceProvider,
    VideoGeneratorProvider,
    VideoRequest,
)
from .quality import QualityController
from .schemas import (
    ArtifactRef,
    AssetKind,
    Attempt,
    AudioCue,
    CostRecord,
    CreativeBrief,
    Project,
    ProjectStatus,
    PromptBundle,
    ProviderError,
    ProviderJob,
    RepairAction,
    RepairKind,
    Shot,
    new_id,
    stable_hash,
)
from .store import EventStore


class BudgetExceeded(RuntimeError):
    pass


class HumanGate(RuntimeError):
    pass


class DirectorOrchestrator:
    def __init__(
        self,
        *,
        video_provider: VideoGeneratorProvider,
        video_providers: Mapping[str, VideoGeneratorProvider] | None = None,
        reference_provider: ReferenceProvider | None = None,
        judge_provider,
        assembler: AssemblerProvider,
        audio_provider: AudioProvider | None = None,
        lip_sync_provider: LipSyncProvider | None = None,
        store: EventStore | None = None,
        max_attempts: int = 3,
        max_poll_cycles: int = 8,
        parallelism: int = 1,
    ) -> None:
        self.video_provider = video_provider
        self.video_providers: dict[str, VideoGeneratorProvider] = dict(video_providers or {})
        self.video_providers.setdefault(getattr(video_provider, "name", "default"), video_provider)
        self.reference_provider = reference_provider
        self.quality = QualityController(judge_provider, max_attempts=max_attempts)
        self.assembler = assembler
        self.audio_provider = audio_provider
        self.lip_sync_provider = lip_sync_provider
        self.callback_url = None
        self.store = store or EventStore()
        self.max_poll_cycles = max_poll_cycles
        if isinstance(parallelism, bool) or not isinstance(parallelism, int) or parallelism < 1:
            raise ValueError("parallelism must be a positive integer")
        self.parallelism = parallelism
        self._parallel_merge_lock = threading.RLock()
        self.clarifier = ClarificationAgent()
        self.planner = PlanAgent()
        self.continuity = ContinuityGuardian()
        self.max_attempts = max_attempts

    def create_project(self, brief: CreativeBrief) -> Project:
        errors = brief.validate()
        if errors:
            raise ValueError("Invalid creative brief: " + "; ".join(errors))
        project = Project(brief.title, brief)
        project.clarification_turns = self.clarifier.inspect(brief)
        project.transition(ProjectStatus.CLARIFYING if project.clarification_turns else ProjectStatus.AWAITING_PLAN_APPROVAL)
        self.store.save_project(project, event_type="project.created")
        return project

    def answer_clarifications(self, project: Project, answers: dict[str, str]) -> Project:
        project.clarification_turns = self.clarifier.apply_answers(project.clarification_turns, answers)
        unresolved = self.clarifier.unresolved(project.clarification_turns)
        project.transition(ProjectStatus.CLARIFYING if unresolved else ProjectStatus.AWAITING_PLAN_APPROVAL)
        self.store.save_project(project, event_type="clarification.answered", payload={"remaining": len(unresolved)})
        return project

    def plan(self, project: Project) -> Project:
        unresolved = self.clarifier.unresolved(project.clarification_turns)
        if unresolved:
            raise HumanGate("Required clarification is unresolved")
        version = len(project.plans) + 1
        project.plans.append(self.planner.create_plan(project.brief, version=version, clarifications=project.clarification_turns))
        project.transition(ProjectStatus.AWAITING_PLAN_APPROVAL)
        self.store.save_project(project, event_type="plan.created", payload={"plan_id": project.active_plan.id})
        return project

    def approve_plan(self, project: Project, *, actor: str = "human") -> Project:
        plan = project.active_plan
        if plan is None:
            raise ValueError("No plan exists")
        if plan.validate():
            raise ValueError("Cannot approve invalid plan")
        plan.status = "approved"
        plan.approved_by = actor
        project.transition(ProjectStatus.PLANNED)
        self.store.save_project(project, event_type="plan.approved", payload={"plan_id": plan.id, "actor": actor})
        return project

    def rollback_plan(self, project: Project, version: int, *, actor: str = "human") -> Project:
        """Create a new draft plan from a previous version.

        Plans are append-only snapshots. Rolling back therefore never mutates a
        historical plan; it appends a fresh version that can be reviewed and
        approved like any other plan.
        """
        source = next((candidate for candidate in project.plans if candidate.version == version), None)
        if source is None:
            raise KeyError(version)
        if project.status in {ProjectStatus.DELIVERED, ProjectStatus.CANCELLED}:
            raise HumanGate("Terminal projects cannot roll back a plan")
        # Do not clone a plan while a provider job or assembler may still be
        # mutating the active execution wave. Operators can first pause or
        # recover that work, then request a rollback from a stable state.
        rollback_states = {
            ProjectStatus.CLARIFYING,
            ProjectStatus.AWAITING_PLAN_APPROVAL,
            ProjectStatus.PLANNED,
            ProjectStatus.AWAITING_HUMAN,
            ProjectStatus.FAILED,
        }
        if project.status not in rollback_states:
            raise HumanGate(f"Plan rollback is not allowed while project is {project.status.value}")
        restored = deepcopy(source)
        scene_ids = {scene.id: new_id("scene") for scene in restored.scenes}
        shot_ids = {shot.id: new_id("shot") for shot in restored.shots}
        for scene in restored.scenes:
            scene.id = scene_ids[scene.id]
            scene.shot_ids = [shot_ids.get(shot_id, shot_id) for shot_id in scene.shot_ids]
        for shot in restored.shots:
            old_id = shot.id
            shot.id = shot_ids[old_id]
            shot.scene_id = scene_ids.get(shot.scene_id, shot.scene_id)
            shot.previous_shot_id = shot_ids.get(shot.previous_shot_id, shot.previous_shot_id)
            shot.next_shot_id = shot_ids.get(shot.next_shot_id, shot.next_shot_id)
            shot.depends_on_shot_ids = [shot_ids.get(dep, dep) for dep in shot.depends_on_shot_ids]
            shot.acceptance_criteria = [replace(criterion, id=new_id("criterion")) for criterion in shot.acceptance_criteria]
            if shot.prompt_bundle is not None:
                shot.prompt_bundle = replace(shot.prompt_bundle, id=new_id("prompt"))
        restored.audio_cues = [replace(cue, id=new_id("cue")) for cue in restored.audio_cues]
        restored.version = max((candidate.version for candidate in project.plans), default=0) + 1
        restored.id = new_id("plan")
        restored.status = "draft"
        restored.approved_by = None
        project.plans.append(restored)
        project.transition(ProjectStatus.AWAITING_PLAN_APPROVAL, force=project.status == ProjectStatus.FAILED)
        self.store.save_project(project, event_type="plan.rolled_back", payload={"source_version": version, "plan_id": restored.id, "actor": actor})
        return project

    def pause(self, project: Project, *, actor: str = "human", reason: str = "paused by operator") -> Project:
        """Pause a resumable project at the human gate."""
        if project.status in {ProjectStatus.DELIVERED, ProjectStatus.CANCELLED}:
            raise HumanGate("Terminal projects cannot be paused")
        project.transition(ProjectStatus.AWAITING_HUMAN)
        self.store.save_project(project, event_type="project.paused", payload={"actor": actor, "reason": reason})
        return project

    def run(self, project: Project, *, approve_plan: bool = False, actor: str = "human") -> Project:
        if project.status == ProjectStatus.DELIVERED:
            raise HumanGate("Delivered projects cannot be run again without a new plan version")
        active_delivery = self._active_delivery(project)
        if project.status == ProjectStatus.AWAITING_HUMAN and active_delivery is not None:
            # Assembly already produced a delivery artifact. Keep the human
            # approval gate stable instead of creating duplicate deliveries
            # when a client retries the run command.
            return project
        if project.active_plan is None:
            self.plan(project)
        if project.status == ProjectStatus.AWAITING_PLAN_APPROVAL:
            if not approve_plan:
                raise HumanGate("Plan approval is required")
            self.approve_plan(project, actor=actor)
        resumable = {
            ProjectStatus.PLANNED,
            ProjectStatus.GENERATING,
            ProjectStatus.JUDGING,
            ProjectStatus.REPAIRING,
            ProjectStatus.FAILED,
            ProjectStatus.AWAITING_HUMAN,
        }
        if project.status not in resumable:
            raise HumanGate(f"Project is not ready to run: {project.status.value}")
        project.transition(ProjectStatus.GENERATING)
        self.store.save_project(project, event_type="generation.started", payload={"resume": bool(project.attempts)})
        if self.parallelism > 1:
            return self._run_parallel(project)
        artifacts_by_shot: dict[str, list[ArtifactRef]] = {}
        shot_index = 0
        # Read the plan on every iteration: a split-shot repair inserts a new
        # node and the same run must execute that node before assembly.
        while project.active_plan and shot_index < len(project.active_plan.shots):
            shot = sorted(project.active_plan.shots, key=lambda item: item.sequence)[shot_index]
            cached = self._passed_artifacts(project, shot.id)
            if cached:
                artifacts_by_shot[shot.id] = cached
                self.store.append_event(project.id, "shot.resumed", {"shot_id": shot.id, "attempt_id": self._latest_passed_attempt(project, shot.id).id})
                shot_index += 1
                continue
            if project.over_budget():
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(project, event_type="budget.hard_stop")
                raise BudgetExceeded("Project budget reached")
            artifacts_by_shot[shot.id] = self._run_shot(project, shot)
            # Catch identity/location/style drift as soon as an adjacent shot
            # is available, before the project spends money on the rest.
            completed_shots = [candidate for candidate in project.active_plan.shots if candidate.id in artifacts_by_shot]
            if len(completed_shots) > 1:
                continuity = self.continuity.check(project, completed_shots, artifacts_by_shot)
                if not continuity.passed:
                    project.transition(ProjectStatus.AWAITING_HUMAN)
                    self.store.save_project(project, event_type="continuity.failed", payload={"issues": [issue.message for issue in continuity.issues], "checked_shot_ids": continuity.checked_shot_ids})
                    raise HumanGate("Cross-shot continuity requires human review")
            shot_index += 1
        return self._assemble_project(project, artifacts_by_shot)

    def _run_parallel(self, project: Project) -> Project:
        """Run dependency-ready shots in bounded concurrent waves.

        Each shot receives a deep-copied project and an isolated in-memory
        EventStore.  This keeps the existing attempt/retry implementation
        single-threaded per shot while allowing independent provider jobs to
        overlap.  Results are merged under a project lock using attempt and
        shot IDs, then persisted with the normal snapshot CAS.
        """
        plan = project.active_plan
        if plan is None:
            raise HumanGate("No plan exists")
        artifacts_by_shot: dict[str, list[ArtifactRef]] = {}
        completed_ids: set[str] = set()
        for shot in plan.shots:
            cached = self._passed_artifacts(project, shot.id)
            if cached:
                artifacts_by_shot[shot.id] = cached
                completed_ids.add(shot.id)
        pending_ids = {shot.id for shot in plan.shots if shot.id not in completed_ids}
        while pending_ids:
            current_plan = project.active_plan
            if current_plan is None:
                raise HumanGate("Active plan disappeared during parallel execution")
            try:
                ready = [shot for shot in ready_shots(current_plan.shots, completed_ids) if shot.id in pending_ids]
            except DependencyError as error:
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(project, event_type="scheduler.blocked", payload={"reason": str(error), "pending_shot_ids": sorted(pending_ids)})
                raise HumanGate(str(error)) from error
            if not ready:
                reason = f"No dependency-ready shots remain: {sorted(pending_ids)}"
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(project, event_type="scheduler.blocked", payload={"reason": reason, "pending_shot_ids": sorted(pending_ids)})
                raise HumanGate(reason)
            wave = ready[: self.parallelism]
            baseline = copy.deepcopy(project)
            baseline_cost = project.total_cost_usd
            self.store.append_event(project.id, "scheduler.wave.started", {"shot_ids": [shot.id for shot in wave], "parallelism": self.parallelism})
            results: list[tuple[Shot, Project, list[ArtifactRef], list[Any], BaseException | None]] = []
            with ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="director-shot") as executor:
                futures = {executor.submit(self._run_parallel_shot, baseline, shot.id): shot for shot in wave}
                for future in as_completed(futures):
                    shot = futures[future]
                    try:
                        local_project, artifacts, worker_events, error = future.result()
                    except BaseException as future_error:  # noqa: BLE001 - preserve worker interruptions for recovery
                        local_project, artifacts, worker_events = copy.deepcopy(baseline), [], []
                        error = future_error
                    results.append((shot, local_project, artifacts, worker_events, error))
            # Merge all completed workers before making a policy decision. A
            # different shot may have passed even when one worker was stopped.
            for shot, local_project, artifacts, worker_events, error in results:
                added_shot_ids = self._merge_parallel_result(
                    project,
                    local_project,
                    baseline_cost,
                    shot.id,
                    error,
                    baseline_project=baseline,
                    worker_events=worker_events,
                )
                pending_ids.update(added_shot_ids)
                if artifacts and error is None:
                    artifacts_by_shot[shot.id] = artifacts
                    completed_ids.add(shot.id)
                    pending_ids.discard(shot.id)
                elif error is not None:
                    pending_ids.discard(shot.id)
            self.store.append_event(project.id, "scheduler.wave.completed", {"shot_ids": [shot.id for shot in wave], "completed_shot_ids": sorted(completed_ids), "pending_shot_ids": sorted(pending_ids)})
            interruption = next((error for _, _, _, _, error in results if type(error) is RuntimeError), None)
            if interruption is not None:
                project.transition(ProjectStatus.FAILED, force=True)
                self.store.save_project(project, event_type="scheduler.interrupted", payload={"error": str(interruption)})
                raise interruption
            unknown_error = next(
                (
                    error
                    for _, _, _, _, error in results
                    if error is not None and not isinstance(error, (BudgetExceeded, HumanGate))
                ),
                None,
            )
            if unknown_error is not None:
                # A worker exception is never evidence that its Shot passed.
                # Stop before assembly so a partial wave cannot be delivered.
                project.transition(ProjectStatus.FAILED, force=True)
                self.store.save_project(
                    project,
                    event_type="scheduler.failed",
                    payload={"error": str(unknown_error), "error_type": type(unknown_error).__name__},
                )
                raise unknown_error
            policy_error = next((error for _, _, _, _, error in results if isinstance(error, (BudgetExceeded, HumanGate))), None)
            if policy_error is not None:
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(project, event_type="scheduler.awaiting_human", payload={"reason": str(policy_error), "completed_shot_ids": sorted(completed_ids)})
                raise policy_error
            # A provider failure may have left a shot without a passed attempt.
            # Keep it pending so the next wave can resume it; a failed attempt
            # is not treated as a satisfied dependency.
            for shot in wave:
                if shot.id in completed_ids:
                    continue
                if self._passed_artifacts(project, shot.id):
                    artifacts_by_shot[shot.id] = self._passed_artifacts(project, shot.id)
                    completed_ids.add(shot.id)
                    pending_ids.discard(shot.id)
                elif not any(attempt.shot_id == shot.id and attempt.status in {"created", "submitted", "running", "generated", "judged_failed", "provider_failed"} for attempt in project.attempts):
                    pending_ids.discard(shot.id)
            if pending_ids and project.over_budget():
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(project, event_type="budget.hard_stop", payload={"pending_shot_ids": sorted(pending_ids)})
                raise BudgetExceeded("Project budget reached")
            if not pending_ids and len(completed_ids) != len({shot.id for shot in (project.active_plan.shots if project.active_plan else [])}):
                # Keep the scheduler fail-closed if a worker returned without
                # artifacts and without an explicit policy exception.
                missing = sorted(
                    {shot.id for shot in (project.active_plan.shots if project.active_plan else [])}
                    - completed_ids
                )
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(
                    project,
                    event_type="scheduler.incomplete",
                    payload={"missing_shot_ids": missing},
                )
                raise HumanGate(f"Parallel execution is missing passed shots: {missing}")
        return self._assemble_project(project, artifacts_by_shot)

    def _run_parallel_shot(self, baseline: Project, shot_id: str) -> tuple[Project, list[ArtifactRef], list[Any], BaseException | None]:
        """Execute one shot against an isolated project snapshot."""
        from .store import EventStore

        local_store = EventStore(":memory:")
        try:
            local_project = copy.deepcopy(baseline)
            local_store.save_project(local_project, event_type="parallel.worker.started", payload={"shot_id": shot_id})
            worker = DirectorOrchestrator(
                video_provider=self.video_provider,
                video_providers=self.video_providers,
                reference_provider=self.reference_provider,
                judge_provider=self.quality.judge,
                assembler=self.assembler,
                audio_provider=self.audio_provider,
                lip_sync_provider=self.lip_sync_provider,
                store=local_store,
                max_attempts=self.max_attempts,
                max_poll_cycles=self.max_poll_cycles,
                parallelism=1,
            )
            shot = next(item for item in local_project.active_plan.shots if item.id == shot_id)
            artifacts = worker._run_shot(local_project, shot)
            return local_project, artifacts, local_store.events(local_project.id), None
        except BaseException as error:  # noqa: BLE001 - caller decides retry vs human policy
            local_project = locals().get("local_project", copy.deepcopy(baseline))
            return local_project, [], local_store.events(local_project.id), error
        finally:
            local_store.close()

    def _merge_parallel_result(
        self,
        project: Project,
        local_project: Project,
        baseline_cost: float,
        shot_id: str,
        error: BaseException | None,
        *,
        baseline_project: Project | None = None,
        worker_events: list[Any] | None = None,
    ) -> set[str]:
        """Merge one isolated worker without overwriting another worker.

        Workers receive the same snapshot for a wave. Merging an entire local
        plan would therefore restore stale copies of shots completed by an
        earlier worker in the same wave. Apply only fields that changed
        relative to that worker baseline, preserving concurrent edits while
        still carrying split-shot topology and reference refreshes forward.
        """
        added_shot_ids: set[str] = set()
        with self._parallel_merge_lock:
            plan = project.active_plan
            local_plan = local_project.active_plan
            if plan is not None and local_plan is not None:
                baseline_plan = baseline_project.active_plan if baseline_project is not None else None
                baseline_shots = {shot.id: shot for shot in (baseline_plan.shots if baseline_plan else [])}
                main_shots = {shot.id: shot for shot in plan.shots}
                shot_fields = (
                    "sequence",
                    "scene_id",
                    "title",
                    "description",
                    "duration_seconds",
                    "character_ids",
                    "location_id",
                    "previous_shot_id",
                    "next_shot_id",
                    "acceptance_criteria",
                    "prompt_bundle",
                    "depends_on_shot_ids",
                )
                for local_shot in local_plan.shots:
                    existing = main_shots.get(local_shot.id)
                    baseline_shot = baseline_shots.get(local_shot.id)
                    if existing is None:
                        plan.shots.append(copy.deepcopy(local_shot))
                        main_shots[local_shot.id] = plan.shots[-1]
                        if baseline_shot is None:
                            added_shot_ids.add(local_shot.id)
                        continue
                    if baseline_shot is None:
                        continue
                    # Apply only fields this worker actually changed. This is
                    # important for a split in one shot shifting sequence/link
                    # fields while another worker repairs a neighbouring shot.
                    for field_name in shot_fields:
                        local_value = getattr(local_shot, field_name)
                        baseline_value = getattr(baseline_shot, field_name)
                        if local_value != baseline_value:
                            setattr(existing, field_name, copy.deepcopy(local_value))
                plan.shots.sort(key=lambda item: item.sequence)
                main_scenes = {scene.id: scene for scene in plan.scenes}
                for local_scene in local_plan.scenes:
                    existing_scene = main_scenes.get(local_scene.id)
                    if existing_scene is None:
                        plan.scenes.append(copy.deepcopy(local_scene))
                        main_scenes[local_scene.id] = plan.scenes[-1]
                        continue
                    # Scene membership is a topology index. Union memberships
                    # from concurrent split workers, then order by merged
                    # shot sequence so the source-of-truth remains deterministic.
                    merged_ids = list(dict.fromkeys([*existing_scene.shot_ids, *local_scene.shot_ids]))
                    sequence_by_id = {shot.id: shot.sequence for shot in plan.shots}
                    existing_scene.shot_ids = sorted(
                        (shot_id for shot_id in merged_ids if shot_id in sequence_by_id),
                        key=lambda value: sequence_by_id[value],
                    )
                # Reference refreshes mutate plan assets in place. Merge those
                # changes with the same baseline-diff rule so two shots
                # refreshing shared references do not lose metadata.
                baseline_assets = {asset.id: asset for asset in (baseline_plan.reference_assets if baseline_plan else [])}
                main_assets = {asset.id: asset for asset in plan.reference_assets}
                for local_asset in local_plan.reference_assets:
                    existing_asset = main_assets.get(local_asset.id)
                    baseline_asset = baseline_assets.get(local_asset.id)
                    if existing_asset is None:
                        plan.reference_assets.append(copy.deepcopy(local_asset))
                        main_assets[local_asset.id] = plan.reference_assets[-1]
                        continue
                    if baseline_asset is None:
                        continue
                    for field_name in ("kind", "uri", "sha256", "provider", "metadata"):
                        local_value = getattr(local_asset, field_name)
                        baseline_value = getattr(baseline_asset, field_name)
                        if local_value != baseline_value:
                            setattr(existing_asset, field_name, copy.deepcopy(local_value))
            baseline_attempts = {
                attempt.id: attempt for attempt in (baseline_project.attempts if baseline_project is not None else [])
            }
            main_attempts = {attempt.id: attempt for attempt in project.attempts}
            attempt_fields = (
                "shot_id",
                "number",
                "prompt_bundle",
                "provider_job",
                "artifacts",
                "cost",
                "judge_result",
                "diagnosis",
                "repair_action",
                "status",
            )
            for local_attempt in local_project.attempts:
                existing = main_attempts.get(local_attempt.id)
                baseline_attempt = baseline_attempts.get(local_attempt.id)
                if existing is None:
                    project.attempts.append(copy.deepcopy(local_attempt))
                    main_attempts[local_attempt.id] = project.attempts[-1]
                    continue
                if baseline_attempt is None:
                    # An attempt with this ID was introduced by another worker
                    # in the same wave. The local baseline cannot authoritatively
                    # overwrite it.
                    continue
                for field_name in attempt_fields:
                    local_value = getattr(local_attempt, field_name)
                    baseline_value = getattr(baseline_attempt, field_name)
                    if local_value != baseline_value:
                        setattr(existing, field_name, copy.deepcopy(local_value))
            delta = local_project.total_cost_usd - baseline_cost
            if math.isfinite(delta) and delta > 0:
                project.total_cost_usd += delta
            # Replay the isolated worker event trail into the durable project
            # stream. Snapshot merging alone would preserve state but lose
            # details such as shot.split, repair and provider lifecycle events.
            # Event IDs are regenerated by the main store; the worker event ID
            # remains available in the payload for audit joins.
            for worker_event in worker_events or ():
                payload = dict(worker_event.payload) if isinstance(worker_event.payload, Mapping) else {}
                payload.setdefault("parallel_worker_shot_id", shot_id)
                payload.setdefault("parallel_worker_event_id", getattr(worker_event, "id", None))
                self.store.append_event(project.id, worker_event.event_type, payload)
            # The summary event makes wave reconciliation easy to query even
            # when consumers do not inspect every replayed worker event.
            self.store.append_event(project.id, "scheduler.shot.merged", {"shot_id": shot_id, "error": str(error) if error else None, "attempt_ids": [attempt.id for attempt in project.attempts if attempt.shot_id == shot_id]})
            self.store.save_project(project, event_type="scheduler.shot.persisted", payload={"shot_id": shot_id, "error": str(error) if error else None})
        return added_shot_ids

    def generate_shot(self, project: Project, shot_id: str, *, actor: str = "human", force: bool = False) -> Project:
        """Generate and judge one approved shot as an explicit command.

        This command is intentionally narrower than :meth:`run`: it never
        assumes that the remaining shots are ready for assembly.  A successful
        shot leaves the project at the human gate unless every shot already
        has a passed attempt, in which case the normal assembly gate is used.
        Keeping this behavior in the orchestrator prevents API and worker
        entrypoints from drifting apart.
        """
        plan = project.active_plan
        if plan is None:
            raise HumanGate("No plan exists")
        shot = next((item for item in plan.shots if item.id == shot_id), None)
        if shot is None:
            raise KeyError(shot_id)
        allowed = {ProjectStatus.PLANNED, ProjectStatus.AWAITING_HUMAN, ProjectStatus.FAILED}
        if project.status not in allowed and not (force and project.status == ProjectStatus.DELIVERED):
            raise HumanGate(f"Shot generation is not allowed while project is {project.status.value}")
        if project.status == ProjectStatus.DELIVERED and not force:
            raise HumanGate("Delivered projects require an explicit force generation")
        if project.status == ProjectStatus.AWAITING_HUMAN and self._active_delivery(project) is not None and not force:
            raise HumanGate("A ready delivery requires an explicit force generation")
        project.transition(ProjectStatus.GENERATING, force=force)
        self.store.save_project(project, event_type="shot.generation.requested", payload={"shot_id": shot_id, "actor": actor, "force": force})
        prior_numbers = [attempt.number for attempt in project.attempts if attempt.shot_id == shot.id]
        attempt_limit = max(self.max_attempts, max(prior_numbers, default=0) + 1) if (force or prior_numbers) else self.max_attempts
        artifacts = self._run_shot(project, shot, max_attempts=attempt_limit)
        passed = self._all_passed_artifacts(project)
        passed[shot_id] = artifacts
        if all(passed.get(candidate.id) for candidate in (plan.shots if plan else [])):
            return self._assemble_project(project, passed)
        project.transition(ProjectStatus.AWAITING_HUMAN)
        self.store.save_project(project, event_type="shot.generation.ready", payload={"shot_id": shot_id, "artifact_count": len(artifacts)})
        return project

    def deliver(self, project: Project, *, actor: str = "human") -> Project:
        if project.status != ProjectStatus.AWAITING_HUMAN or not project.artifacts:
            raise HumanGate("A ready assembly and delivery approval are required")
        delivery = self._active_delivery(project)
        if delivery is None:
            raise HumanGate("A ready assembly and delivery approval are required")
        if (
            delivery.kind != AssetKind.VIDEO
            or not isinstance(delivery.metadata, dict)
            or delivery.metadata.get("artifact_role") != "delivery"
            or not isinstance(delivery.uri, str)
            or not delivery.uri.strip()
        ):
            raise HumanGate("A valid delivery artifact is required before approval")
        project.transition(ProjectStatus.DELIVERED)
        self.store.save_project(project, event_type="project.delivered", payload={"actor": actor, "artifact_id": delivery.id})
        return project

    def cancel(self, project: Project, *, actor: str = "human", reason: str = "cancelled by operator") -> Project:
        """Cancel a project and best-effort cancel outstanding provider jobs."""
        if project.status == ProjectStatus.DELIVERED:
            raise HumanGate("Delivered projects cannot be cancelled")
        for attempt in project.attempts:
            job = attempt.provider_job
            if job and job.status in {"queued", "running", "submitted"}:
                provider = self._provider_for_job(job)
                try:
                    provider.cancel(job)
                except Exception as error:  # noqa: BLE001 - cancellation is best effort
                    self.store.append_event(project.id, "provider.cancel_failed", {"attempt_id": attempt.id, "error": str(error)})
                job.status = "cancelled"
                attempt.status = "cancelled"
        project.transition(ProjectStatus.CANCELLED)
        self.store.save_project(project, event_type="project.cancelled", payload={"actor": actor, "reason": reason})
        return project

    def reconcile_provider_callback(self, project: Project, provider: str, payload: Mapping[str, Any]) -> Attempt:
        """Persist one external provider callback exactly once.

        Providers differ in field names, so the adapter's ``parse_poll`` method
        is used when available. The callback only settles the Attempt; a worker
        can subsequently resume the project and run Judge/Repair/Assembly.
        """
        if not isinstance(payload, Mapping):
            raise TypeError("provider callback payload must be an object")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider callback requires a provider name")
        provider = provider.strip()
        raw_external_id = payload.get("request_id") or payload.get("prompt_id") or payload.get("job_id") or payload.get("id")
        if not isinstance(raw_external_id, str) or not raw_external_id.strip():
            raise ValueError("provider callback is missing an external job id")
        external_id = raw_external_id.strip()
        candidates = [attempt for attempt in project.attempts if attempt.provider_job and attempt.provider_job.external_id == external_id]
        if not candidates:
            raise LookupError(f"No attempt is associated with provider job {external_id}")
        attempt = candidates[-1]
        if attempt.provider_job is None or attempt.provider_job.provider != provider:
            raise ValueError("provider callback identifies a different provider")
        # Never let an unregistered callback select the default adapter.  A
        # provider job is external state, so resolving it through a different
        # adapter could attach the wrong artifacts, cost or terminal status.
        callback_provider = self.video_providers.get(provider)
        if callback_provider is None:
            raise ValueError(f"provider callback references unknown provider: {provider}")
        callback_status = str(payload.get("status", payload.get("state", "queued"))).strip().lower()
        current_status = self._normalize_job_status(attempt.provider_job.status)
        if current_status is None:
            raise ValueError("persisted provider job has an invalid status")
        terminal_statuses = {"succeeded", "failed", "cancelled"}
        callback_output = self._first_present(payload, "output", "video", "artifacts", "data")
        callback_cost = payload.get("cost_usd") if "cost_usd" in payload else payload.get("cost")
        callback_fingerprint = stable_hash({
             "status": callback_status,
             "event_id": payload.get("event_id") or payload.get("eventId"),
             "output": callback_output,
             "cost": callback_cost,
         })[:16]
        # A provider may emit queued -> running -> completed callbacks. The
        # lifecycle status is part of the idempotency key, while duplicate
        # deliveries of the same payload remain no-ops.
        callback_key = f"provider-callback:{provider}:{external_id}:{callback_status}:{callback_fingerprint}"
        claimed, _ = self.store.claim_idempotency(project.id, callback_key, {"attempt_id": attempt.id})
        if not claimed:
            return attempt
        job = attempt.provider_job
        poll_result = None
        # A terminal attempt is immutable with respect to a callback that
        # carries a different lifecycle state.  Return before parsing so a
        # late success cannot sneak in artifacts or cost through an adapter.
        callback_status_normalized = self._normalize_job_status(callback_status)
        if current_status in terminal_statuses and callback_status_normalized != current_status:
            self.store.save_project(
                project,
                event_type="provider.callback.stale",
                payload={
                    "provider": provider,
                    "external_id": external_id,
                    "attempt_id": attempt.id,
                    "current_status": current_status,
                    "incoming_status": callback_status_normalized or callback_status,
                },
            )
            return attempt
        parser = getattr(callback_provider, "parse_callback", None) or getattr(callback_provider, "parse_poll", None)
        if callable(parser):
            try:
                poll_result = parser(payload, job)
                poll_result = self._validate_poll_result(callback_provider, poll_result, current_job=job)
            except Exception as error:  # noqa: BLE001 - malformed callbacks fail closed
                normalized = self._normalize_provider_error(callback_provider, error)
                if current_status in terminal_statuses:
                    self.store.save_project(
                        project,
                        event_type="provider.callback.invalid",
                        payload={
                            "provider": provider,
                            "external_id": external_id,
                            "attempt_id": attempt.id,
                            "error": normalized.code,
                            "message": normalized.message,
                            "ignored": True,
                        },
                    )
                    return attempt
                attempt.provider_job = replace(job, status="failed")
                attempt.status = "provider_failed"
                self.store.save_project(
                    project,
                    event_type="provider.callback.invalid",
                    payload={
                        "provider": provider,
                        "external_id": external_id,
                        "attempt_id": attempt.id,
                        "error": normalized.code,
                        "message": normalized.message,
                    },
                )
                return attempt
        if poll_result is not None:
            incoming_job = poll_result.job
            incoming_status = incoming_job.status
            if current_status in terminal_statuses and incoming_status != current_status:
                self.store.save_project(
                    project,
                    event_type="provider.callback.stale",
                    payload={
                        "provider": provider,
                        "external_id": external_id,
                        "attempt_id": attempt.id,
                        "current_status": current_status,
                        "incoming_status": incoming_status,
                    },
                )
                return attempt
            # Merge the lifecycle first.  Only a non-stale transition is
            # allowed to expose the callback's artifacts or settled cost.
            job = self._merge_callback_job(callback_provider, attempt.provider_job, incoming_job)
            if job.status != incoming_status:
                # The parser may return an older status than the persisted
                # job (for example queued after running).  Ignore the whole
                # payload, including any opportunistic output/cost fields.
                self.store.save_project(
                    project,
                    event_type="provider.callback.stale",
                    payload={
                        "provider": provider,
                        "external_id": external_id,
                        "attempt_id": attempt.id,
                        "current_status": current_status,
                        "incoming_status": incoming_status,
                    },
                )
                return attempt
            attempt.provider_job = job
            # Artifacts and settled cost are meaningful only for a succeeded
            # job.  Ignore opportunistic output/cost fields on queued/running
            # callbacks so a provider cannot charge or attach media before
            # the lifecycle reaches a terminal success.
            if job.status == "succeeded" and poll_result.artifacts and not attempt.artifacts:
                attempt.artifacts = self._validate_artifacts(poll_result.artifacts, provider_name=provider)
            if job.status == "succeeded" and poll_result.cost is not None and attempt.cost is None:
                attempt.cost = self._validate_cost(poll_result.cost, provider)
                self._record_cost(project, poll_result.cost)
            # Merge terminal states before applying the callback payload. A
            # late failed/cancelled webhook must not regress a job that already
            # reached succeeded and was persisted.
            if job.status in {"failed", "cancelled"}:
                attempt.status = "provider_failed"
            elif job.status == "succeeded":
                if not attempt.artifacts:
                    attempt.status = "provider_failed"
                    self.store.save_project(project, event_type="provider.callback.invalid", payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "error": "missing_artifact"})
                else:
                    attempt.status = "generated"
        else:
            status = self._normalize_job_status(payload.get("status", payload.get("state", job.status)))
            if status is None:
                if current_status in terminal_statuses:
                    self.store.save_project(project, event_type="provider.callback.invalid", payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "error": "invalid_status", "ignored": True})
                    return attempt
                attempt.provider_job = replace(job, status="failed")
                attempt.status = "provider_failed"
                self.store.save_project(project, event_type="provider.callback.invalid", payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "error": "invalid_status"})
                return attempt
            if current_status in terminal_statuses and status != current_status:
                self.store.save_project(
                    project,
                    event_type="provider.callback.stale",
                    payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "current_status": current_status, "incoming_status": status},
                )
                return attempt
            attempt.provider_job = self._merge_callback_job(callback_provider, job, replace(job, status=status))
            status = attempt.provider_job.status
            if status in {"failed", "cancelled"}:
                attempt.status = "provider_failed" if status == "failed" else "cancelled"
            elif status == "succeeded":
                attempt.status = "generated"
                if not attempt.artifacts:
                    attempt.artifacts = self._validate_artifacts(self._callback_artifacts(payload, provider, external_id), provider_name=provider)
                cost_raw = payload.get("cost_usd") if "cost_usd" in payload else payload.get("cost")
                if cost_raw is not None and attempt.cost is None:
                    try:
                        attempt.cost = CostRecord(float(cost_raw), provider=provider)
                        self._validate_cost(attempt.cost, provider)
                    except (TypeError, ValueError, OverflowError) as error:
                        attempt.status = "provider_failed"
                        self.store.save_project(project, event_type="provider.callback.invalid", payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "error": "invalid_cost", "message": str(error)})
                        return attempt
                    self._record_cost(project, attempt.cost)
                if not attempt.artifacts:
                    attempt.status = "provider_failed"
                    self.store.save_project(project, event_type="provider.callback.invalid", payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "error": "missing_artifact"})
        self.store.save_project(project, event_type="provider.callback", payload={"provider": provider, "external_id": external_id, "attempt_id": attempt.id, "status": attempt.provider_job.status})
        return attempt

    @staticmethod
    def _callback_artifacts(payload: Mapping[str, Any], provider: str, external_id: str) -> list[ArtifactRef]:
        """Map common webhook output shapes when an adapter has no parser."""
        output = DirectorOrchestrator._first_present(payload, "output", "video", "artifacts", "data")
        urls: list[str] = []
        if isinstance(output, str):
            urls = [output]
        elif isinstance(output, Mapping):
            candidate = output.get("video_url") or output.get("url") or output.get("video")
            if isinstance(candidate, str) and candidate.strip():
                urls = [candidate.strip()]
        elif isinstance(output, list):
            for item in output:
                if isinstance(item, str):
                    if item.strip():
                        urls.append(item.strip())
                elif isinstance(item, Mapping):
                    candidate = item.get("video_url") or item.get("url") or item.get("video")
                    if isinstance(candidate, str) and candidate.strip():
                        urls.append(candidate.strip())
        return [ArtifactRef(AssetKind.VIDEO, url.strip(), metadata={"provider": provider, "external_id": external_id}) for url in urls if isinstance(url, str) and url.strip()]

    @staticmethod
    def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
        """Return the first present webhook field, preserving valid falsey values."""
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        return None

    def _run_shot(self, project: Project, shot: Shot, *, max_attempts: int | None = None) -> list[ArtifactRef]:
        prompt = shot.prompt_bundle
        if prompt is None:
            raise ValueError(f"Shot {shot.id} has no prompt")
        attempt_limit = self.max_attempts if max_attempts is None else max(1, int(max_attempts))
        previous_attempts = [attempt.number for attempt in project.attempts if attempt.shot_id == shot.id]
        inflight = next(
            (attempt for attempt in reversed(project.attempts)
             if attempt.shot_id == shot.id and attempt.status in {"created", "submitted", "running", "generated", "judged_failed"}),
            None,
        )
        if inflight is not None:
            resumed = self._resume_attempt(project, shot, inflight, max_attempts=attempt_limit)
            if resumed is not None:
                return resumed
            prompt = shot.prompt_bundle or prompt
        first_number = inflight.number if inflight is not None and inflight.status == "created" and inflight.provider_job is None else max(previous_attempts, default=0) + 1
        # Automatic retries are bounded per shot across process restarts.
        if first_number > attempt_limit:
            project.transition(ProjectStatus.AWAITING_HUMAN)
            raise HumanGate(f"Shot {shot.sequence} exhausted automatic retries")
        for number in range(first_number, attempt_limit + 1):
            # A retry follows the explicit repair -> generation transition;
            # this also makes a resumed worker's next submission auditable.
            project.transition(ProjectStatus.GENERATING)
            if project.total_cost_usd >= project.brief.budget_usd:
                project.transition(ProjectStatus.AWAITING_HUMAN)
                self.store.save_project(project, event_type="budget.hard_stop", payload={"shot_id": shot.id, "attempt": number})
                raise BudgetExceeded("Project budget reached before shot generation")
            prompt = replace(prompt, parameters={**prompt.parameters, "attempt": number})
            attempt = next((item for item in project.attempts if item.shot_id == shot.id and item.number == number and item.status == "created" and item.provider_job is None), None)
            if attempt is None:
                attempt = Attempt(shot.id, number, prompt, status="created")
                project.attempts.append(attempt)
                self.store.save_project(project, event_type="shot.attempt.created", payload={"shot_id": shot.id, "attempt": number})
            else:
                attempt.prompt_bundle = prompt
                self.store.save_project(project, event_type="shot.attempt.resumed", payload={"shot_id": shot.id, "attempt": number})
            provider = self._provider_for_prompt(prompt)
            provider_name = getattr(provider, "name", None)
            prompt = replace(prompt, provider=provider_name)
            attempt.prompt_bundle = prompt
            request = self._video_request(project, shot, prompt)
            try:
                estimate = self._validate_cost(provider.estimate_cost(request), provider_name)
            except Exception as error:  # noqa: BLE001 - pricing failures are retryable provider errors
                normalized = self._normalize_provider_error(provider, error)
                attempt.status = "provider_failed"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": number, "error": normalized.code, "message": normalized.message})
                self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                prompt = shot.prompt_bundle or prompt
                continue
            if project.total_cost_usd + estimate.amount_usd > project.brief.budget_usd:
                project.transition(ProjectStatus.AWAITING_HUMAN)
                attempt.status = "budget_blocked"
                self.store.save_project(project, event_type="budget.hard_stop", payload={"shot_id": shot.id, "attempt": number})
                raise BudgetExceeded("Estimated next attempt exceeds project budget")
            key = f"{project.id}:{shot.id}:attempt:{number}"
            try:
                self._validate_provider_request(provider, request)
                callback_url = None
                if self.callback_url:
                    callback_url = self.callback_url.format(provider=provider_name, project_id=project.id, shot_id=shot.id)
                job = provider.submit(request, ProviderContext(project.id, shot.id, key, callback_url=callback_url))
                job = self._validate_provider_job(provider, job)
            except Exception as error:
                # Preserve an exact RuntimeError as the worker interruption
                # signal used by the durable lease/recovery path. Adapter
                # subclasses (for example ProviderHTTPError) are normalized.
                if type(error) is RuntimeError:
                    raise
                normalized = self._normalize_provider_error(provider, error)
                attempt.status = "provider_failed"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": number, "error": normalized.code, "message": normalized.message})
                self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                prompt = shot.prompt_bundle or prompt
                continue
            attempt.provider_job = job
            attempt.status = "submitted"
            self.store.append_event(project.id, "shot.submitted", {"shot_id": shot.id, "attempt_id": attempt.id, "job_id": job.external_id, "idempotency_key": key})
            self.store.save_project(project, event_type="shot.submitted.persisted", payload={"shot_id": shot.id, "attempt": number, "job_id": job.external_id})
            result = None
            for _ in range(self.max_poll_cycles):
                try:
                    result = provider.poll(job)
                    result = self._validate_poll_result(provider, result, current_job=job)
                except Exception as error:
                    if type(error) is RuntimeError:
                        raise
                    result = None
                    normalized = self._normalize_provider_error(provider, error)
                    attempt.status = "provider_failed"
                    self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": number, "error": normalized.code, "message": normalized.message})
                    self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                    prompt = shot.prompt_bundle or prompt
                    break
                attempt.provider_job = result.job
                if result.job.status in ("succeeded", "failed", "cancelled"):
                    break
            if result is None or result.job.status != "succeeded":
                attempt.status = "provider_failed"
                reason = result.error.message if result and result.error else "poll_timeout"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": number, "error": result.error.code if result and result.error else "poll_timeout", "message": reason})
                self._apply_provider_failure_repair(project, shot, attempt, provider, reason)
                prompt = shot.prompt_bundle or prompt
                continue
            try:
                artifacts = result.artifacts or provider.fetch_artifacts(job)
                artifacts = self._validate_artifacts(artifacts, provider_name=getattr(provider, "name", None))
            except Exception as error:
                if type(error) is RuntimeError:
                    raise
                normalized = self._normalize_provider_error(provider, error)
                attempt.status = "provider_failed"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": number, "error": normalized.code, "message": normalized.message})
                self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                prompt = shot.prompt_bundle or prompt
                continue
            if not artifacts:
                attempt.status = "provider_failed"
                reason = "Provider reported success without any video artifact"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": number, "error": "missing_artifact", "message": reason})
                self._apply_provider_failure_repair(project, shot, attempt, provider, reason)
                prompt = shot.prompt_bundle or prompt
                continue
            attempt.artifacts = artifacts
            attempt.cost = self._validate_cost(result.cost or estimate, getattr(provider, "name", None))
            # The provider either returns a settled cost or the conservative
            # estimate; both are recorded exactly once for this new attempt.
            self._record_cost(project, attempt.cost)
            self.store.save_project(project, event_type="shot.generated", payload={"shot_id": shot.id, "attempt": number, "cost_usd": attempt.cost.amount_usd})
            project.transition(ProjectStatus.JUDGING)
            judge = self.quality.evaluate(shot, artifacts)
            attempt.judge_result = judge
            attempt.status = "passed" if judge.passed else "judged_failed"
            self.store.save_project(project, event_type="shot.judged", payload={"shot_id": shot.id, "attempt": number, "verdict": judge.verdict.value, "failed_criteria": [item.criterion_id for item in judge.failed_criteria]})
            if judge.passed:
                return artifacts
            attempt.diagnosis = self.quality.diagnose(judge)
            project.transition(ProjectStatus.REPAIRING)
            action = self.quality.choose_repair(
                shot,
                attempt,
                attempt.diagnosis,
                provider_fallback=self._fallback_provider_name(provider),
                max_attempts=attempt_limit,
            )
            attempt.repair_action = action
            self.store.save_project(project, event_type="shot.repair_planned", payload={"shot_id": shot.id, "attempt": number, "kind": action.kind.value if action else None})
            if action is None or action.kind == RepairKind.HUMAN:
                project.transition(ProjectStatus.AWAITING_HUMAN)
                raise HumanGate(f"Shot {shot.sequence} requires human review")
            if action.changes.get("prompt_bundle") is not None:
                shot.prompt_bundle = action.changes["prompt_bundle"]
                prompt = shot.prompt_bundle
            elif action.kind == RepairKind.PROVIDER:
                fallback_name = str(action.changes.get("provider", ""))
                if fallback_name not in self.video_providers:
                    project.transition(ProjectStatus.AWAITING_HUMAN)
                    raise HumanGate("No configured fallback provider is available")
                shot.prompt_bundle = replace(
                    prompt,
                    provider=fallback_name,
                    version=prompt.version + 1,
                    parameters={**prompt.parameters, "attempt": attempt.number + 1},
                )
                prompt = shot.prompt_bundle
            elif action.kind == RepairKind.REFERENCE:
                prompt = self._refresh_references(project, shot, prompt, attempt)
                shot.prompt_bundle = prompt
            elif action.kind == RepairKind.SPLIT_SHOT:
                self._split_shot(project, shot, attempt)
                prompt = shot.prompt_bundle or prompt
        project.transition(ProjectStatus.AWAITING_HUMAN)
        raise HumanGate(f"Shot {shot.sequence} exhausted automatic retries")

    def _resume_attempt(
        self,
        project: Project,
        shot: Shot,
        attempt: Attempt,
        *,
        max_attempts: int | None = None,
    ) -> list[ArtifactRef] | None:
        """Finish an attempt that was persisted before a worker interruption."""
        attempt_limit = self.max_attempts if max_attempts is None else max(1, int(max_attempts))
        artifacts = list(attempt.artifacts)
        result = None
        provider = self._provider_for_prompt(attempt.prompt_bundle)
        if not artifacts and attempt.provider_job is not None:
            for _ in range(self.max_poll_cycles):
                provider = self._provider_for_job(attempt.provider_job)
                try:
                    result = provider.poll(attempt.provider_job)
                    result = self._validate_poll_result(provider, result, current_job=attempt.provider_job)
                except Exception as error:
                    if type(error) is RuntimeError:
                        raise
                    normalized = self._normalize_provider_error(provider, error)
                    attempt.status = "provider_failed"
                    self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": attempt.number, "error": normalized.code, "message": normalized.message})
                    self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                    return None
                attempt.provider_job = result.job
                if result.job.status in ("succeeded", "failed", "cancelled"):
                    break
            if result is None or result.job.status != "succeeded":
                attempt.status = "provider_failed"
                reason = result.error.message if result and result.error else "poll_timeout"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": attempt.number, "error": result.error.code if result and result.error else "poll_timeout", "message": reason})
                self._apply_provider_failure_repair(project, shot, attempt, provider, reason)
                return None
            try:
                artifacts = result.artifacts or provider.fetch_artifacts(attempt.provider_job)
                artifacts = self._validate_artifacts(artifacts, provider_name=getattr(provider, "name", None))
            except Exception as error:
                if type(error) is RuntimeError:
                    raise
                normalized = self._normalize_provider_error(provider, error)
                attempt.status = "provider_failed"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": attempt.number, "error": normalized.code, "message": normalized.message})
                self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                return None
            if not artifacts:
                attempt.status = "provider_failed"
                reason = "Provider reported success without any video artifact"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": attempt.number, "error": "missing_artifact", "message": reason})
                self._apply_provider_failure_repair(project, shot, attempt, provider, reason)
                return None
            attempt.artifacts = artifacts
            if attempt.cost is None:
                try:
                    attempt.cost = self._validate_cost(
                        result.cost or provider.estimate_cost(self._video_request(project, shot, attempt.prompt_bundle)),
                        getattr(provider, "name", None),
                    )
                except Exception as error:  # noqa: BLE001 - malformed pricing fails closed
                    normalized = self._normalize_provider_error(provider, error)
                    attempt.status = "provider_failed"
                    self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": attempt.number, "error": normalized.code, "message": normalized.message})
                    self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                    return None
                self._record_cost(project, attempt.cost)
            self.store.save_project(project, event_type="shot.generated.resumed", payload={"shot_id": shot.id, "attempt": attempt.number, "cost_usd": attempt.cost.amount_usd})
        elif not artifacts:
            # A crash before submit left a durable placeholder; let the normal
            # loop submit that same numbered attempt with its idempotency key.
            return None
        elif attempt.cost is None:
            # A crash can occur after artifact persistence but before the cost
            # write. Settle that attempt before judging so accounting remains
            # idempotent across process restarts.
            provider = self._provider_for_prompt(attempt.prompt_bundle)
            try:
                attempt.cost = self._validate_cost(
                    provider.estimate_cost(self._video_request(project, shot, attempt.prompt_bundle)),
                    getattr(provider, "name", None),
                )
            except Exception as error:  # noqa: BLE001 - malformed pricing fails closed
                normalized = self._normalize_provider_error(provider, error)
                attempt.status = "provider_failed"
                self.store.save_project(project, event_type="shot.provider_failed", payload={"shot_id": shot.id, "attempt": attempt.number, "error": normalized.code, "message": normalized.message})
                self._apply_provider_failure_repair(project, shot, attempt, provider, normalized.message)
                return None
            self._record_cost(project, attempt.cost)
            self.store.save_project(project, event_type="shot.cost.reconciled", payload={"shot_id": shot.id, "attempt": attempt.number, "cost_usd": attempt.cost.amount_usd})
        if attempt.judge_result is None:
            project.transition(ProjectStatus.JUDGING)
            judge = self.quality.evaluate(shot, artifacts)
            attempt.judge_result = judge
            attempt.status = "passed" if judge.passed else "judged_failed"
            self.store.save_project(project, event_type="shot.judged.resumed", payload={"shot_id": shot.id, "attempt": attempt.number, "verdict": judge.verdict.value, "failed_criteria": [item.criterion_id for item in judge.failed_criteria]})
            if judge.passed:
                return artifacts
            attempt.diagnosis = self.quality.diagnose(judge)
            project.transition(ProjectStatus.REPAIRING)
            action = self.quality.choose_repair(
                shot,
                attempt,
                attempt.diagnosis,
                provider_fallback=self._fallback_provider_name(provider),
                max_attempts=attempt_limit,
            )
            attempt.repair_action = action
            self.store.save_project(project, event_type="shot.repair_planned", payload={"shot_id": shot.id, "attempt": attempt.number, "kind": action.kind.value if action else None})
            if action is None or action.kind == RepairKind.HUMAN:
                project.transition(ProjectStatus.AWAITING_HUMAN)
                raise HumanGate(f"Shot {shot.sequence} requires human review")
            if action.changes.get("prompt_bundle") is not None:
                shot.prompt_bundle = action.changes["prompt_bundle"]
            elif action.kind == RepairKind.PROVIDER:
                fallback_name = str(action.changes.get("provider", ""))
                if fallback_name not in self.video_providers:
                    project.transition(ProjectStatus.AWAITING_HUMAN)
                    raise HumanGate("No configured fallback provider is available")
                shot.prompt_bundle = replace(
                    attempt.prompt_bundle,
                    provider=fallback_name,
                    version=attempt.prompt_bundle.version + 1,
                    parameters={**attempt.prompt_bundle.parameters, "attempt": attempt.number + 1},
                )
            elif action.kind == RepairKind.REFERENCE:
                shot.prompt_bundle = self._refresh_references(project, shot, attempt.prompt_bundle, attempt)
            elif action.kind == RepairKind.SPLIT_SHOT:
                self._split_shot(project, shot, attempt)
        elif attempt.status == "passed":
            return artifacts
        return None

    def _provider_for_prompt(self, prompt) -> VideoGeneratorProvider:
        name = prompt.provider if prompt is not None else None
        if name is None:
            return self.video_provider
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Prompt bundle has an invalid video provider name")
        provider = self.video_providers.get(name.strip())
        if provider is None:
            raise ValueError(f"Unknown video provider in prompt bundle: {name}")
        return provider

    def _record_cost(self, project: Project, cost: CostRecord) -> None:
        """Settle a cost once and emit the configured 80% soft warning."""
        self._validate_cost(cost, cost.provider)
        before = project.total_cost_usd
        project.add_cost(cost)
        budget = project.brief.budget_usd
        if budget > 0 and before / budget < 0.8 <= project.total_cost_usd / budget:
            self.store.save_project(project, event_type="budget.soft_warning", payload={"spent_usd": project.total_cost_usd, "budget_usd": budget, "utilization": project.total_cost_usd / budget})

    def _provider_for_job(self, job) -> VideoGeneratorProvider:
        name = getattr(job, "provider", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Provider job is missing a provider name")
        provider = self.video_providers.get(name)
        if provider is None:
            raise ValueError(f"Unknown video provider in persisted job: {name}")
        return provider

    def _fallback_provider_name(self, current: VideoGeneratorProvider) -> str | None:
        current_name = getattr(current, "name", None)
        return next((name for name in self.video_providers if name != current_name), None)

    @staticmethod
    def _normalize_provider_error(provider: VideoGeneratorProvider, error: Exception):
        normalizer = getattr(provider, "normalize_error", None)
        if callable(normalizer):
            try:
                normalized = normalizer(error)
                if DirectorOrchestrator._is_valid_provider_error(normalized, provider):
                    return normalized
            except Exception as normalization_error:  # noqa: BLE001 - adapters must not break retry policy
                # A broken normalizer must never hide the original provider
                # failure; fall back to the generic contract below.
                _ = normalization_error
        return ProviderError("provider_error", str(error), retryable=True, provider=getattr(provider, "name", None))

    @staticmethod
    def _is_valid_provider_error(error: Any, provider: VideoGeneratorProvider | None = None) -> bool:
        """Validate adapter errors before they enter the event/repair policy."""
        if not isinstance(error, ProviderError):
            return False
        if not isinstance(error.code, str) or not error.code.strip():
            return False
        if not isinstance(error.message, str) or not error.message.strip():
            return False
        if not isinstance(error.retryable, bool):
            return False
        if error.provider is not None and not isinstance(error.provider, str):
            return False
        provider_name = getattr(provider, "name", None) if provider is not None else None
        if provider_name and error.provider and error.provider != provider_name:
            return False
        return isinstance(error.details, dict)

    @staticmethod
    def _normalize_job_status(value: Any) -> str | None:
        status = str(value).strip().lower() if value is not None else ""
        return {
            "completed": "succeeded",
            "success": "succeeded",
            "done": "succeeded",
            "error": "failed",
        }.get(status, status) if status in {"queued", "submitted", "running", "succeeded", "failed", "cancelled", "completed", "success", "done", "error"} else None

    @classmethod
    def _merge_callback_job(cls, provider: VideoGeneratorProvider, current: Any, incoming: Any):
        """Apply callback updates monotonically so late webhooks cannot reopen a job."""
        normalized = cls._validate_provider_job(provider, incoming)
        if current is None:
            return normalized
        current_status = cls._normalize_job_status(getattr(current, "status", "queued")) or "queued"
        incoming_status = normalized.status
        rank = {"queued": 0, "submitted": 1, "running": 2, "succeeded": 3, "failed": 3, "cancelled": 3}
        if rank[incoming_status] < rank[current_status]:
            return replace(current, status=current_status)
        if current_status in {"succeeded", "failed", "cancelled"} and incoming_status != current_status:
            return replace(current, status=current_status)
        return normalized

    @classmethod
    def _validate_provider_job(cls, provider: VideoGeneratorProvider, job: Any):
        if not isinstance(job, ProviderJob):
            raise TypeError("provider returned an invalid job object")
        if not hasattr(job, "provider") or not hasattr(job, "external_id") or not hasattr(job, "status"):
            raise ValueError("provider submit returned an invalid job object")
        provider_name = getattr(provider, "name", None)
        if not isinstance(job.provider, str) or not job.provider.strip() or (provider_name and job.provider != provider_name):
            raise ValueError("provider submit returned a job for a different provider")
        if not isinstance(job.external_id, str) or not job.external_id.strip():
            raise ValueError("provider submit returned a job without an external id")
        if job.idempotency_key is not None and (not isinstance(job.idempotency_key, str) or not job.idempotency_key.strip()):
            raise TypeError("provider job idempotency key must be a non-empty string")
        if not isinstance(job.submitted_at, datetime):
            raise TypeError("provider job submitted_at must be a datetime")
        if not isinstance(job.metadata, dict):
            raise TypeError("provider job metadata must be an object")
        status = cls._normalize_job_status(job.status)
        if status is None:
            raise ValueError(f"provider returned an unsupported job status: {job.status!r}")
        return replace(job, external_id=job.external_id.strip(), status=status, metadata=dict(job.metadata))

    @classmethod
    def _validate_cost(cls, cost: Any, provider_name: str | None = None) -> CostRecord:
        if not isinstance(cost, CostRecord):
            raise TypeError("provider returned an invalid cost record")
        amount = cost.amount_usd
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)) or float(amount) < 0:
            raise ValueError("provider cost must be a finite non-negative number")
        if provider_name and cost.provider and cost.provider != provider_name:
            raise ValueError("provider cost identifies a different provider")
        if not isinstance(cost.currency, str) or not cost.currency.strip():
            raise ValueError("provider cost currency must be a non-empty string")
        if cost.provider is not None and not isinstance(cost.provider, str):
            raise ValueError("provider cost provider must be a string")
        if cost.model is not None and not isinstance(cost.model, str):
            raise ValueError("provider cost model must be a string")
        if not isinstance(cost.estimated, bool):
            raise TypeError("provider cost estimated flag must be a boolean")
        if not isinstance(cost.id, str) or not cost.id.strip():
            raise ValueError("provider cost must have a non-empty id")
        return cost

    @classmethod
    def _validate_artifacts(cls, artifacts: Any, *, provider_name: str | None = None) -> list[ArtifactRef]:
        if not isinstance(artifacts, (list, tuple)):
            raise TypeError("provider returned artifacts in an invalid shape")
        normalized: list[ArtifactRef] = []
        for artifact in artifacts:
            if not isinstance(artifact, ArtifactRef):
                raise TypeError("provider returned a malformed artifact")
            if artifact.kind != AssetKind.VIDEO:
                raise ValueError("video provider returned a non-video artifact")
            if not isinstance(artifact.uri, str) or not artifact.uri.strip():
                raise ValueError("provider returned an artifact without a URI")
            if artifact.duration_seconds is not None and (
                isinstance(artifact.duration_seconds, bool)
                or not isinstance(artifact.duration_seconds, (int, float))
                or not math.isfinite(float(artifact.duration_seconds))
                or float(artifact.duration_seconds) < 0
            ):
                raise ValueError("provider artifact duration must be a finite non-negative number")
            if artifact.sha256 is not None and not isinstance(artifact.sha256, str):
                raise TypeError("provider artifact sha256 must be a string")
            if artifact.mime_type is not None and not isinstance(artifact.mime_type, str):
                raise TypeError("provider artifact mime_type must be a string")
            if not isinstance(artifact.metadata, dict):
                raise TypeError("provider artifact metadata must be an object")
            if not isinstance(artifact.id, str) or not artifact.id.strip():
                raise ValueError("provider artifact must have a non-empty id")
            metadata = dict(artifact.metadata)
            if provider_name and metadata.get("provider") is None:
                metadata["provider"] = provider_name
            normalized.append(replace(artifact, uri=artifact.uri.strip(), metadata=metadata))
        return normalized

    @classmethod
    def _validate_poll_result(cls, provider: VideoGeneratorProvider, result: Any, *, current_job) -> Any:
        if not isinstance(result, PollResult):
            raise TypeError("provider poll returned an invalid result object")
        if not isinstance(current_job, ProviderJob):
            raise TypeError("current provider job is malformed")
        current_job = cls._validate_provider_job(provider, current_job)
        job = cls._validate_provider_job(provider, result.job)
        if job.external_id != current_job.external_id:
            raise ValueError("provider poll returned a different job")
        artifacts = cls._validate_artifacts(result.artifacts, provider_name=getattr(provider, "name", None))
        cost = cls._validate_cost(result.cost, getattr(provider, "name", None)) if result.cost is not None else None
        error = result.error
        if error is not None and not cls._is_valid_provider_error(error, provider):
            raise TypeError("provider poll returned a malformed error")
        return replace(result, job=job, artifacts=artifacts, cost=cost)

    @staticmethod
    def _validate_provider_request(provider: VideoGeneratorProvider, request: VideoRequest) -> None:
        capabilities = provider.capabilities()
        aspect_ratio = str(request.parameters.get("aspect_ratio", "16:9"))
        if capabilities.supported_aspect_ratios and aspect_ratio not in capabilities.supported_aspect_ratios:
            raise ValueError(f"provider does not support aspect ratio {aspect_ratio}")
        if capabilities.max_duration_seconds is not None and request.shot.duration_seconds > capabilities.max_duration_seconds:
            raise ValueError(f"provider maximum duration is {capabilities.max_duration_seconds} seconds")
        if request.parameters.get("audio_required") and not capabilities.supported_audio:
            raise ValueError("provider does not support requested audio")

    def _apply_provider_failure_repair(self, project: Project, shot: Shot, attempt: Attempt, provider: VideoGeneratorProvider, reason: str) -> None:
        """Switch providers after a repeated provider-side failure.

        A transient first failure keeps the same provider so adapters can retry
        their own queue. Once the next attempt is reached, a configured
        alternative is cheaper than repeatedly submitting a known-bad job.
        """
        fallback = self._fallback_provider_name(provider)
        if attempt.number < 2 or not fallback:
            return
        current = attempt.prompt_bundle
        updated = replace(current, provider=fallback, version=current.version + 1, parameters={**current.parameters, "attempt": attempt.number + 1})
        shot.prompt_bundle = updated
        action = RepairAction(shot.id, RepairKind.PROVIDER, {"provider": fallback}, f"Switch provider after repeated failure: {reason}", attempt_number=attempt.number + 1)
        attempt.repair_action = action
        self.store.save_project(project, event_type="shot.repair_planned", payload={"shot_id": shot.id, "attempt": attempt.number, "kind": action.kind.value, "provider": fallback})

    def _split_shot(self, project: Project, shot: Shot, attempt: Attempt) -> Shot:
        """Split one shot into two linked shots while preserving provenance."""
        plan = project.active_plan
        if plan is None:
            return shot
        half = max(0.5, shot.duration_seconds / 2)
        old_next_id = shot.next_shot_id
        original_prompt = shot.prompt_bundle
        if original_prompt is not None:
            shot.prompt_bundle = replace(
                original_prompt,
                id=new_id("prompt"),
                version=original_prompt.version + 1,
                parameters={**original_prompt.parameters, "duration_seconds": half, "attempt": attempt.number + 1},
            )
        shot.acceptance_criteria = [replace(criterion, id=new_id("criterion")) for criterion in shot.acceptance_criteria]
        shot.duration_seconds = half
        shot.description = f"{shot.description} (segment 1 of 2)"
        shot.title = f"{shot.title} / A"
        second_prompt = replace(
            shot.prompt_bundle or PromptBundle(positive=shot.description),
            id=new_id("prompt"),
            version=(shot.prompt_bundle.version + 1 if shot.prompt_bundle else 1),
            parameters={**(shot.prompt_bundle.parameters if shot.prompt_bundle else {}), "duration_seconds": half, "attempt": 1},
        )
        second_criteria = [replace(criterion, id=new_id("criterion")) for criterion in shot.acceptance_criteria]
        second = replace(
            shot,
            sequence=shot.sequence + 1,
            title=shot.title.rsplit(" / A", 1)[0] + " / B",
            description=shot.description.replace(" (segment 1 of 2)", " (segment 2 of 2)"),
            duration_seconds=half,
            previous_shot_id=shot.id,
            next_shot_id=old_next_id,
            acceptance_criteria=second_criteria,
            prompt_bundle=second_prompt,
            depends_on_shot_ids=[shot.id],
            id=new_id("shot"),
        )
        shot.next_shot_id = second.id
        index = plan.shots.index(shot)
        plan.shots.insert(index + 1, second)
        for item in plan.shots[index + 2:]:
            item.sequence += 1
            if item.previous_shot_id == shot.id:
                item.previous_shot_id = second.id
            if shot.id in item.depends_on_shot_ids:
                item.depends_on_shot_ids = [second.id if dep == shot.id else dep for dep in item.depends_on_shot_ids]
        if old_next_id:
            successor = next((item for item in plan.shots if item.id == old_next_id), None)
            if successor is not None:
                successor.previous_shot_id = second.id
                successor.depends_on_shot_ids = [second.id if dep == shot.id else dep for dep in successor.depends_on_shot_ids]
        for scene in plan.scenes:
            if shot.id in scene.shot_ids:
                scene.shot_ids.insert(scene.shot_ids.index(shot.id) + 1, second.id)
                break
        self.store.save_project(project, event_type="shot.split", payload={"shot_id": shot.id, "new_shot_id": second.id, "attempt": attempt.number, "duration_seconds": half})
        return second

    def _refresh_references(self, project: Project, shot: Shot, prompt, attempt: Attempt):
        """Resolve fresh source-of-truth references for a repair attempt."""
        plan = project.active_plan
        if plan is None:
            return replace(prompt, version=prompt.version + 1, parameters={**prompt.parameters, "reference_refresh_nonce": attempt.id, "attempt": attempt.number + 1})
        assets = {asset.id: asset for asset in plan.reference_assets}
        for asset_id in prompt.reference_asset_ids:
            asset = assets.get(asset_id)
            if asset is None:
                continue
            if self.reference_provider is not None:
                resolved = self.reference_provider.resolve(asset.uri, project_id=project.id)
                asset.uri = resolved.uri
                asset.sha256 = resolved.sha256
                asset.provider = getattr(self.reference_provider, "name", None)
                asset.metadata["last_refreshed_for_attempt"] = attempt.id
        return replace(
            prompt,
            version=prompt.version + 1,
            parameters={**prompt.parameters, "reference_refresh_nonce": attempt.id, "attempt": attempt.number + 1},
        )

    def retry_shot(self, project: Project, shot_id: str, *, actor: str = "human", force: bool = False) -> Project:
        """Request a bounded retry for one shot from a paused project."""
        plan = project.active_plan
        if plan is None:
            raise HumanGate("No plan exists")
        shot = next((item for item in plan.shots if item.id == shot_id), None)
        if shot is None:
            raise KeyError(shot_id)
        if project.status not in {ProjectStatus.AWAITING_HUMAN, ProjectStatus.PLANNED, ProjectStatus.FAILED, ProjectStatus.DELIVERED}:
            raise HumanGate(f"Shot retry is not allowed while project is {project.status.value}")
        if project.status == ProjectStatus.DELIVERED and not force:
            raise HumanGate("Delivered projects require an explicit force retry")
        project.transition(ProjectStatus.GENERATING, force=force)
        self.store.save_project(project, event_type="shot.retry_requested", payload={"shot_id": shot_id, "actor": actor})
        # A manual retry starts a fresh bounded wave while preserving all prior
        # attempts. This avoids reusing an idempotency key from an old attempt.
        prior_numbers = [attempt.number for attempt in project.attempts if attempt.shot_id == shot.id]
        attempt_limit = max(self.max_attempts, max(prior_numbers, default=0) + 1)
        self._run_shot(project, shot, max_attempts=attempt_limit)
        return self._assemble_project(project, self._all_passed_artifacts(project))

    @staticmethod
    def _latest_passed_attempt(project: Project, shot_id: str) -> Attempt:
        attempts = [attempt for attempt in project.attempts if attempt.shot_id == shot_id and attempt.status == "passed" and attempt.artifacts]
        if not attempts:
            raise LookupError(f"No passed attempt for shot {shot_id}")
        return max(attempts, key=lambda attempt: attempt.number)

    @classmethod
    def _passed_artifacts(cls, project: Project, shot_id: str) -> list[ArtifactRef]:
        try:
            return list(cls._latest_passed_attempt(project, shot_id).artifacts)
        except LookupError:
            return []

    @classmethod
    def _all_passed_artifacts(cls, project: Project) -> dict[str, list[ArtifactRef]]:
        # Only the active plan can be assembled. Historical attempts from an
        # earlier plan remain useful for audit, but must never leak into a new
        # delivery wave.
        shot_ids = [shot.id for shot in (project.active_plan.shots if project.active_plan else [])]
        return {shot_id: cls._passed_artifacts(project, shot_id) for shot_id in shot_ids}

    @staticmethod
    def _active_delivery(project: Project) -> ArtifactRef | None:
        """Return the newest non-superseded delivery artifact.

        Delivery artifacts are append-only so an operator can inspect every
        version.  The active marker is metadata rather than a second mutable
        pointer, which keeps old snapshots backwards compatible.
        """
        deliveries = []
        for artifact in project.artifacts:
            if artifact.kind != AssetKind.VIDEO or not isinstance(artifact.metadata, dict):
                continue
            metadata = artifact.metadata
            if metadata.get("artifact_role") != "delivery":
                continue
            # Metadata is persisted JSON and can be malformed after a partial
            # write or a hand-edited snapshot.  Treat non-boolean lineage
            # markers as invalid instead of accepting truthy strings/numbers.
            active = metadata.get("active", True)
            superseded = metadata.get("superseded", False)
            if not isinstance(active, bool) or not isinstance(superseded, bool):
                continue
            if active is False or superseded is True:
                continue
            deliveries.append(artifact)
        if not deliveries:
            return None
        return max(
            deliveries,
            key=lambda artifact: (
                artifact.metadata.get("delivery_version", 0)
                if isinstance(artifact.metadata.get("delivery_version", 0), int)
                and not isinstance(artifact.metadata.get("delivery_version", 0), bool)
                else 0,
                project.artifacts.index(artifact),
            ),
        )

    def _assemble_project(self, project: Project, artifacts_by_shot: dict[str, list[ArtifactRef]]) -> Project:
        shots = project.active_plan.shots if project.active_plan else []
        continuity = self.continuity.check(project, shots, artifacts_by_shot)
        if not continuity.passed:
            project.transition(ProjectStatus.AWAITING_HUMAN)
            self.store.save_project(project, event_type="continuity.failed", payload={"issues": [issue.message for issue in continuity.issues]})
            raise HumanGate("Cross-shot continuity requires human review")
        project.transition(ProjectStatus.ASSEMBLING)
        all_video = [artifact for shot in sorted(shots, key=lambda item: item.sequence) for artifact in artifacts_by_shot.get(shot.id, ()) if artifact.kind.value == "video"]
        if not all_video:
            project.transition(ProjectStatus.AWAITING_HUMAN)
            self.store.save_project(project, event_type="assembly.failed", payload={"reason": "no_video_artifacts"})
            raise HumanGate("No passed video artifacts are available for assembly")
        try:
            audio = self._audio_for_project(project)
            previous_delivery = self._active_delivery(project)
            previous_version = (
                previous_delivery.metadata.get("delivery_version", 0)
                if previous_delivery and isinstance(previous_delivery.metadata, dict)
                else 0
            )
            if isinstance(previous_version, bool) or not isinstance(previous_version, int) or previous_version < 0:
                previous_version = 0
            delivery_metadata = {
                "project_id": project.id,
                "plan_id": project.active_plan.id if project.active_plan else None,
                "artifact_role": "delivery",
                "delivery_version": previous_version + 1,
                "active": True,
            }
            if previous_delivery is not None:
                delivery_metadata["supersedes"] = previous_delivery.id
            final = self.assembler.assemble(all_video, audio, metadata=delivery_metadata)
        except Exception as error:
            project.transition(ProjectStatus.AWAITING_HUMAN)
            self.store.save_project(project, event_type="assembly.failed", payload={"reason": str(error), "video_count": len(all_video)})
            raise HumanGate(f"Assembly requires human review: {error}") from error
        if (
            not isinstance(final, ArtifactRef)
            or final.kind != AssetKind.VIDEO
            or not isinstance(final.uri, str)
            or not final.uri.strip()
            or not isinstance(final.metadata, dict)
        ):
            project.transition(ProjectStatus.AWAITING_HUMAN)
            self.store.save_project(project, event_type="assembly.failed", payload={"reason": "assembler_returned_invalid_artifact", "video_count": len(all_video)})
            raise HumanGate("Assembler returned an invalid delivery artifact")
        # Assemblers may add their own metadata, but the delivery lineage is
        # owned by the orchestrator and must be authoritative.
        final.metadata = {**dict(final.metadata), "artifact_role": "delivery", "delivery_version": delivery_metadata["delivery_version"], "active": True}
        if previous_delivery is not None:
            final.metadata["supersedes"] = previous_delivery.id
            previous_delivery.metadata = {
                **dict(previous_delivery.metadata),
                "active": False,
                "superseded": True,
                "superseded_by": final.id,
            }
        project.artifacts.append(final)
        project.transition(ProjectStatus.AWAITING_HUMAN)
        self.store.save_project(project, event_type="assembly.ready", payload={"artifact_id": final.id, "video_count": len(all_video)})
        return project

    @staticmethod
    def _video_request(project: Project, shot: Shot, prompt):
        plan = project.active_plan
        references = {asset.id: asset.uri for asset in (plan.reference_assets if plan else [])}
        reference_uris = [references[item] for item in prompt.reference_asset_ids if item in references]
        continuity = {
            "character_signature": "|".join(shot.character_ids),
            "location_signature": shot.location_id or "",
            "style_signature": plan.style_bible.id if plan and plan.style_bible else "",
        }
        parameters = {**prompt.parameters, "_continuity_signatures": continuity}
        parameters.setdefault("audio_required", project.brief.audio_required)
        return VideoRequest(shot, prompt.positive, prompt.negative, parameters, reference_uris, prompt.model)

    def _audio_for_project(self, project: Project) -> list[ArtifactRef]:
        if not self.audio_provider or not project.brief.audio_required:
            return []
        cues = project.active_plan.audio_cues if project.active_plan else []
        if not cues:
            cues = [AudioCue("narration", project.brief.request, 0.0, project.brief.duration_seconds, voice="narrator")]
        provider_name = getattr(self.audio_provider, "name", self.audio_provider.__class__.__name__)
        cached: list[ArtifactRef] = []
        generated: list[ArtifactRef] = []
        for cue in cues:
            request = AudioRequest(
                cue.text,
                voice=cue.voice,
                duration_seconds=cue.duration_seconds,
                kind=cue.kind,
                parameters=cue.parameters,
            )
            cache_key = stable_hash({
                "text": request.text,
                "voice": request.voice,
                "duration_seconds": request.duration_seconds,
                "kind": request.kind,
                "parameters": request.parameters,
                "start_seconds": cue.start_seconds,
                "provider": provider_name,
                "provider_version": getattr(self.audio_provider, "version", None),
            })
            hit = next(
                (artifact for artifact in project.artifacts
                 if artifact.kind in {AssetKind.AUDIO, AssetKind.SUBTITLE}
                 and isinstance(artifact.metadata, dict)
                 and artifact.metadata.get("artifact_role") == "audio_cache"
                 and artifact.metadata.get("cache_key") == cache_key),
                None,
            )
            if hit is not None:
                cached.append(hit)
                continue
            artifact = self.audio_provider.synthesize(
                request,
                ProviderContext(project.id, metadata={"start_seconds": cue.start_seconds, "cue_id": cue.id, "cache_key": cache_key}),
            )
            if not isinstance(artifact, ArtifactRef) or artifact.kind not in {AssetKind.AUDIO, AssetKind.SUBTITLE}:
                raise TypeError("audio provider returned an invalid artifact")
            if not isinstance(artifact.uri, str) or not artifact.uri.strip():
                raise ValueError("audio provider returned an artifact without a URI")
            if not isinstance(artifact.metadata, dict):
                raise TypeError("audio provider artifact metadata must be an object")
            artifact.metadata = {
                **artifact.metadata,
                "artifact_role": "audio_cache",
                "cache_key": cache_key,
                "provider": provider_name,
                "cue_id": cue.id,
                "start_seconds": cue.start_seconds,
            }
            project.artifacts.append(artifact)
            generated.append(artifact)
            cached.append(artifact)
        if generated:
            self.store.save_project(project, event_type="audio.generated", payload={"artifact_ids": [item.id for item in generated], "reused_count": len(cached) - len(generated)})
        elif cached:
            self.store.append_event(project.id, "audio.reused", {"artifact_ids": [item.id for item in cached]})
        return cached
