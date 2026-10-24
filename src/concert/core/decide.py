"""decide(): the next events to post for one issue.

Pure. Input is the state derived from trusted events, the handoffs `accept_handoff` accepted
and the commands `validate_command` accepted. Output is the events the PM should append
(it assigns seq, prev and epoch). One step per call: one accepted handoff (or one review
round) or one command, so every poll is small and re-derivable.

Gates that need more evidence than a command (`approve uat`, `approve security`,
protected-path approvals, merge preconditions) are not decided here yet.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from concert.core.commands import Actor, Command
from concert.core.counters import AgentState, Cause, Outcome
from concert.core.derive import IssueState
from concert.core.handoff import Accepted, Handoff, consolidate_reviews
from concert.core.transitions import IssueType, Status, Trigger, is_terminal, next_status

REVIEWERS = frozenset({"code", "security"})


@dataclass(frozen=True)
class Proposed:
    kind: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class AcceptedHandoff:
    handoff: Handoff
    decision: Accepted
    comment_id: int


@dataclass(frozen=True)
class ValidCommand:
    comment_id: int
    command: Command
    actor: Actor
    body: str
    created_at: int


# A rejection or return is a re-entry, charged at the returning transition.
_CAUSE: dict[Trigger, Cause] = {
    Trigger.CHANGES_NEEDED: Cause.CHANGES_NEEDED,
    Trigger.FAILED: Cause.UAT_FAILED,
    Trigger.SPEC_DRIFT: Cause.SPEC_DRIFT,
    Trigger.REJECT_SPEC: Cause.REJECT_SPEC,
    Trigger.REJECT_UAT: Cause.REJECT_UAT,
}

# (status, issue type, verb, kind) -> trigger, for commands that settle a gate on their own.
_GATES: dict[tuple[Status, IssueType, str, str | None], Trigger] = {
    (Status.INBOX, IssueType.CODE, "dispatch", None): Trigger.DISPATCH,
    (Status.INBOX, IssueType.HOTFIX, "dispatch", None): Trigger.DISPATCH,
    (Status.SPEC_APPROVAL, IssueType.CODE, "approve", "spec"): Trigger.APPROVE_SPEC,
    (Status.SPEC_APPROVAL, IssueType.CODE, "reject", "spec"): Trigger.REJECT_SPEC,
    (Status.SPEC_APPROVAL, IssueType.HOTFIX, "approve", "spec"): Trigger.APPROVE_SPEC,
    (Status.SPEC_APPROVAL, IssueType.HOTFIX, "reject", "spec"): Trigger.REJECT_SPEC,
    (Status.HUMAN_UAT, IssueType.CODE, "reject", "uat"): Trigger.REJECT_UAT,
    (Status.HUMAN_UAT, IssueType.HOTFIX, "reject", "uat"): Trigger.REJECT_UAT,
    (Status.HUMAN_UAT, IssueType.REVERT, "approve", "revert"): Trigger.APPROVE_REVERT,
    (Status.HUMAN_UAT, IssueType.REVERT, "reject", "revert"): Trigger.REJECT_REVERT,
}

_COUNTED_STATES = {state.value: state for state in AgentState}


def decide(
    state: IssueState,
    handoffs: Sequence[AcceptedHandoff],
    commands: Sequence[ValidCommand],
) -> list[Proposed]:
    if state.blocked_reason is not None or is_terminal(state.status):
        return []
    from_handoffs = _from_handoffs(state, handoffs)
    if from_handoffs:
        return from_handoffs
    return _from_commands(state, commands)


# --- handoffs --------------------------------------------------------------------------------


def _from_handoffs(state: IssueState, handoffs: Sequence[AcceptedHandoff]) -> list[Proposed]:
    pending = sorted(
        (
            h
            for h in handoffs
            if h.handoff.run_id not in state.accepted_runs
            and h.handoff.state.value == state.status.value
        ),
        key=lambda h: h.comment_id,
    )
    if not pending:
        return []
    if state.status is Status.IN_REVIEW:
        return _review_round(state, pending)
    first = pending[0]
    return [_accepted(first), *_trigger_events(state, first.decision.trigger)]


def _review_round(state: IssueState, pending: Sequence[AcceptedHandoff]) -> list[Proposed]:
    """Reviewers run in parallel at one head; the round settles once, when all have reported."""
    by_reviewer: dict[str, AcceptedHandoff] = {}
    for item in pending:
        if item.handoff.reviewer is not None:
            by_reviewer.setdefault(item.handoff.reviewer, item)
    if not REVIEWERS <= by_reviewer.keys():
        return []
    used = [by_reviewer[name] for name in sorted(REVIEWERS)]
    outcome = consolidate_reviews([item.handoff.exit for item in used])
    trigger = {"clean": Trigger.CLEAN, "changes_needed": Trigger.CHANGES_NEEDED}.get(outcome)
    return [*(_accepted(item) for item in used), *_trigger_events(state, trigger)]


def _accepted(item: AcceptedHandoff) -> Proposed:
    return Proposed("handoff_accepted", {"run_id": item.handoff.run_id})


# --- commands --------------------------------------------------------------------------------


def _from_commands(state: IssueState, commands: Sequence[ValidCommand]) -> list[Proposed]:
    for item in sorted(commands, key=lambda c: c.comment_id):
        if item.comment_id in state.consumed_commands:
            continue
        passage = Proposed("gate_passage", _snapshot(item))
        if state.status is Status.BLOCKED and item.command.verb == "unblock":
            return [passage, *_unblock_events(item.command.arg.strip())]
        key = (state.status, state.issue_type, item.command.verb, item.command.kind)
        trigger = _GATES.get(key)
        if trigger is not None:
            return [passage, *_trigger_events(state, trigger)]
    return []


def _snapshot(item: ValidCommand) -> dict[str, Any]:
    """Gate-passage events snapshot the command they consumed."""
    return {
        "comment_id": item.comment_id,
        "author_id": item.actor.user_id,
        "author_login": item.actor.login,
        "verb": item.command.verb,
        "kind": item.command.kind,
        "arg": item.command.arg,
        "body": item.body,
        "created_at": item.created_at,
    }


def _unblock_events(target: str) -> list[Proposed]:
    events: list[Proposed] = []
    counted = _COUNTED_STATES.get(target)
    if counted is not None:
        events.append(Proposed("counter_reset", {"state": counted.value}))
    events.append(Proposed("unblocked", {"to": target}))
    return events


# --- shared ----------------------------------------------------------------------------------


def _trigger_events(state: IssueState, trigger: Trigger | None) -> list[Proposed]:
    """The events for one trigger: a charge first when the return is a re-entry. If the
    charge would exceed the budget the issue goes to blocked instead, uncharged."""
    if trigger is None:
        return []
    cause = _CAUSE.get(trigger)
    charge: list[Proposed] = []
    if cause is not None:
        _counters, outcome = state.counters.charge(cause)
        if outcome is Outcome.BLOCKED:
            trigger = Trigger.BUDGET_EXCEEDED
        else:
            charge = [Proposed("counter_charged", {"cause": cause.value})]
    target = next_status(state.status, trigger, state.issue_type)
    move = Proposed(
        "transition", {"from": state.status.value, "to": target.value, "trigger": trigger.value}
    )
    return [*charge, move]
