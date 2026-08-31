"""Dependency-aware scheduling primitives for Scene/Shot execution."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from .schemas import Shot


class DependencyError(ValueError):
    """Raised when a shot dependency graph cannot make progress."""


def ready_shots(
    shots: Sequence[Shot],
    completed_ids: Iterable[str] = (),
) -> list[Shot]:
    """Return shots whose explicit dependencies have passed.

    The function is intentionally pure so API schedulers, workers and tests can
    share the same readiness rule. Sequence order is used only as a stable
    tie-breaker; a shot with no dependency is eligible even when an earlier
    sequence is still running.
    """
    completed = {item for item in completed_ids if isinstance(item, str)}
    known = {shot.id for shot in shots}
    result: list[Shot] = []
    for shot in sorted(shots, key=lambda item: item.sequence):
        if shot.id in completed:
            continue
        dependencies = shot.depends_on_shot_ids
        if not isinstance(dependencies, list):
            raise DependencyError(f"shot {shot.id} has malformed dependencies")
        missing = [dependency for dependency in dependencies if dependency not in known]
        if missing:
            raise DependencyError(f"shot {shot.id} has missing dependencies: {missing}")
        if all(dependency in completed for dependency in dependencies):
            result.append(shot)
    return result


def dependency_waves(shots: Sequence[Shot], completed_ids: Iterable[str] = ()) -> list[list[Shot]]:
    """Build deterministic topological waves for a finite Shot DAG."""
    known = {shot.id for shot in shots}
    completed = {item for item in completed_ids if isinstance(item, str)}
    if not completed.issubset(known):
        completed &= known
    waves: list[list[Shot]] = []
    while len(completed) < len(known):
        wave = ready_shots(shots, completed)
        if not wave:
            unresolved = sorted(known - completed)
            raise DependencyError(f"shot dependency graph has no ready node: {unresolved}")
        waves.append(wave)
        completed.update(shot.id for shot in wave)
    return waves


__all__ = ["DependencyError", "dependency_waves", "ready_shots"]
