"""Issue dependencies: what waits, what is refused (design: Issue dependencies)."""

from concert.core.dependencies import (
    Dependency,
    dependency_problem,
    parse_dependency_arg,
    unmet,
)
from concert.core.transitions import Status

START, MERGE = "start", "merge"


def test_arguments_name_an_issue_and_an_optional_kind() -> None:
    assert parse_dependency_arg("#12") == (12, START)  # code prerequisites are the default
    assert parse_dependency_arg("#12 merge") == (12, MERGE)
    assert parse_dependency_arg("#12 start") == (12, START)
    assert parse_dependency_arg("  #12   MERGE ") == (12, MERGE)


def test_malformed_arguments_are_refused() -> None:
    for bad in ("", "12", "#x", "#12 sometime", "#12 merge extra", "#0"):
        assert parse_dependency_arg(bad) is None


def test_only_dependencies_on_unfinished_issues_are_unmet() -> None:
    deps = (Dependency(5, START), Dependency(6, MERGE))
    statuses = {5: Status.ON_STAGING, 6: Status.IN_REVIEW}
    assert [d.issue for d in unmet(deps, statuses, kind=None)] == [6]
    assert unmet(deps, statuses, kind=START) == []
    assert [d.issue for d in unmet(deps, statuses, kind=MERGE)] == [6]


def test_an_issue_not_seen_yet_counts_as_unmet() -> None:
    assert [d.issue for d in unmet((Dependency(5, START),), {}, kind=None)] == [5]


def test_a_dependency_on_an_issue_closed_without_merging_stays_unmet() -> None:
    statuses = {5: Status.CLOSED, 6: Status.CLOSED_DUPLICATE, 7: Status.BLOCKED}
    deps = tuple(Dependency(n, START) for n in (5, 6, 7))
    assert len(unmet(deps, statuses, kind=None)) == 3


def problem(
    issue: int = 1,
    target: int = 2,
    status: Status = Status.SPEC,
    graph: dict[int, frozenset[int]] | None = None,
    exists: bool = True,
) -> str | None:
    return dependency_problem(issue, target, status, graph or {}, exists)


def test_a_sound_dependency_has_no_problem() -> None:
    assert problem() is None


def test_an_issue_cannot_depend_on_itself() -> None:
    assert "itself" in (problem(target=1) or "")


def test_the_target_must_exist() -> None:
    assert "does not exist" in (problem(exists=False) or "")


def test_a_cycle_is_refused() -> None:
    graph = {2: frozenset({3}), 3: frozenset({1})}
    assert "cycle" in (problem(graph=graph) or "")


def test_a_long_chain_without_a_cycle_is_fine() -> None:
    graph = {2: frozenset({3}), 3: frozenset({4})}
    assert problem(graph=graph) is None


def test_dependencies_can_only_be_added_before_the_merge() -> None:
    for status in (Status.INTEGRATING, Status.VERIFYING_STAGING, Status.ON_STAGING):
        assert "merge" in (problem(status=status) or "")
    for status in (Status.INBOX, Status.TRIAGE, Status.SPEC, Status.IMPLEMENTING, Status.HUMAN_UAT):
        assert problem(status=status) is None
