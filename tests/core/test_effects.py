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
    assert (effect.key, effect.action, dict(effect.data)) == (
        "ready:0",
        "mark_ready",
        {"number": 7},
    )


def test_a_ready_pr_needs_nothing_more() -> None:
    state = replace(BRANCHED, status=Status.IN_REVIEW, pr_number=7, pr_ready=True)
    assert plan_effect(state, 4, "staging") is None


def test_the_pr_is_not_marked_ready_while_implementing() -> None:
    state = replace(BRANCHED, status=Status.IMPLEMENTING, pr_number=7)
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


@pytest.mark.parametrize("status", [Status.ON_STAGING, Status.CLOSED])
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
    state = replace(READY_TO_MERGE, status=Status.HUMAN_UAT, kits_posted=frozenset({"kit:uat:h1"}))
    assert plan_effect(state, 4, "staging", gate_ready=True) is None


def test_no_merge_is_planned_without_a_ready_pr() -> None:
    state = replace(READY_TO_MERGE, pr_ready=False)
    effect = plan_effect(state, 4, "staging", gate_ready=True)
    assert effect is not None and effect.action == "mark_ready"


# --- auto-revert ----------------------------------------------------------------------------------

FAILED = replace(
    BRANCHED,
    status=Status.VERIFYING_STAGING,
    pr_number=7,
    pr_ready=True,
    head_sha="h1",
    merge_sha="m1",
    verify_failed=True,
)


def test_a_final_failure_under_auto_revert_opens_a_revert_pr() -> None:
    effect = plan_effect(FAILED, 4, "staging", auto_revert=True)
    assert effect is not None
    assert (effect.key, effect.action, dict(effect.data)) == (
        "revert_pr",
        "revert_pr",
        {"number": 7},
    )


def test_no_revert_is_opened_under_the_freeze_policy() -> None:
    assert plan_effect(FAILED, 4, "staging", auto_revert=False) is None


def test_no_revert_is_opened_while_the_failure_is_not_final() -> None:
    assert plan_effect(replace(FAILED, verify_failed=False), 4, "staging", auto_revert=True) is None


def test_only_code_issues_are_auto_reverted() -> None:
    for kind in (IssueType.HOTFIX, IssueType.REVERT):
        assert plan_effect(replace(FAILED, issue_type=kind), 4, "staging", auto_revert=True) is None


def test_a_green_revert_pr_is_merged_at_its_head() -> None:
    state = replace(FAILED, revert_pr=9, revert_head="rv1", revert_ci_passed=True)
    effect = plan_effect(state, 4, "staging", auto_revert=True)
    assert effect is not None
    assert (effect.key, effect.action, dict(effect.data)) == (
        "merge_revert",
        "merge_revert",
        {"number": 9, "expected_head": "rv1", "reverts": "m1"},
    )


def test_the_revert_pr_waits_for_its_checks() -> None:
    for passed in (None, False):
        state = replace(FAILED, revert_pr=9, revert_head="rv1", revert_ci_passed=passed)
        assert plan_effect(state, 4, "staging", auto_revert=True) is None


def test_a_merged_revert_needs_no_more_effects() -> None:
    state = replace(
        FAILED, revert_pr=9, revert_head="rv1", revert_ci_passed=True, revert_merge_sha="rm1"
    )
    assert plan_effect(state, 4, "staging", auto_revert=True) is None


def test_the_pr_of_a_re_landed_issue_uses_its_new_branch() -> None:
    state = replace(BRANCHED, status=Status.IMPLEMENTING, branch="pm/4-r1")
    effect = plan_effect(state, 4, "staging")
    assert effect is not None and effect.data["head"] == "pm/4-r1"


# --- kits -------------------------------------------------------------------------------------------


def test_the_spec_kit_is_posted_once_the_spec_awaits_approval() -> None:
    state = replace(BRANCHED, pr_number=7, spec_rev="s1")
    effect = plan_effect(state, 4, "staging")
    assert effect is not None
    assert (effect.key, effect.action, dict(effect.data)) == (
        "kit:spec:s1",
        "post_kit",
        {"kind": "spec", "rev": "s1"},
    )


def test_a_posted_kit_is_not_posted_again_but_a_new_revision_gets_a_new_kit() -> None:
    state = replace(BRANCHED, pr_number=7, spec_rev="s1", kits_posted=frozenset({"kit:spec:s1"}))
    assert plan_effect(state, 4, "staging") is None
    changed = replace(state, spec_rev="s2")
    effect = plan_effect(changed, 4, "staging")
    assert effect is not None and effect.key == "kit:spec:s2"


