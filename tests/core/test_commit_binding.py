"""Commit binding, the between-runs check and recovery (design: "Branch protection and merge paths")."""

import pytest

from concert.core.commit_binding import (
    Bound,
    CommitMeta,
    ForeignCommits,
    InheritedRejected,
    InheritedValid,
    ResetPlan,
    UnexpectedPush,
    check_between_runs,
    check_commit_binding,
    expected_head,
    plan_recovery,
    validate_inherited_range,
)
from concert.core.handoff import Handoff, parse_handoff

AGENT = "concert-vm1[bot]"


def run(run_id: str, commits: list[str], rebases: dict[str, str] | None = None) -> Handoff:
    return parse_handoff(
        {
            "schema": 1,
            "state": "implementing",
            "run_id": run_id,
            "runner": "vm-1",
            "exit": "done",
            "commit_sha": commits[-1] if commits else None,
            "artifact_rev": "spec1",
            "run_commits": commits,
            "rebases": rebases or {},
        }
    )


def test_commits_produced_by_accepted_runs_are_bound() -> None:
    runs = [run("r1", ["c1", "c2"]), run("r2", ["c3"])]
    assert check_commit_binding(["c1", "c2", "c3"], runs) == Bound()


def test_an_empty_range_is_bound() -> None:
    assert check_commit_binding([], []) == Bound()


def test_a_commit_no_run_produced_is_foreign_and_all_foreign_commits_are_listed_in_order() -> None:
    result = check_commit_binding(["c1", "x1", "c2", "x2"], [run("r1", ["c1", "c2"])])
    assert result == ForeignCommits(("x1", "x2"))


def test_a_rebase_product_of_bound_commits_is_bound() -> None:
    runs = [run("r1", ["c1", "c2"]), run("r2", [], rebases={"c1": "c1r", "c2": "c2r"})]
    assert check_commit_binding(["c1r", "c2r"], runs) == Bound()


def test_rebases_chain_transitively() -> None:
    runs = [
        run("r1", ["c1"]),
        run("r2", [], rebases={"c1": "c1r"}),
        run("r3", [], rebases={"c1r": "c1rr"}),
    ]
    assert check_commit_binding(["c1rr"], runs) == Bound()


def test_a_rebase_mapping_from_an_unbound_commit_does_not_launder_it() -> None:
    runs = [run("r1", ["c1"], rebases={"evil": "evil2"})]
    assert check_commit_binding(["c1", "evil2"], runs) == ForeignCommits(("evil2",))


def test_inherited_and_adopted_commits_count_as_accepted() -> None:
    runs = [run("r1", ["c1"])]
    result = check_commit_binding(["c1", "i1", "h1"], runs, inherited=[("i1",)], adopted=["h1"])
    assert result == Bound()


# --- between runs ---------------------------------------------------------------------------


def test_the_expected_head_is_the_last_accepted_commit_else_the_branch_creation_commit() -> None:
    assert expected_head(last_accepted="c2", branch_creation="b0") == "c2"
    assert expected_head(last_accepted=None, branch_creation="b0") == "b0"
    with pytest.raises(ValueError):
        expected_head(last_accepted=None, branch_creation=None)


def test_a_head_that_differs_from_the_expected_one_is_an_unexpected_push() -> None:
    assert check_between_runs(branch_head="c2", expected="c2") is None
    assert check_between_runs(branch_head="zz", expected="c2") == UnexpectedPush(
        head="zz", expected="c2"
    )


# --- inherited ranges after a crash --------------------------------------------------------


def meta(sha: str, parent: str, author: str = AGENT, committer: str = AGENT) -> CommitMeta:
    return CommitMeta(sha=sha, parent=parent, author=author, committer=committer)


def test_an_inherited_range_must_fast_forward_from_the_last_accepted_commit() -> None:
    commits = [meta("i1", "c2"), meta("i2", "i1")]
    assert validate_inherited_range(
        commits, last_accepted="c2", agent_identity=AGENT
    ) == InheritedValid(("i1", "i2"))


def test_an_empty_inherited_range_is_valid() -> None:
    assert validate_inherited_range([], last_accepted="c2", agent_identity=AGENT) == InheritedValid(
        ()
    )


@pytest.mark.parametrize(
    "commits",
    [
        [meta("i1", "other")],  # does not start at the last accepted commit
        [meta("i1", "c2"), meta("i2", "elsewhere")],  # not linear
        [meta("i1", "c2", author="mallory")],
        [meta("i1", "c2", committer="mallory")],
    ],
)
def test_an_inherited_range_that_is_not_a_clean_agent_fast_forward_is_rejected(
    commits: list[CommitMeta],
) -> None:
    assert isinstance(
        validate_inherited_range(commits, last_accepted="c2", agent_identity=AGENT),
        InheritedRejected,
    )


# --- recovery -------------------------------------------------------------------------------


def test_recovery_resets_the_branch_to_the_last_accepted_commit_and_logs_what_was_dropped() -> None:
    assert plan_recovery(foreign=["x1", "x2"], last_accepted="c2") == ResetPlan(
        reset_to="c2", dropped=("x1", "x2")
    )


def test_rebase_chains_resolve_whatever_order_the_runs_are_listed_in() -> None:
    runs = [
        run("r3", [], rebases={"c1r": "c1rr"}),  # the later rebase is listed first
        run("r2", [], rebases={"c1": "c1r"}),
        run("r1", ["c1"]),
    ]
    assert check_commit_binding(["c1rr"], runs) == Bound()
