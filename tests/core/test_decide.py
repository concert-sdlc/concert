"""decide(): derived state + accepted handoffs + valid commands -> the next events to post."""

from collections.abc import Mapping, Sequence
from typing import Any

from concert.core.commands import Actor, Command, Role
from concert.core.counters import AgentState
from concert.core.decide import AcceptedHandoff, Proposed, ValidCommand, decide
from concert.core.derive import IssueState, derive
from concert.core.events import Event, append_event
from concert.core.handoff import Accepted, parse_handoff
from concert.core.transitions import Status, Trigger

APPROVER = Actor(user_id=2, login="pat", is_bot=False, role=Role.APPROVER)
ADMIN = Actor(user_id=1, login="alice", is_bot=False, role=Role.ADMIN)


class Log:
    """An event log we can extend with proposals and re-derive."""

    def __init__(self, issue_type: str = "code") -> None:
        self.events: list[Event] = []
        self.add("created", {"issue_type": issue_type})

    def add(self, kind: str, data: Mapping[str, Any]) -> "Log":
        self.events.append(append_event(self.events, epoch=1, kind=kind, data=data))
        return self

    def move(self, src: str, dst: str, trigger: str) -> "Log":
        return self.add("transition", {"from": src, "to": dst, "trigger": trigger})

    def state(self, budget: int = 4) -> IssueState:
        return derive([e.to_dict() for e in self.events], budget=budget)

    def apply(self, proposals: Sequence[Proposed]) -> "Log":
        for proposal in proposals:
            self.add(proposal.kind, proposal.data)
        return self


def log_at(status: str) -> Log:
    """A log whose derived status is `status`, along the main path."""
    path = [
        ("inbox", "triage", "dispatch"),
        ("triage", "spec", "spec"),
        ("spec", "spec_approval", "ready_for_approval"),
        ("spec_approval", "implementing", "approve_spec"),
        ("implementing", "in_review", "done"),
        ("in_review", "uat", "clean"),
        ("uat", "human_uat", "passed"),
    ]
    log = Log()
    if status == "inbox":
        return log
    for src, dst, trigger in path:
        log.move(src, dst, trigger)
        if dst == status:
            return log
    raise AssertionError(status)


def handoff(
    run_id: str, state: str, exit_name: str, reviewer: str | None = None
) -> AcceptedHandoff:
    parsed = parse_handoff(
        {
            "schema": 1,
            "state": state,
            "run_id": run_id,
            "runner": "vm-1",
            "exit": exit_name,
            "commit_sha": None,
            "artifact_rev": None,
            "reviewer": reviewer,
        }
    )
    trigger = {
        ("triage", "spec"): Trigger.SPEC,
        ("implementing", "done"): Trigger.DONE,
        ("in_review", "clean"): Trigger.CLEAN,
        ("in_review", "changes_needed"): Trigger.CHANGES_NEEDED,
        ("uat", "failed"): Trigger.FAILED,
        ("uat", "passed"): Trigger.PASSED,
    }.get((state, exit_name))
    return AcceptedHandoff(parsed, Accepted(trigger=trigger), comment_id=int(run_id[2:]))


def spec_drift_handoff(run_id: str) -> AcceptedHandoff:
    base = handoff(run_id, "implementing", "done")
    return AcceptedHandoff(
        base.handoff, Accepted(trigger=Trigger.SPEC_DRIFT, spec_drift=True), base.comment_id
    )


def command(
    comment_id: int, verb: str, kind: str | None = None, arg: str = "", actor: Actor = APPROVER
) -> ValidCommand:
    return ValidCommand(
        comment_id=comment_id,
        command=Command(verb, kind, arg),
        actor=actor,
        body=f"/{verb} {kind or ''} {arg}".strip(),
        created_at=100 + comment_id,
    )


def kinds(proposals: Sequence[Proposed]) -> list[str]:
    return [p.kind for p in proposals]


# --- nothing to do -----------------------------------------------------------------------------


def test_with_no_input_nothing_is_proposed() -> None:
    assert decide(log_at("inbox").state(), [], []) == []


def test_a_blocked_or_terminal_issue_proposes_nothing_but_unblock() -> None:
    blocked = Log().move("inbox", "triage", "dispatch").move("triage", "blocked", "budget_exceeded")
    assert decide(blocked.state(), [handoff("r-1", "triage", "spec")], []) == []


# --- gates ------------------------------------------------------------------------------------


