"""derive(): fold trusted pm-events into the issue state (design: "Event log")."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from concert.core.counters import AgentState
from concert.core.derive import derive
from concert.core.events import Event, append_event
from concert.core.transitions import IssueType, Status

FIXTURES = Path(__file__).parent.parent / "fixtures" / "events"

Raw = Mapping[str, Any]


class Journey:
    """Builds a valid chain event by event."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def add(self, kind: str, data: Mapping[str, Any], epoch: int = 1) -> "Journey":
        self.events.append(append_event(self.events, epoch=epoch, kind=kind, data=data))
        return self

    def move(self, src: str, dst: str, trigger: str, epoch: int = 1) -> "Journey":
        return self.add("transition", {"from": src, "to": dst, "trigger": trigger}, epoch)

    def raw(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events]


def to_in_review() -> Journey:
    return (
        Journey()
        .add("created", {"issue_type": "code"})
        .move("inbox", "triage", "dispatch")
        .move("triage", "spec", "spec")
        .move("spec", "spec_approval", "ready_for_approval")
        .move("spec_approval", "implementing", "approve_spec")
        .move("implementing", "in_review", "done")
    )


def test_an_issue_with_no_events_is_a_new_code_issue_in_inbox() -> None:
    state = derive([])
    assert state.status is Status.INBOX
    assert state.issue_type is IssueType.CODE
    assert state.blocked_reason is None
    assert state.last_seq == 0


def test_transitions_fold_into_the_current_status() -> None:
    state = derive(to_in_review().raw())
    assert state.status is Status.IN_REVIEW
    assert state.last_seq == 6
    assert state.blocked_reason is None


def test_a_revert_issue_starts_in_the_pm_only_implementing_state() -> None:
    state = derive(Journey().add("created", {"issue_type": "revert"}).raw())
    assert state.issue_type is IssueType.REVERT
    assert state.status is Status.IMPLEMENTING


def test_counter_events_charge_and_reset() -> None:
    journey = to_in_review().add("counter_charged", {"cause": "changes_needed"})
    journey.move("in_review", "implementing", "changes_needed")
    state = derive(journey.raw())
    assert state.status is Status.IMPLEMENTING
    assert state.counters.used(AgentState.IMPLEMENTING) == 1

    reset = journey.add("counter_reset", {"state": "implementing"})
    assert derive(reset.raw()).counters.used(AgentState.IMPLEMENTING) == 0


def test_exceeding_the_budget_blocks_the_issue() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("counter_charged", {"cause": "changes_needed"})
    journey.add("counter_charged", {"cause": "changes_needed"})
    state = derive(journey.raw(), budget=1)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason == "retry budget exceeded for implementing"


def test_the_golden_journey_derives_to_implementing_with_one_charge() -> None:
    raw = json.loads((FIXTURES / "v1_journey.json").read_text())
    state = derive(raw["events"])
    assert state.status is Status.IMPLEMENTING
    assert state.counters.used(AgentState.IMPLEMENTING) == 1
    assert state.last_seq == 8


def test_a_deleted_event_blocks_the_issue_and_shows_the_gap() -> None:
    raw = to_in_review().raw()
    del raw[2]
    state = derive(raw)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None
    assert "seq 3" in state.blocked_reason


def test_a_rewritten_event_blocks_the_issue() -> None:
    raw = to_in_review().raw()
    raw[2] = raw[2] | {"data": {"from": "triage", "to": "implementing", "trigger": "spec"}}
    state = derive(raw)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None
    assert "hash break" in state.blocked_reason


def test_an_unknown_newer_schema_blocks_with_runner_too_old() -> None:
    raw = to_in_review().raw()
    raw[3] = raw[3] | {"schema": 99}
    state = derive(raw)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None
    assert "runner too old" in state.blocked_reason


def test_a_malformed_event_blocks_the_issue() -> None:
    raw = to_in_review().raw()
    del raw[1]["kind"]
    state = derive(raw)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None
    assert "malformed" in state.blocked_reason


def test_a_transition_from_the_wrong_status_is_a_conflict() -> None:
    journey = to_in_review().move("triage", "spec", "spec")  # the issue is in in_review
    state = derive(journey.raw())
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None
    assert "conflict" in state.blocked_reason


