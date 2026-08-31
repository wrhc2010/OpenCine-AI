from __future__ import annotations

import pytest

from video_director.dependencies import DependencyError, dependency_waves, ready_shots
from video_director.schemas import Shot


def make_shot(sequence: int, *, depends_on: list[str] | None = None) -> Shot:
    return Shot(
        sequence,
        "scene",
        f"shot-{sequence}",
        f"description-{sequence}",
        depends_on_shot_ids=list(depends_on or []),
    )


def test_ready_shots_allows_independent_work_and_respects_dependencies():
    first = make_shot(1)
    second = make_shot(2)
    dependent = make_shot(3, depends_on=[first.id])
    shots = [dependent, second, first]

    assert [shot.id for shot in ready_shots(shots)] == [first.id, second.id]
    assert [shot.id for shot in ready_shots(shots, {first.id})] == [second.id, dependent.id]
    assert [[shot.id for shot in wave] for wave in dependency_waves(shots)] == [
        [first.id, second.id],
        [dependent.id],
    ]


def test_dependency_helpers_fail_closed_for_missing_dependency_and_cycle():
    missing = make_shot(1, depends_on=["missing"])
    with pytest.raises(DependencyError, match="missing dependencies"):
        ready_shots([missing])

    first = make_shot(1)
    second = make_shot(2, depends_on=[first.id])
    first.depends_on_shot_ids = [second.id]
    with pytest.raises(DependencyError, match="no ready node"):
        dependency_waves([first, second])


def test_dependency_waves_ignores_unknown_completed_ids_but_not_unknown_edges():
    first = make_shot(1)
    second = make_shot(2, depends_on=[first.id])
    waves = dependency_waves([first, second], {"stale-shot"})
    assert waves[0] == [first]
    assert waves[1] == [second]