def test_dispatch_records_the_gate_passage_and_moves_to_triage() -> None:
    log = log_at("inbox")
    proposals = decide(log.state(), [], [command(5, "dispatch")])
    assert kinds(proposals) == ["gate_passage", "transition"]
    assert proposals[0].data["comment_id"] == 5
    assert proposals[0].data["author_id"] == APPROVER.user_id
    assert log.apply(proposals).state().status is Status.TRIAGE


def test_a_command_already_consumed_is_not_applied_twice() -> None:
    log = log_at("inbox")
    log.apply(decide(log.state(), [], [command(5, "dispatch")]))
    assert decide(log.state(), [], [command(5, "dispatch")]) == []


def test_a_command_that_does_not_apply_to_the_state_is_ignored() -> None:
    assert decide(log_at("inbox").state(), [], [command(5, "approve", "spec", "abc1234")]) == []


def test_approve_spec_moves_to_implementing_without_a_charge() -> None:
    log = log_at("spec_approval")
    proposals = decide(log.state(), [], [command(7, "approve", "spec", "abc1234")])
    assert kinds(proposals) == ["gate_passage", "transition"]
    assert log.apply(proposals).state().status is Status.IMPLEMENTING


def test_reject_spec_charges_the_spec_counter_and_returns_to_spec() -> None:
    log = log_at("spec_approval")
    proposals = decide(log.state(), [], [command(7, "reject", "spec", "too vague")])
    assert kinds(proposals) == ["gate_passage", "counter_charged", "transition"]
    state = log.apply(proposals).state()
    assert state.status is Status.SPEC
    assert state.counters.used(AgentState.SPEC) == 1


def test_reject_uat_charges_implementing_and_returns_there() -> None:
    log = log_at("human_uat")
    proposals = decide(log.state(), [], [command(9, "reject", "uat", "export is slow")])
    state = log.apply(proposals).state()
    assert state.status is Status.IMPLEMENTING
    assert state.counters.used(AgentState.IMPLEMENTING) == 1


def test_a_gate_rejection_at_the_budget_limit_blocks_instead_of_charging() -> None:
    log = log_at("human_uat")
    for _ in range(4):
        log.add("counter_charged", {"cause": "reject_uat"})
    proposals = decide(log.state(), [], [command(9, "reject", "uat", "again")])
    assert kinds(proposals) == ["gate_passage", "transition"]
    assert proposals[1].data["trigger"] == "budget_exceeded"
    assert log.apply(proposals).state().status is Status.BLOCKED


def test_a_rejected_revert_goes_to_blocked_without_a_charge() -> None:
    log = Log("revert").move("implementing", "human_uat", "ci_green")
    proposals = decide(log.state(), [], [command(4, "reject", "revert", "wrong merge", ADMIN)])
    state = log.apply(proposals).state()
    assert state.status is Status.BLOCKED
    assert state.counters.used(AgentState.IMPLEMENTING) == 0


def test_an_approved_revert_goes_to_integrating() -> None:
    log = Log("revert").move("implementing", "human_uat", "ci_green")
    proposals = decide(log.state(), [], [command(4, "approve", "revert", "abc1234", ADMIN)])
    assert log.apply(proposals).state().status is Status.INTEGRATING


def test_unblock_resets_the_target_counter_and_moves_to_the_target() -> None:
    log = log_at("spec_approval").move("spec_approval", "implementing", "approve_spec")
    for _ in range(4):
        log.add("counter_charged", {"cause": "changes_needed"})
    log.move("implementing", "blocked", "budget_exceeded")
    proposals = decide(log.state(), [], [command(11, "unblock", None, "implementing")])
    assert kinds(proposals) == ["gate_passage", "counter_reset", "unblocked"]
    state = log.apply(proposals).state()
    assert state.status is Status.IMPLEMENTING
    assert state.counters.used(AgentState.IMPLEMENTING) == 0


# --- handoffs ---------------------------------------------------------------------------------


def test_an_accepted_triage_handoff_moves_the_issue_to_spec() -> None:
    log = Log().move("inbox", "triage", "dispatch")
    proposals = decide(log.state(), [handoff("r-3", "triage", "spec")], [])
    assert kinds(proposals) == ["handoff_accepted", "transition"]
    assert log.apply(proposals).state().status is Status.SPEC


def test_a_handoff_is_accepted_only_once() -> None:
    log = Log().move("inbox", "triage", "dispatch")
    log.apply(decide(log.state(), [handoff("r-3", "triage", "spec")], []))
    assert decide(log.state(), [handoff("r-3", "triage", "spec")], []) == []


def test_a_handoff_for_another_state_is_left_alone() -> None:
    log = Log().move("inbox", "triage", "dispatch")
    assert decide(log.state(), [handoff("r-3", "implementing", "done")], []) == []