def test_a_transition_the_table_does_not_allow_blocks_the_issue() -> None:
    journey = to_in_review().move("in_review", "integrating", "approve_uat")
    state = derive(journey.raw())
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None
    assert "invalid transition" in state.blocked_reason


def test_a_stale_epoch_event_is_ignored() -> None:
    journey = Journey().add("created", {"issue_type": "code"}, epoch=2)
    journey.move("inbox", "triage", "dispatch", epoch=2)
    journey.move("triage", "spec", "spec", epoch=1)  # posted by a stale coordinator
    state = derive(journey.raw())
    assert state.status is Status.TRIAGE
    assert state.blocked_reason is None
    assert state.last_seq == 3


def test_derive_is_deterministic() -> None:
    raw = to_in_review().raw()
    assert derive(raw) == derive(raw)


# --- events that record consumed inputs ---------------------------------------------------------


def test_accepted_runs_and_consumed_commands_are_remembered() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("handoff_accepted", {"run_id": "r-1"})
    journey.add("gate_passage", {"comment_id": 12, "verb": "dispatch"})
    state = derive(journey.raw())
    assert state.accepted_runs == frozenset({"r-1"})
    assert state.consumed_commands == frozenset({12})


def test_an_unblock_event_moves_a_blocked_issue_to_the_named_state() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.move("inbox", "triage", "dispatch")
    journey.move("triage", "blocked", "budget_exceeded")
    journey.add("unblocked", {"to": "triage"})
    state = derive(journey.raw())
    assert state.status is Status.TRIAGE
    assert state.blocked_reason is None


def test_an_unblock_event_on_an_issue_that_is_not_blocked_is_a_conflict() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("unblocked", {"to": "triage"})
    state = derive(journey.raw())
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None and "conflict" in state.blocked_reason


# --- spec revision, approved spec and head --------------------------------------------------------


def test_an_accepted_spec_run_sets_the_current_spec_rev_and_head() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add(
        "handoff_accepted",
        {"run_id": "r-2", "state": "spec", "commit_sha": "aaa1111", "artifact_rev": "spec1"},
    )
    state = derive(journey.raw())
    assert state.spec_rev == "spec1"
    assert state.head_sha == "aaa1111"
    assert state.approved_spec_rev is None


def test_a_run_that_reports_no_commit_leaves_the_head_alone() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add(
        "handoff_accepted",
        {"run_id": "r-1", "state": "triage", "commit_sha": None, "artifact_rev": None},
    )
    assert derive(journey.raw()).head_sha is None


def test_approving_the_spec_records_the_approved_hash() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add(
        "gate_passage", {"comment_id": 4, "verb": "approve", "kind": "spec", "spec_hash": "spec1"}
    )
    assert derive(journey.raw()).approved_spec_rev == "spec1"


def test_a_returned_spec_loses_its_approval() -> None:
    journey = (
        Journey()
        .add("created", {"issue_type": "code"})
        .move("inbox", "triage", "dispatch")
        .move("triage", "spec", "spec")
        .move("spec", "spec_approval", "ready_for_approval")
        .add(
            "gate_passage",
            {"comment_id": 4, "verb": "approve", "kind": "spec", "spec_hash": "spec1"},
        )
        .move("spec_approval", "implementing", "approve_spec")
        .move("implementing", "spec_approval", "spec_drift")
    )
    assert derive(journey.raw()).approved_spec_rev is None


# --- assignments and leases ---------------------------------------------------------------------


def assignment(
    run_id: str, runner: str = "vm-1", state: str = "triage", **extra: object
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "runner": runner,
        "state": state,
        "reviewer": None,
        "head": None,
        "issued_at": 100,
    } | extra


def test_an_assignment_makes_the_run_active() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add("assignment", assignment("r-1-1"))
    state = derive(journey.raw())
    assert [(r.run_id, r.runner, r.state, r.issued_at) for r in state.active_runs] == [
        ("r-1-1", "vm-1", "triage", 100)
    ]
    assert state.assignments_made == 1