def test_the_uat_kit_is_posted_for_the_head_when_the_human_gate_opens() -> None:
    state = replace(BRANCHED, status=Status.HUMAN_UAT, pr_number=7, pr_ready=True, head_sha="h1")
    assert plan_effect(state, 4, "staging") is None  # the automated evidence is not complete yet
    effect = plan_effect(state, 4, "staging", awaiting_human=True)
    assert effect is not None
    assert (effect.key, dict(effect.data)) == ("kit:uat:h1", {"kind": "uat", "rev": "h1"})


def test_no_uat_kit_without_a_head_and_none_for_other_states() -> None:
    gate = replace(BRANCHED, status=Status.HUMAN_UAT, pr_number=7, pr_ready=True)
    assert plan_effect(gate, 4, "staging", awaiting_human=True) is None
    review = replace(gate, status=Status.IN_REVIEW, head_sha="h1")
    assert plan_effect(review, 4, "staging", awaiting_human=True) is None


def test_the_pr_comes_before_the_kit() -> None:
    state = replace(BRANCHED, spec_rev="s1")  # no PR yet
    effect = plan_effect(state, 4, "staging")
    assert effect is not None and effect.action == "open_pr"


# --- questions --------------------------------------------------------------------------------------


ASKING = replace(
    BRANCHED,
    status=Status.NEEDS_INPUT,
    asked_from=Status.SPEC,
    doubts=("Which formats?",),
)


def test_a_needs_input_issue_gets_its_question_posted_once() -> None:
    effect = plan_effect(ASKING, 4, "staging")
    assert effect is not None
    assert (effect.key, effect.action) == ("question:0", "post_question")
    assert list(effect.data["doubts"]) == ["Which formats?"]
    posted = replace(ASKING, questions_posted=frozenset({"question:0"}))
    assert plan_effect(posted, 4, "staging") is None


def test_each_new_ask_is_a_new_question() -> None:
    again = replace(ASKING, answers=("first answer",))
    effect = plan_effect(again, 4, "staging")
    assert effect is not None and effect.key == "question:1"
    posted = replace(again, questions_posted=frozenset({"question:0"}))
    assert plan_effect(posted, 4, "staging").key == "question:1"  # type: ignore[union-attr]


def test_no_question_is_posted_without_doubts_to_ask() -> None:
    assert plan_effect(replace(ASKING, doubts=()), 4, "staging") is not None  # asks generically


# --- duplicates -------------------------------------------------------------------------------------


def test_a_duplicate_is_closed_on_github_once() -> None:
    state = replace(BRANCHED, status=Status.CLOSED_DUPLICATE, decisions=("same as #3",))
    effect = plan_effect(state, 4, "staging")
    assert effect is not None
    assert (effect.key, effect.action) == ("close_duplicate", "close_issue")
    assert list(effect.data["decisions"]) == ["same as #3"]
    done = replace(state, closed_posted=frozenset({"close_duplicate"}))
    assert plan_effect(done, 4, "staging") is None


# --- draft discipline ---------------------------------------------------------------------------------


def test_a_ready_pr_goes_back_to_draft_when_the_issue_returns_to_rework() -> None:
    for status in (Status.IMPLEMENTING, Status.SPEC, Status.SPEC_APPROVAL):
        state = replace(BRANCHED, status=status, pr_number=7, pr_ready=True)
        effect = plan_effect(state, 4, "staging")
        assert effect is not None
        assert (effect.key, effect.action, dict(effect.data)) == (
            "draft:0",
            "mark_draft",
            {"number": 7},
        )


def test_a_draft_pr_in_rework_needs_nothing() -> None:
    state = replace(BRANCHED, status=Status.IMPLEMENTING, pr_number=7, pr_ready=False)
    assert plan_effect(state, 4, "staging") is None


def test_each_toggle_has_its_own_key() -> None:
    again = replace(BRANCHED, status=Status.IN_REVIEW, pr_number=7, ready_toggles=2)
    effect = plan_effect(again, 4, "staging")
    assert effect is not None and effect.key == "ready:2"
    back = replace(again, status=Status.IMPLEMENTING, pr_ready=True, ready_toggles=3)
    effect = plan_effect(back, 4, "staging")
    assert effect is not None and effect.key == "draft:3"


def test_a_blocked_or_waiting_issue_keeps_its_pr_as_it_is() -> None:
    for status in (Status.BLOCKED, Status.NEEDS_INPUT, Status.HUMAN_UAT):
        state = replace(BRANCHED, status=status, pr_number=7, pr_ready=True)
        effect = plan_effect(state, 4, "staging")
        assert effect is None or effect.action != "mark_draft"