def test_an_implementing_handoff_moves_to_review_without_a_charge() -> None:
    log = log_at("implementing")
    proposals = decide(log.state(), [handoff("r-4", "implementing", "done")], [])
    assert kinds(proposals) == ["handoff_accepted", "transition"]
    assert log.apply(proposals).state().status is Status.IN_REVIEW


def test_a_failed_uat_run_is_charged_and_returns_to_implementing() -> None:
    log = log_at("uat")
    proposals = decide(log.state(), [handoff("r-5", "uat", "failed")], [])
    assert kinds(proposals) == ["handoff_accepted", "counter_charged", "transition"]
    state = log.apply(proposals).state()
    assert state.status is Status.IMPLEMENTING
    assert state.counters.used(AgentState.IMPLEMENTING) == 1


def test_spec_drift_overrides_the_exit_and_is_charged() -> None:
    log = log_at("implementing")
    proposals = decide(log.state(), [spec_drift_handoff("r-6")], [])
    state = log.apply(proposals).state()
    assert state.status is Status.SPEC_APPROVAL
    assert state.counters.used(AgentState.IMPLEMENTING) == 1


def test_exceeding_the_budget_blocks_instead_of_charging() -> None:
    log = log_at("uat")
    for _ in range(4):
        log.add("counter_charged", {"cause": "uat_failed"})
    proposals = decide(log.state(), [handoff("r-5", "uat", "failed")], [])
    assert kinds(proposals) == ["handoff_accepted", "transition"]
    assert proposals[1].data["trigger"] == "budget_exceeded"
    assert log.apply(proposals).state().status is Status.BLOCKED


# --- reviewers ---------------------------------------------------------------------------------


def test_review_waits_until_both_reviewers_have_reported() -> None:
    log = log_at("in_review")
    only_code = [handoff("r-7", "in_review", "clean", reviewer="code")]
    assert decide(log.state(), only_code, []) == []


def test_two_clean_reviews_move_to_uat() -> None:
    log = log_at("in_review")
    both = [
        handoff("r-7", "in_review", "clean", reviewer="code"),
        handoff("r-8", "in_review", "clean", reviewer="security"),
    ]
    proposals = decide(log.state(), both, [])
    assert kinds(proposals) == ["handoff_accepted", "handoff_accepted", "transition"]
    assert log.apply(proposals).state().status is Status.UAT


def test_a_review_round_with_findings_costs_one_reentry_not_one_per_reviewer() -> None:
    log = log_at("in_review")
    both = [
        handoff("r-7", "in_review", "changes_needed", reviewer="code"),
        handoff("r-8", "in_review", "changes_needed", reviewer="security"),
    ]
    state = log.apply(decide(log.state(), both, [])).state()
    assert state.status is Status.IMPLEMENTING
    assert state.counters.used(AgentState.IMPLEMENTING) == 1


def test_one_command_per_call_and_the_earliest_first() -> None:
    log = log_at("inbox")
    proposals = decide(log.state(), [], [command(9, "dispatch"), command(5, "dispatch")])
    assert proposals[0].data["comment_id"] == 5
    assert kinds(proposals).count("transition") == 1


# --- replay and precedence ---------------------------------------------------------------------


def test_a_handoff_that_leaves_the_state_unchanged_is_still_accepted_only_once() -> None:
    log = Log().move("inbox", "triage", "dispatch")
    waiting = handoff("r-3", "triage", "blocked_by")  # waits on a dependency: no transition
    first = decide(log.state(), [waiting], [])
    assert kinds(first) == ["handoff_accepted"]
    log.apply(first)
    assert decide(log.state(), [waiting], []) == []


def test_an_old_command_is_not_replayed_when_the_issue_returns_to_the_same_gate() -> None:
    log = log_at("spec_approval")
    rejection = command(7, "reject", "spec", "too vague")
    log.apply(decide(log.state(), [], [rejection]))
    assert log.state().status is Status.SPEC
    log.move("spec", "spec_approval", "ready_for_approval")  # a revised spec is ready again
    assert decide(log.state(), [], [rejection]) == []


def test_needs_prerequisite_wins_over_findings_and_nothing_is_charged() -> None:
    log = log_at("in_review")
    both = [
        handoff("r-7", "in_review", "needs_prerequisite", reviewer="code"),
        handoff("r-8", "in_review", "changes_needed", reviewer="security"),
    ]
    proposals = decide(log.state(), both, [])
    assert kinds(proposals) == ["handoff_accepted", "handoff_accepted"]
    assert log.apply(proposals).state().counters.used(AgentState.IMPLEMENTING) == 0