def test_an_accepted_handoff_ends_the_run_and_a_transition_clears_the_state() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add("assignment", assignment("r-1-1"))
    journey.add(
        "handoff_accepted",
        {"run_id": "r-1-1", "state": "triage", "commit_sha": None, "artifact_rev": None},
    )
    state = derive(journey.raw())
    assert state.active_runs == ()
    assert state.settled_runs == frozenset({"r-1-1"})  # accepted in this state, no transition yet
    journey.move("triage", "spec", "spec")
    assert derive(journey.raw()).settled_runs == frozenset()


def test_an_expired_lease_ends_the_run_and_counts_an_infra_attempt() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add("assignment", assignment("r-1-1"))
    journey.add("lease_expired", {"run_id": "r-1-1", "state": "triage", "at": 400})
    state = derive(journey.raw())
    assert state.active_runs == ()
    assert state.infra_attempts == {"triage": 1}
    assert state.last_expiry_at == 400


def test_infra_attempts_reset_when_the_issue_moves_on() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add("assignment", assignment("r-1-1"))
    journey.add("lease_expired", {"run_id": "r-1-1", "state": "triage", "at": 400})
    journey.move("triage", "spec", "spec")
    assert derive(journey.raw()).infra_attempts == {}


def test_a_transition_drops_runs_still_in_flight_for_the_old_state() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add("assignment", assignment("r-1-1"))
    journey.move("triage", "blocked", "budget_exceeded")
    assert derive(journey.raw()).active_runs == ()


# --- notes and the issue branch ------------------------------------------------------------------


def test_a_block_by_a_failed_check_keeps_its_note_until_unblocked() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add(
        "transition",
        {
            "from": "triage",
            "to": "blocked",
            "trigger": "check_failed",
            "note": "unexpected push: abc",
        },
    )
    state = derive(journey.raw())
    assert state.status is Status.BLOCKED and state.note == "unexpected push: abc"
    journey.add("unblocked", {"to": "triage"})
    assert derive(journey.raw()).note is None


def test_the_branch_creation_commit_is_remembered() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("branch_created", {"branch": "pm/7", "sha": "base111"})
    state = derive(journey.raw())
    assert state.branch_base == "base111"


# --- evidence ----------------------------------------------------------------------------------


def test_evidence_events_are_collected_in_order() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add(
        "evidence",
        {
            "kind": "ci",
            "item": "tests",
            "head_sha": "h1",
            "passed": True,
            "policy_rev": "p1",
            "workflow_hash": "w1",
        },
    )
    journey.add(
        "evidence",
        {"kind": "approval", "item": "uat", "head_sha": "h1", "passed": True, "actor_id": 2},
    )
    state = derive(journey.raw())
    assert [(e.kind, e.item, e.head_sha, e.passed, e.actor_id) for e in state.evidence] == [
        ("ci", "tests", "h1", True, None),
        ("approval", "uat", "h1", True, 2),
    ]
    assert state.evidence[0].workflow_hash == "w1" and state.evidence[0].policy_rev == "p1"


# --- side effects: intent and outcome ----------------------------------------------------------


def test_an_intent_without_an_outcome_is_pending() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("intent", {"key": "open_pr", "action": "open_pr", "branch": "pm/1"})
    state = derive(journey.raw())
    assert [(i.key, i.action, dict(i.data)) for i in state.pending_intents] == [
        ("open_pr", "open_pr", {"branch": "pm/1"})
    ]


def test_an_outcome_resolves_its_intent_and_records_the_pr() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("intent", {"key": "open_pr", "action": "open_pr", "branch": "pm/1"})
    journey.add("outcome", {"key": "open_pr", "result": {"number": 7}})
    state = derive(journey.raw())
    assert state.pending_intents == ()
    assert state.pr_number == 7
    assert state.pr_ready is False


def test_a_mark_ready_outcome_records_that_the_pr_is_ready() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("outcome", {"key": "open_pr", "result": {"number": 7}})
    journey.add("intent", {"key": "ready", "action": "mark_ready", "number": 7})
    journey.add("outcome", {"key": "ready", "result": {"number": 7}})
    assert derive(journey.raw()).pr_ready is True


