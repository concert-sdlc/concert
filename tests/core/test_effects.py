"""Which external side effect an issue needs next (design: Crash-safe side effects)."""

from dataclasses import replace

import pytest

from concert.core.derive import Intent, IssueState
from concert.core.effects import plan_effect
from concert.core.transitions import IssueType, Status

BRANCHED = IssueState(status=Status.SPEC_APPROVAL, branch_base="b0")


def test_a_draft_pr_is_opened_once_the_spec_is_ready_for_approval() -> None:
    effect = plan_effect(BRANCHED, issue=4, base_branch="staging")
    assert effect is not None
    assert (effect.key, effect.action) == ("open_pr", "open_pr")
    assert effect.data == {"head": "pm/4", "base": "staging", "title": "Concert #4"}
    assert effect.resumed is False


def test_no_pr_is_opened_before_the_spec_is_ready() -> None:
    assert plan_effect(replace(BRANCHED, status=Status.SPEC), 4, "staging") is None


def test_no_pr_is_opened_when_one_exists() -> None:
    assert plan_effect(replace(BRANCHED, pr_number=7), 4, "staging") is None


def test_no_pr_is_opened_for_an_issue_without_a_branch() -> None:
    assert plan_effect(replace(BRANCHED, branch_base=None), 4, "staging") is None


def test_a_revert_issue_does_not_get_the_draft_pr_of_the_normal_path() -> None:
    state = replace(BRANCHED, issue_type=IssueType.REVERT, status=Status.HUMAN_UAT)
    assert plan_effect(state, 4, "staging") is None


def test_the_pr_is_marked_ready_when_review_starts() -> None:
    state = replace(BRANCHED, status=Status.IN_REVIEW, pr_number=7)
    effect = plan_effect(state, 4, "staging")
    assert effect is not None
    assert (effect.key, effect.action, dict(effect.data)) == ("ready", "mark_ready", {"number": 7})


def test_a_ready_pr_needs_nothing_more() -> None:
    state = replace(BRANCHED, status=Status.IN_REVIEW, pr_number=7, pr_ready=True)
    assert plan_effect(state, 4, "staging") is None


def test_the_pr_is_not_marked_ready_while_implementing() -> None:
    state = replace(BRANCHED, status=Status.IMPLEMENTING, pr_number=7)
    assert plan_effect(state, 4, "staging") is None


def test_an_issue_that_returns_to_implementing_keeps_its_ready_pr() -> None:
    state = replace(BRANCHED, status=Status.IMPLEMENTING, pr_number=7, pr_ready=True)
    assert plan_effect(state, 4, "staging") is None


def test_a_pending_intent_is_resumed_before_anything_new() -> None:
    pending = Intent(
        "open_pr", "open_pr", {"head": "pm/4", "base": "staging", "title": "Concert #4"}
    )
    state = replace(BRANCHED, status=Status.IN_REVIEW, pending_intents=(pending,))
    effect = plan_effect(state, 4, "staging")
    assert effect is not None
    assert (effect.key, effect.action, effect.resumed) == ("open_pr", "open_pr", True)
    assert effect.data == pending.data


def test_a_blocked_issue_has_no_new_effects() -> None:
    state = replace(BRANCHED, status=Status.BLOCKED, blocked_reason="why")
    assert plan_effect(state, 4, "staging") is None


@pytest.mark.parametrize(
    "status",
    [Status.IN_REVIEW, Status.UAT, Status.HUMAN_UAT, Status.INTEGRATING, Status.VERIFYING_STAGING],
)
def test_the_pr_is_marked_ready_from_review_onwards(status: Status) -> None:
    effect = plan_effect(replace(BRANCHED, status=status, pr_number=7), 4, "staging")
    assert effect is not None and effect.action == "mark_ready"


@pytest.mark.parametrize("status", [Status.ON_STAGING, Status.CLOSED, Status.CLOSED_DUPLICATE])
def test_a_finished_issue_needs_no_effects(status: Status) -> None:
    assert plan_effect(replace(BRANCHED, status=status), 4, "staging") is None
    assert plan_effect(replace(BRANCHED, status=status, pr_number=7), 4, "staging") is None


# --- the merge ------------------------------------------------------------------------------------

READY_TO_MERGE = replace(
    BRANCHED,
    status=Status.INTEGRATING,
    pr_number=7,
    pr_ready=True,
    head_sha="h1",
    settled_runs=frozenset({"r-9"}),
)


def test_a_merge_is_planned_once_the_integration_run_is_done_and_the_gate_holds() -> None:
    effect = plan_effect(READY_TO_MERGE, 4, "staging", gate_ready=True)
    assert effect is not None
    assert (effect.key, effect.action) == ("merge", "merge")
    assert dict(effect.data) == {"number": 7, "expected_head": "h1"}


def test_no_merge_is_planned_while_the_gate_does_not_hold() -> None:
    assert plan_effect(READY_TO_MERGE, 4, "staging", gate_ready=False) is None


def test_no_merge_is_planned_before_the_integration_run_reports() -> None:
    state = replace(READY_TO_MERGE, settled_runs=frozenset())
    assert plan_effect(state, 4, "staging", gate_ready=True) is None


def test_no_merge_is_planned_twice() -> None:
    state = replace(READY_TO_MERGE, merge_sha="m1")
    assert plan_effect(state, 4, "staging", gate_ready=True) is None


def test_no_merge_is_planned_outside_integrating() -> None:
    state = replace(READY_TO_MERGE, status=Status.HUMAN_UAT)
    assert plan_effect(state, 4, "staging", gate_ready=True) is None


def test_no_merge_is_planned_without_a_ready_pr() -> None:
    state = replace(READY_TO_MERGE, pr_ready=False)
    effect = plan_effect(state, 4, "staging", gate_ready=True)
    assert effect is not None and effect.action == "mark_ready"
