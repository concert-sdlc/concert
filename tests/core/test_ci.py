"""CI check runs become evidence, once per distinct result (design: Evidence ledger)."""

from concert.core.ci import CheckRun, plan_ci_evidence
from concert.core.evidence import Evidence

DOD = frozenset({"tests", "lint"})
ALLOWED = frozenset({"w1"})


def run(name: str, conclusion: str | None, head: str = "h1", workflow: str = "w1") -> CheckRun:
    return CheckRun(name=name, head_sha=head, conclusion=conclusion, workflow_hash=workflow)


def plan(runs: list[CheckRun], recorded: tuple[Evidence, ...] = (), head: str | None = "h1"):
    return plan_ci_evidence(head, runs, recorded, DOD, policy_rev="p1")


def test_a_finished_dod_check_on_the_head_becomes_ci_evidence() -> None:
    [proposal] = plan([run("tests", "success")])
    assert proposal.kind == "evidence"
    assert proposal.data == {
        "kind": "ci",
        "item": "tests",
        "head_sha": "h1",
        "passed": True,
        "policy_rev": "p1",
        "workflow_hash": "w1",
    }


def test_a_failed_check_is_failing_evidence() -> None:
    [proposal] = plan([run("lint", "failure")])
    assert proposal.data["passed"] is False


def test_only_success_passes() -> None:
    for conclusion in ("cancelled", "timed_out", "neutral", "skipped", "action_required"):
        [proposal] = plan([run("tests", conclusion)])
        assert proposal.data["passed"] is False, conclusion


def test_a_check_that_has_not_finished_is_not_evidence_yet() -> None:
    assert plan([run("tests", None)]) == []


def test_checks_outside_the_definition_of_done_are_ignored() -> None:
    assert plan([run("coverage-badge", "success")]) == []


def test_checks_for_another_head_are_ignored() -> None:
    assert plan([run("tests", "success", head="old")]) == []


def test_nothing_is_recorded_before_the_issue_has_a_head() -> None:
    assert plan([run("tests", "success")], head=None) == []


def test_an_identical_recorded_result_is_not_recorded_again() -> None:
    recorded = (Evidence("ci", "tests", "h1", True, policy_rev="p1", workflow_hash="w1"),)
    assert plan([run("tests", "success")], recorded) == []


def test_a_changed_result_for_the_same_check_is_recorded() -> None:
    recorded = (Evidence("ci", "tests", "h1", False, policy_rev="p1", workflow_hash="w1"),)
    [proposal] = plan([run("tests", "success")], recorded)
    assert proposal.data["passed"] is True


def test_a_different_workflow_or_policy_is_recorded_again() -> None:
    recorded = (Evidence("ci", "tests", "h1", True, policy_rev="p0", workflow_hash="w1"),)
    assert len(plan([run("tests", "success")], recorded)) == 1
    recorded = (Evidence("ci", "tests", "h1", True, policy_rev="p1", workflow_hash="w0"),)
    assert len(plan([run("tests", "success")], recorded)) == 1


def test_a_result_recorded_for_an_older_head_does_not_suppress_the_new_one() -> None:
    recorded = (Evidence("ci", "tests", "old", True, policy_rev="p1", workflow_hash="w1"),)
    assert len(plan([run("tests", "success")], recorded)) == 1


def test_when_a_check_ran_twice_the_latest_run_wins() -> None:
    [proposal] = plan([run("tests", "failure"), run("tests", "success")])
    assert proposal.data["passed"] is True


def test_proposals_follow_the_order_of_the_definition_of_done() -> None:
    names = [p.data["item"] for p in plan([run("tests", "success"), run("lint", "success")])]
    assert names == ["lint", "tests"]


def test_a_result_that_flips_back_is_recorded_even_if_it_appeared_earlier() -> None:
    recorded = (
        Evidence("ci", "tests", "h1", False, policy_rev="p1", workflow_hash="w1"),
        Evidence("ci", "tests", "h1", True, policy_rev="p1", workflow_hash="w1"),
    )
    [proposal] = plan([run("tests", "failure")], recorded)
    assert proposal.data["passed"] is False


def test_other_evidence_with_the_same_item_name_does_not_hide_the_recorded_ci_result() -> None:
    recorded = (
        Evidence("ci", "tests", "h1", True, policy_rev="p1", workflow_hash="w1"),
        Evidence("review", "tests", "h1", True),
    )
    assert plan([run("tests", "success")], recorded) == []