def test_an_outcome_for_an_unknown_intent_is_still_applied() -> None:
    # a reconciled recovery may record an outcome the intent of which was in a lost comment
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("outcome", {"key": "open_pr", "action": "open_pr", "result": {"number": 3}})
    assert derive(journey.raw()).pr_number == 3


def test_announcing_the_same_intent_twice_leaves_one_pending() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("intent", {"key": "open_pr", "action": "open_pr", "head": "pm/1"})
    journey.add("intent", {"key": "open_pr", "action": "open_pr", "head": "pm/1"})
    assert len(derive(journey.raw()).pending_intents) == 1


def test_a_merge_outcome_records_the_merge_commit() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("intent", {"key": "merge", "action": "merge", "number": 7, "expected_head": "h1"})
    assert derive(journey.raw()).merge_sha is None
    journey.add("outcome", {"key": "merge", "result": {"sha": "m1"}})
    state = derive(journey.raw())
    assert state.merge_sha == "m1" and state.pending_intents == ()


def test_a_failed_merge_outcome_resolves_the_intent_without_a_merge() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("intent", {"key": "merge", "action": "merge", "number": 7, "expected_head": "h1"})
    journey.add("outcome", {"key": "merge", "result": {"error": "stale head"}})
    state = derive(journey.raw())
    assert state.merge_sha is None and state.pending_intents == ()


def test_verify_rerun_counts_the_failure_and_clears_the_settled_run() -> None:
    journey = to_in_review().move("in_review", "uat", "clean").move("uat", "human_uat", "passed")
    journey.move("human_uat", "integrating", "approve_uat")
    journey.move("integrating", "verifying_staging", "ready_to_merge")
    journey.add(
        "handoff_accepted",
        {"run_id": "r-1", "state": "verifying_staging", "commit_sha": "m1", "artifact_rev": None},
    )
    assert derive(journey.raw()).settled_runs == frozenset({"r-1"})
    journey.add("verify_rerun", {"run_id": "r-1"})
    state = derive(journey.raw())
    assert state.verify_failures == 1 and state.settled_runs == frozenset()
    assert state.verify_failed is False
    journey.add("verify_failed", {"run_id": "r-2"})
    assert derive(journey.raw()).verify_failed is True


def test_verification_failures_start_over_when_the_issue_returns_to_verifying() -> None:
    journey = to_in_review().move("in_review", "uat", "clean").move("uat", "human_uat", "passed")
    journey.move("human_uat", "integrating", "approve_uat")
    journey.move("integrating", "verifying_staging", "ready_to_merge")
    journey.add("verify_rerun", {"run_id": "r-1"})
    journey.add("verify_failed", {"run_id": "r-2"})
    journey.move("verifying_staging", "implementing", "failed_reverted")
    assert derive(journey.raw()).verify_failures == 0
    assert derive(journey.raw()).verify_failed is False


# --- re-landing after an auto-revert -----------------------------------------------------------


def reverted_journey() -> Journey:
    journey = to_in_review().move("in_review", "uat", "clean").move("uat", "human_uat", "passed")
    journey.add("branch_created", {"branch": "pm/1", "sha": "b0"})
    journey.add("outcome", {"key": "open_pr", "action": "open_pr", "result": {"number": 7}})
    journey.add("evidence", {"kind": "uat", "item": None, "head_sha": "h1", "passed": True})
    journey.add(
        "handoff_accepted",
        {"run_id": "r-1", "state": "implementing", "commit_sha": "h1", "artifact_rev": None},
    )
    journey.move("human_uat", "integrating", "approve_uat")
    journey.add("outcome", {"key": "merge", "action": "merge", "result": {"sha": "m1"}})
    journey.move("integrating", "verifying_staging", "ready_to_merge")
    journey.add(
        "outcome",
        {
            "key": "revert_pr",
            "action": "revert_pr",
            "result": {"number": 9, "branch": "r", "sha": "rv"},
        },
    )
    return journey.move("verifying_staging", "implementing", "failed_reverted")


def test_a_reverted_issue_starts_over_on_a_new_branch_generation() -> None:
    state = derive(reverted_journey().raw())
    assert state.status is Status.IMPLEMENTING
    assert state.relands == 1 and state.reland_of == "m1"
    assert state.branch_base is None and state.head_sha is None
    assert state.pr_number is None and state.pr_ready is False
    assert state.merge_sha is None and state.revert_pr is None and state.revert_head is None
    assert state.revert_ci_passed is None and state.revert_merge_sha is None
    assert state.evidence == ()


