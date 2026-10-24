"""decide(): derived state + accepted handoffs + valid commands -> the next events to post."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from concert.core.ci import CheckRun
from concert.core.commands import Actor, Command, Role
from concert.core.counters import AgentState
from concert.core.decide import (
    AcceptedHandoff,
    Answer,
    Proposed,
    ValidCommand,
    decide,
    plan_integration,
    plan_merged,
    plan_revert_ci,
    plan_verify_failure,
)
from concert.core.derive import IssueState, derive
from concert.core.events import Event, append_event
from concert.core.evidence import GatePolicy
from concert.core.handoff import Accepted, parse_handoff
from concert.core.transitions import IssueType, Status, Trigger

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


def test_a_revert_approval_is_evidence_not_a_transition() -> None:
    log = Log("revert").move("implementing", "human_uat", "ci_green")
    log.add(
        "handoff_accepted",
        {"run_id": "r-1", "state": "implementing", "commit_sha": "rev1", "artifact_rev": None},
    )
    proposals = decide(log.state(), [], [command(4, "approve", "revert", "rev1", ADMIN)])
    assert kinds(proposals) == ["gate_passage", "evidence"]
    assert proposals[1].data == {
        "kind": "approval",
        "item": "revert",
        "head_sha": "rev1",
        "passed": True,
        "actor_id": ADMIN.user_id,
    }
    assert log.apply(proposals).state().status is Status.HUMAN_UAT


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


# --- facts the poll loop needs ----------------------------------------------------------------


def test_an_accepted_handoff_event_carries_the_state_commit_and_artifact() -> None:
    log = Log().move("inbox", "triage", "dispatch").move("triage", "spec", "spec")
    item = handoff("r-2", "spec", "ready_for_approval")
    committing = AcceptedHandoff(
        parse_handoff(
            {
                "schema": 1,
                "state": "spec",
                "run_id": "r-2",
                "runner": "vm-1",
                "exit": "ready_for_approval",
                "commit_sha": "aaa1111",
                "artifact_rev": "spec1",
            }
        ),
        Accepted(trigger=Trigger.READY_FOR_APPROVAL),
        item.comment_id,
    )
    [accepted, _move] = decide(log.state(), [committing], [])
    assert accepted.data == {
        "run_id": "r-2",
        "state": "spec",
        "commit_sha": "aaa1111",
        "artifact_rev": "spec1",
        "doubts": [],
    }
    state = log.apply(decide(log.state(), [committing], [])).state()
    assert state.spec_rev == "spec1" and state.head_sha == "aaa1111"


def test_approving_the_spec_records_the_hash_that_was_approved() -> None:
    log = log_at("spec_approval")
    log.add(
        "handoff_accepted",
        {"run_id": "r-2", "state": "spec", "commit_sha": "aaa1111", "artifact_rev": "spec1"},
    )
    proposals = decide(log.state(), [], [command(7, "approve", "spec", "spec1")])
    assert proposals[0].data["spec_hash"] == "spec1"
    assert log.apply(proposals).state().approved_spec_rev == "spec1"


# --- evidence from handoffs and approvals ----------------------------------------------------------


def head_handoff(
    run_id: str, state: str, exit_name: str, sha: str, reviewer: str | None = None
) -> AcceptedHandoff:
    base = handoff(run_id, state, exit_name, reviewer=reviewer)
    parsed = parse_handoff(
        {
            "schema": 1,
            "state": state,
            "run_id": run_id,
            "runner": "vm-1",
            "exit": exit_name,
            "commit_sha": sha,
            "artifact_rev": "spec1",
            "reviewer": reviewer,
        }
    )
    return AcceptedHandoff(parsed, base.decision, base.comment_id)


def test_a_passed_uat_run_is_recorded_as_evidence_on_the_head_it_ran_against() -> None:
    log = log_at("uat")
    proposals = decide(log.state(), [head_handoff("r-5", "uat", "passed", "h1")], [])
    assert kinds(proposals) == ["handoff_accepted", "evidence", "transition"]
    assert proposals[1].data == {"kind": "uat", "item": None, "head_sha": "h1", "passed": True}
    assert [(e.kind, e.passed) for e in log.apply(proposals).state().evidence] == [("uat", True)]


def test_a_failed_uat_run_is_recorded_as_failing_evidence() -> None:
    log = log_at("uat")
    proposals = decide(log.state(), [head_handoff("r-5", "uat", "failed", "h1")], [])
    assert kinds(proposals) == ["handoff_accepted", "evidence", "counter_charged", "transition"]
    assert proposals[1].data["passed"] is False


def test_each_reviewers_verdict_is_recorded_as_evidence() -> None:
    log = log_at("in_review")
    both = [
        head_handoff("r-7", "in_review", "changes_needed", "h1", reviewer="code"),
        head_handoff("r-8", "in_review", "clean", "h1", reviewer="security"),
    ]
    evidence = [p.data for p in decide(log.state(), both, []) if p.kind == "evidence"]
    assert evidence == [
        {"kind": "review", "item": "code", "head_sha": "h1", "passed": False},
        {"kind": "review", "item": "security", "head_sha": "h1", "passed": True},
    ]


def test_human_uat_approvals_are_evidence_bound_to_the_head() -> None:
    log = log_at("human_uat")
    log.add(
        "handoff_accepted",
        {"run_id": "r-5", "state": "uat", "commit_sha": "h1", "artifact_rev": None},
    )
    for kind in ("uat", "security"):
        proposals = decide(log.state(), [], [command(20, "approve", kind, "h1")])
        assert kinds(proposals) == ["gate_passage", "evidence"]
        assert proposals[1].data == {
            "kind": "approval",
            "item": kind,
            "head_sha": "h1",
            "passed": True,
            "actor_id": APPROVER.user_id,
        }


# --- moving to integrating -------------------------------------------------------------------------

HEAD = "h1"
POLICY = GatePolicy(
    policy_rev="p1",
    dod_items=frozenset({"tests"}),
    allowed_workflows=frozenset({"w1"}),
    approvers=frozenset({APPROVER.user_id}),
    admins=frozenset({ADMIN.user_id}),
    security_sensitive=False,
    protected_changed=False,
    issue_type=IssueType.CODE,
)


def ready_log() -> Log:
    log = log_at("human_uat")
    log.add(
        "handoff_accepted",
        {"run_id": "r-5", "state": "uat", "commit_sha": HEAD, "artifact_rev": None},
    )
    for item in (
        {
            "kind": "ci",
            "item": "tests",
            "head_sha": HEAD,
            "passed": True,
            "policy_rev": "p1",
            "workflow_hash": "w1",
        },
        {"kind": "review", "item": "code", "head_sha": HEAD, "passed": True},
        {"kind": "review", "item": "security", "head_sha": HEAD, "passed": True},
        {"kind": "uat", "item": None, "head_sha": HEAD, "passed": True},
        {
            "kind": "approval",
            "item": "uat",
            "head_sha": HEAD,
            "passed": True,
            "actor_id": APPROVER.user_id,
        },
    ):
        log.add("evidence", item)
    return log


def test_a_ready_issue_at_the_head_of_the_queue_moves_to_integrating() -> None:
    log = ready_log()
    proposals = plan_integration(log.state(), POLICY, at_queue_head=True)
    assert kinds(proposals) == ["transition"]
    assert proposals[0].data["trigger"] == "approve_uat"
    assert log.apply(proposals).state().status is Status.INTEGRATING


def test_an_issue_waits_in_human_uat_until_it_reaches_the_head_of_the_queue() -> None:
    assert plan_integration(ready_log().state(), POLICY, at_queue_head=False) == []


def test_an_issue_whose_evidence_is_missing_or_stale_does_not_move() -> None:
    log = ready_log()
    log.add(
        "evidence",
        {
            "kind": "ci",
            "item": "tests",
            "head_sha": HEAD,
            "passed": False,
            "policy_rev": "p1",
            "workflow_hash": "w1",
        },
    )
    assert plan_integration(log.state(), POLICY, at_queue_head=True) == []


def test_a_new_head_makes_the_old_evidence_stale() -> None:
    log = ready_log()
    log.add(
        "handoff_accepted",
        {"run_id": "r-6", "state": "implementing", "commit_sha": "h2", "artifact_rev": None},
    )
    assert plan_integration(log.state(), POLICY, at_queue_head=True) == []


def test_a_ready_revert_moves_to_integrating_on_the_revert_trigger() -> None:
    log = Log("revert").move("implementing", "human_uat", "ci_green")
    log.add(
        "handoff_accepted",
        {"run_id": "r-1", "state": "implementing", "commit_sha": "rev1", "artifact_rev": None},
    )
    log.add(
        "evidence",
        {
            "kind": "ci",
            "item": "tests",
            "head_sha": "rev1",
            "passed": True,
            "policy_rev": "p1",
            "workflow_hash": "w1",
        },
    )
    log.add(
        "evidence",
        {
            "kind": "approval",
            "item": "revert",
            "head_sha": "rev1",
            "passed": True,
            "actor_id": ADMIN.user_id,
        },
    )
    policy = replace(POLICY, issue_type=IssueType.REVERT, require_reviews=False)
    proposals = plan_integration(log.state(), policy, at_queue_head=True)
    assert proposals[0].data["trigger"] == "approve_revert"
    assert log.apply(proposals).state().status is Status.INTEGRATING


def test_only_issues_in_human_uat_are_planned() -> None:
    assert plan_integration(log_at("in_review").state(), POLICY, at_queue_head=True) == []


def test_only_issues_in_human_uat_are_planned_even_with_a_ready_ledger() -> None:
    log = ready_log()
    log.add("transition", {"from": "human_uat", "to": "implementing", "trigger": "reject_uat"})
    log.add(
        "handoff_accepted",
        {"run_id": "r-9", "state": "implementing", "commit_sha": HEAD, "artifact_rev": None},
    )
    assert log.state().status is Status.IMPLEMENTING
    assert plan_integration(log.state(), POLICY, at_queue_head=True) == []


def test_an_approval_before_any_head_exists_is_not_recorded() -> None:
    log = log_at("human_uat")
    assert log.state().head_sha is None
    assert decide(log.state(), [], [command(20, "approve", "uat", "h1")]) == []


# --- the merge ------------------------------------------------------------------------------------


def integrating_log() -> Log:
    log = ready_log()
    log.move("human_uat", "integrating", "approve_uat")
    return log


def integrating_handoff(exit_name: str, trigger: Trigger) -> AcceptedHandoff:
    base = head_handoff("r-9", "integrating", exit_name, HEAD)
    return AcceptedHandoff(base.handoff, Accepted(trigger=trigger), base.comment_id)


def test_ready_to_merge_is_accepted_but_the_issue_waits_for_the_merge() -> None:
    log = integrating_log()
    proposals = decide(
        log.state(), [integrating_handoff("ready_to_merge", Trigger.READY_TO_MERGE)], []
    )
    assert kinds(proposals) == ["handoff_accepted"]
    assert log.apply(proposals).state().status is Status.INTEGRATING


def test_a_rebase_still_returns_the_issue_to_review_at_once() -> None:
    log = integrating_log()
    proposals = decide(log.state(), [integrating_handoff("rebased", Trigger.REBASED)], [])
    assert kinds(proposals) == ["handoff_accepted", "transition"]
    assert log.apply(proposals).state().status is Status.IN_REVIEW


def test_a_merged_issue_moves_to_verifying_staging() -> None:
    log = integrating_log()
    log.add("outcome", {"key": "merge", "action": "merge", "result": {"sha": "m1"}})
    proposals = plan_merged(log.state())
    assert kinds(proposals) == ["transition"]
    assert proposals[0].data["trigger"] == "ready_to_merge"
    assert log.apply(proposals).state().status is Status.VERIFYING_STAGING


def test_an_issue_that_has_not_merged_stays_in_integrating() -> None:
    assert plan_merged(integrating_log().state()) == []


def test_only_integrating_issues_move_after_a_merge() -> None:
    log = integrating_log()
    log.add("outcome", {"key": "merge", "action": "merge", "result": {"sha": "m1"}})
    log.move("integrating", "verifying_staging", "ready_to_merge")
    assert plan_merged(log.state()) == []


# --- staging verification failures --------------------------------------------------------------


def verifying_log(issue_type: str = "code") -> Log:
    log = integrating_log() if issue_type == "code" else Log(issue_type)
    if issue_type == "code":
        log.add("outcome", {"key": "merge", "action": "merge", "result": {"sha": "m1"}})
        log.move("integrating", "verifying_staging", "ready_to_merge")
    return log


def verify_handoff(run_id: str, exit_name: str) -> AcceptedHandoff:
    base = head_handoff(run_id, "verifying_staging", exit_name, "m1")
    trigger = Trigger.PASSED if exit_name == "passed" else None
    return AcceptedHandoff(base.handoff, Accepted(trigger=trigger), base.comment_id)


def test_a_first_failed_verification_is_re_run_once() -> None:
    log = verifying_log()
    proposals = decide(log.state(), [verify_handoff("r-9", "failed")], [])
    assert kinds(proposals) == ["handoff_accepted", "verify_rerun"]
    state = log.apply(proposals).state()
    assert state.status is Status.VERIFYING_STAGING
    assert state.verify_failures == 1 and state.settled_runs == frozenset()


def test_a_second_failed_verification_is_recorded_as_failed() -> None:
    log = verifying_log()
    log.apply(decide(log.state(), [verify_handoff("r-9", "failed")], []))
    proposals = decide(log.state(), [verify_handoff("r-10", "failed")], [])
    assert kinds(proposals) == ["handoff_accepted", "verify_failed"]
    state = log.apply(proposals).state()
    assert state.verify_failed and state.status is Status.VERIFYING_STAGING


def test_the_number_of_re_runs_is_configurable() -> None:
    log = verifying_log()
    proposals = decide(log.state(), [verify_handoff("r-9", "failed")], [], verify_reruns=0)
    assert kinds(proposals) == ["handoff_accepted", "verify_failed"]


def test_a_passing_verification_moves_to_on_staging() -> None:
    log = verifying_log()
    proposals = decide(log.state(), [verify_handoff("r-9", "passed")], [])
    assert kinds(proposals) == ["handoff_accepted", "transition"]
    assert log.apply(proposals).state().status is Status.ON_STAGING


def failed_log(issue_type: str = "code") -> Log:
    log = verifying_log(issue_type)
    log.add("verify_failed", {"run_id": "r-10"})
    return log


def test_a_failure_with_the_freeze_policy_blocks_the_issue() -> None:
    log = failed_log()
    proposals = plan_verify_failure(log.state(), on_failure="freeze")
    assert [p.data["trigger"] for p in proposals] == ["verify_failed"]
    assert log.apply(proposals).state().status is Status.BLOCKED


def test_a_failure_with_the_auto_revert_policy_waits_for_the_revert() -> None:
    assert plan_verify_failure(failed_log().state(), on_failure="auto_revert") == []


def test_a_hotfix_failure_freezes_even_under_auto_revert() -> None:
    log = failed_log()
    state = replace(log.state(), issue_type=IssueType.HOTFIX)
    assert [p.data["trigger"] for p in plan_verify_failure(state, "auto_revert")] == [
        "verify_failed"
    ]


def test_a_revert_failure_freezes_and_is_never_reverted_again() -> None:
    state = replace(failed_log().state(), issue_type=IssueType.REVERT)
    assert [p.data["trigger"] for p in plan_verify_failure(state, "auto_revert")] == [
        "verify_failed"
    ]


def test_nothing_is_planned_before_the_failure_is_final() -> None:
    assert plan_verify_failure(verifying_log().state(), on_failure="freeze") == []


# --- auto-revert ----------------------------------------------------------------------------------


def reverting_log() -> Log:
    log = failed_log()
    log.add(
        "outcome",
        {
            "key": "revert_pr",
            "action": "revert_pr",
            "result": {"number": 9, "branch": "revert-pr-7", "sha": "rv1"},
        },
    )
    return log


def check(name: str, conclusion: str | None, sha: str = "rv1") -> CheckRun:
    return CheckRun(name, sha, conclusion, "w1")


def test_green_checks_on_the_revert_head_clear_the_revert_for_merging() -> None:
    log = reverting_log()
    proposals = plan_revert_ci(log.state(), [check("tests", "success")], frozenset({"tests"}))
    assert [(p.kind, dict(p.data)) for p in proposals] == [
        ("revert_ci", {"head": "rv1", "passed": True})
    ]
    assert log.apply(proposals).state().revert_ci_passed is True


def test_a_failing_check_on_the_revert_head_is_recorded() -> None:
    log = reverting_log()
    proposals = plan_revert_ci(log.state(), [check("tests", "failure")], frozenset({"tests"}))
    assert proposals[0].data["passed"] is False


def test_the_revert_waits_for_unfinished_or_missing_checks() -> None:
    state = reverting_log().state()
    dod = frozenset({"tests", "lint"})
    assert plan_revert_ci(state, [check("tests", "success")], dod) == []
    assert plan_revert_ci(state, [check("tests", None), check("lint", "success")], dod) == []


def test_checks_on_another_commit_do_not_count_for_the_revert() -> None:
    state = reverting_log().state()
    assert (
        plan_revert_ci(state, [check("tests", "success", sha="other")], frozenset({"tests"})) == []
    )


def test_a_decided_revert_ci_is_not_decided_again() -> None:
    log = reverting_log()
    log.add("revert_ci", {"head": "rv1", "passed": True})
    assert plan_revert_ci(log.state(), [check("tests", "failure")], frozenset({"tests"})) == []


def test_a_revert_ci_failure_freezes_the_issue() -> None:
    log = reverting_log()
    log.add("revert_ci", {"head": "rv1", "passed": False})
    proposals = plan_verify_failure(log.state(), "auto_revert")
    assert log.apply(proposals).state().status is Status.BLOCKED


def merged_revert_log() -> Log:
    log = reverting_log()
    log.add("revert_ci", {"head": "rv1", "passed": True})
    log.add("outcome", {"key": "merge_revert", "action": "merge_revert", "result": {"sha": "rm1"}})
    return log


def test_merging_the_revert_starts_a_fresh_verification() -> None:
    log = reverting_log()
    log.add(
        "handoff_accepted",
        {"run_id": "r-x", "state": "verifying_staging", "commit_sha": "m1", "artifact_rev": None},
    )
    log.add("revert_ci", {"head": "rv1", "passed": True})
    assert log.state().settled_runs == frozenset({"r-x"}) and log.state().verify_failed
    log.add("outcome", {"key": "merge_revert", "action": "merge_revert", "result": {"sha": "rm1"}})
    state = log.state()
    assert state.revert_merge_sha == "rm1"
    assert state.verify_failed is False and state.settled_runs == frozenset()
    assert state.verify_failures == 0


def test_a_verified_revert_returns_the_issue_to_implementing() -> None:
    log = merged_revert_log()
    proposals = decide(log.state(), [verify_handoff("r-11", "passed")], [])
    assert kinds(proposals) == ["handoff_accepted", "transition"]
    assert proposals[1].data["trigger"] == "failed_reverted"
    assert log.apply(proposals).state().status is Status.IMPLEMENTING


def test_a_revert_that_fails_verification_freezes_even_under_auto_revert() -> None:
    log = merged_revert_log()
    log.apply(decide(log.state(), [verify_handoff("r-11", "failed")], []))
    log.apply(decide(log.state(), [verify_handoff("r-12", "failed")], []))
    assert log.state().verify_failed
    proposals = plan_verify_failure(log.state(), "auto_revert")
    assert log.apply(proposals).state().status is Status.BLOCKED


def test_merging_the_revert_ends_runs_still_in_flight() -> None:
    log = reverting_log()
    log.add(
        "assignment",
        {
            "run_id": "r-z",
            "runner": "vm-1",
            "state": "verifying_staging",
            "reviewer": None,
            "head": "m1",
            "issued_at": 1,
        },
    )
    assert len(log.state().active_runs) == 1
    log.add("outcome", {"key": "merge_revert", "action": "merge_revert", "result": {"sha": "rm1"}})
    assert log.state().active_runs == ()


# --- needs_input ----------------------------------------------------------------------------------


def asking_log() -> Log:
    log = log_at("inbox")
    log.move("inbox", "triage", "dispatch").move("triage", "spec", "spec")
    log.move("spec", "needs_input", "needs_input")
    return log


def test_the_first_answer_returns_the_issue_to_where_it_asked() -> None:
    log = asking_log()
    answers = [Answer(60, 2, "later"), Answer(50, 1, "CSV only")]
    proposals = decide(log.state(), [], [], answers=answers)
    assert [(p.kind, dict(p.data)) for p in proposals] == [
        ("answered", {"to": "spec", "comment_id": 50, "author_id": 1, "text": "CSV only"})
    ]
    assert log.apply(proposals).state().status is Status.SPEC


def test_no_answer_means_no_change() -> None:
    assert decide(asking_log().state(), [], [], answers=[]) == []


def test_answers_are_ignored_outside_needs_input() -> None:
    assert decide(log_at("uat").state(), [], [], answers=[Answer(5, 1, "x")]) == []


def test_a_very_long_answer_is_truncated_in_the_event() -> None:
    proposals = decide(asking_log().state(), [], [], answers=[Answer(5, 1, "x" * 50_000)])
    assert len(str(proposals[0].data["text"])) <= 8_000


def test_accepted_handoffs_carry_the_agents_doubts() -> None:
    log = log_at("inbox")
    log.move("inbox", "triage", "dispatch")
    item = handoff("r-3", "triage", "spec")
    parsed = parse_handoff(
        {
            "schema": 1,
            "state": "triage",
            "run_id": "r-3",
            "runner": "vm-1",
            "exit": "spec",
            "commit_sha": None,
            "artifact_rev": None,
            "doubts": ["Which formats?"],
        }
    )
    proposals = decide(log.state(), [AcceptedHandoff(parsed, item.decision, item.comment_id)], [])
    assert proposals[0].data["doubts"] == ["Which formats?"]