def test_the_new_branch_is_recorded_by_name() -> None:
    journey = reverted_journey()
    journey.add("branch_created", {"branch": "pm/1-r1", "sha": "b1"})
    state = derive(journey.raw())
    assert state.branch == "pm/1-r1" and state.branch_base == "b1"


def test_the_dispatch_time_is_taken_from_the_dispatch_command() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add(
        "gate_passage",
        {"comment_id": 4, "verb": "dispatch", "kind": None, "arg": "", "created_at": 1234},
    )
    assert derive(journey.raw()).dispatched_at == 1234


def test_a_failed_verification_freezes_the_queue_until_the_issue_is_unblocked() -> None:
    journey = to_in_review().move("in_review", "uat", "clean").move("uat", "human_uat", "passed")
    journey.move("human_uat", "integrating", "approve_uat")
    journey.move("integrating", "verifying_staging", "ready_to_merge")
    assert derive(journey.raw()).froze_queue is False
    journey.move("verifying_staging", "blocked", "verify_failed")
    assert derive(journey.raw()).froze_queue is True
    journey.add("unblocked", {"to": "implementing"})
    assert derive(journey.raw()).froze_queue is False


def test_a_kit_outcome_remembers_the_kit_was_posted() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("intent", {"key": "kit:spec:s1", "action": "post_kit", "kind": "spec", "rev": "s1"})
    journey.add("outcome", {"key": "kit:spec:s1", "result": {"comment_id": 5}})
    state = derive(journey.raw())
    assert state.kits_posted == frozenset({"kit:spec:s1"}) and state.pending_intents == ()


# --- needs_input ---------------------------------------------------------------------------------


def asking(source: str = "spec") -> Journey:
    journey = (
        Journey()
        .add("created", {"issue_type": "code"})
        .move("inbox", "triage", "dispatch")
        .move("triage", "spec", "spec")
    )
    journey.add(
        "handoff_accepted",
        {
            "run_id": "r-1",
            "state": "spec",
            "commit_sha": None,
            "artifact_rev": None,
            "doubts": ["Which formats?", "Which locale?"],
        },
    )
    return journey.move("spec", "needs_input", "needs_input")


def test_a_needs_input_issue_remembers_where_it_asked_and_what() -> None:
    state = derive(asking().raw())
    assert state.status is Status.NEEDS_INPUT
    assert state.asked_from is Status.SPEC
    assert state.doubts == ("Which formats?", "Which locale?")


def test_the_question_comment_is_recorded_by_its_outcome() -> None:
    journey = asking()
    journey.add("intent", {"key": "question:1", "action": "post_question"})
    journey.add("outcome", {"key": "question:1", "result": {"comment_id": 41}})
    state = derive(journey.raw())
    assert state.question_comment_id == 41 and state.questions_posted == frozenset({"question:1"})


def test_an_answer_returns_the_issue_to_where_it_asked_without_charging() -> None:
    journey = asking()
    journey.add("answered", {"to": "spec", "comment_id": 50, "author_id": 1, "text": "CSV, en-US"})
    state = derive(journey.raw())
    assert state.status is Status.SPEC and state.asked_from is None
    assert state.answers == ("CSV, en-US",)
    assert state.counters.used(AgentState.SPEC) == 0


def test_an_answer_that_names_another_state_is_a_conflict() -> None:
    journey = asking()
    journey.add("answered", {"to": "implementing", "comment_id": 50, "author_id": 1, "text": "x"})
    assert derive(journey.raw()).status is Status.BLOCKED


def test_an_answer_outside_needs_input_is_a_conflict() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("answered", {"to": "spec", "comment_id": 1, "author_id": 1, "text": "x"})
    assert derive(journey.raw()).status is Status.BLOCKED


# --- close and reopen ----------------------------------------------------------------------------


def test_closing_ends_the_runs_in_flight() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add(
        "assignment",
        {
            "run_id": "r-1",
            "runner": "vm-1",
            "state": "triage",
            "reviewer": None,
            "head": None,
            "issued_at": 1,
        },
    )
    assert len(derive(journey.raw()).active_runs) == 1
    journey.move("triage", "closed", "close")
    state = derive(journey.raw())
    assert state.status is Status.CLOSED and state.active_runs == ()


def test_reopening_voids_the_approvals_and_starts_a_new_generation() -> None:
    journey = to_in_review()
    journey.add("branch_created", {"branch": "pm/1", "sha": "b0"})
    journey.add("outcome", {"key": "open_pr", "action": "open_pr", "result": {"number": 7}})
    journey.add("evidence", {"kind": "uat", "item": None, "head_sha": "h1", "passed": True})
    assert derive(journey.raw()).approved_spec_rev is None  # (set below through a gate passage)
    journey.add(
        "gate_passage",
        {
            "comment_id": 3,
            "verb": "approve",
            "kind": "spec",
            "arg": "x",
            "spec_hash": "s1",
            "created_at": 5,
        },
    )
    assert derive(journey.raw()).approved_spec_rev == "s1"
    journey.move("in_review", "closed", "close").move("closed", "inbox", "reopen")
    state = derive(journey.raw())
    assert state.status is Status.INBOX and state.approved_spec_rev is None
    assert state.relands == 1 and state.branch_base is None and state.branch is None
    assert state.pr_number is None and state.evidence == () and state.head_sha is None


def test_decisions_are_kept_and_a_close_outcome_is_remembered() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add(
        "handoff_accepted",
        {
            "run_id": "r-1",
            "state": "triage",
            "commit_sha": None,
            "artifact_rev": None,
            "doubts": [],
            "decisions": ["same as #3"],
        },
    )
    journey.move("triage", "closed_duplicate", "duplicate")
    assert derive(journey.raw()).decisions == ("same as #3",)
    journey.add("intent", {"key": "close_duplicate", "action": "close_issue"})
    journey.add("outcome", {"key": "close_duplicate", "result": {"closed": True}})
    state = derive(journey.raw())
    assert state.closed_posted == frozenset({"close_duplicate"}) and state.pending_intents == ()


# --- dependencies --------------------------------------------------------------------------------


def test_dependencies_are_added_replaced_and_dropped() -> None:
    from concert.core.dependencies import Dependency

    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("dependency_added", {"issue": 5, "kind": "start", "source": "command"})
    journey.add("dependency_added", {"issue": 6, "kind": "merge", "source": "agent"})
    assert derive(journey.raw()).dependencies == (Dependency(5, "start"), Dependency(6, "merge"))
    journey.add("dependency_added", {"issue": 5, "kind": "merge", "source": "command"})
    assert derive(journey.raw()).dependencies == (Dependency(6, "merge"), Dependency(5, "merge"))
    journey.add("dependency_dropped", {"issue": 6})
    assert derive(journey.raw()).dependencies == (Dependency(5, "merge"),)
    journey.add("dependency_dropped", {"issue": 99})  # dropping an unknown one changes nothing
    assert derive(journey.raw()).dependencies == (Dependency(5, "merge"),)


def test_a_deferred_run_frees_the_slot_for_a_later_attempt() -> None:
    journey = Journey().add("created", {"issue_type": "code"}).move("inbox", "triage", "dispatch")
    journey.add(
        "handoff_accepted",
        {"run_id": "r-1", "state": "triage", "commit_sha": None, "artifact_rev": None},
    )
    assert derive(journey.raw()).settled_runs == frozenset({"r-1"})
    journey.add("run_deferred", {"run_id": "r-1"})
    state = derive(journey.raw())
    assert state.settled_runs == frozenset() and state.status is Status.TRIAGE


def test_typing_an_issue_is_only_allowed_while_it_is_in_the_inbox() -> None:
    journey = Journey().add("created", {"issue_type": "code"})
    journey.add("issue_typed", {"issue_type": "hotfix"})
    assert derive(journey.raw()).issue_type is IssueType.HOTFIX
    journey.move("inbox", "triage", "dispatch")
    journey.add("issue_typed", {"issue_type": "code"})
    assert derive(journey.raw()).status is Status.BLOCKED
