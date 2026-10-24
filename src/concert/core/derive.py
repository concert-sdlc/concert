"""derive(): fold the trusted pm-event chain into the issue state.

Pure. The caller (the github module) has already dropped events that were not authored by
the PM App or that were ever edited; derive() enforces the rest: schema support, chain
integrity (no gap, no hash break), the coordinator epoch rule and the transition table.
Any failure sends the issue to `blocked` with the reason shown, never a guess.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from concert.core.counters import AgentState, Cause, Outcome, RetryCounters, charge_target
from concert.core.events import (
    ChainError,
    Event,
    MalformedEvent,
    UnsupportedSchema,
    parse_event,
    verify_chain,
)
from concert.core.evidence import Evidence
from concert.core.transitions import (
    InvalidTransition,
    IssueType,
    Status,
    Trigger,
    initial_status,
    next_status,
)

DEFAULT_BUDGET = 4

# Moving back to the spec or to its approval gate voids the approval.
_UNAPPROVED_TARGETS = frozenset({Status.SPEC, Status.SPEC_APPROVAL})

# Event kinds that are part of the log but do not change status or counters.
_RECORD_ONLY = frozenset({"assignment", "reconciled", "admin_command"})


@dataclass(frozen=True)
class ActiveRun:
    run_id: str
    runner: str
    state: str
    reviewer: str | None
    head: str | None  # the branch head the run started from
    issued_at: int


@dataclass(frozen=True)
class Intent:
    """An external side effect the PM announced and has not yet recorded an outcome for."""

    key: str
    action: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class IssueState:
    status: Status = Status.INBOX
    issue_type: IssueType = IssueType.CODE
    counters: RetryCounters = field(default_factory=lambda: RetryCounters(budget=DEFAULT_BUDGET))
    blocked_reason: str | None = None
    last_seq: int = 0
    accepted_runs: frozenset[str] = frozenset()  # run_ids whose handoff the PM accepted
    consumed_commands: frozenset[int] = frozenset()  # comment ids of commands that passed a gate
    spec_rev: str | None = None  # spec hash of the latest accepted spec run
    approved_spec_rev: str | None = None  # the spec hash a human approved, until the spec changes
    head_sha: str | None = None  # the commit of the latest accepted committing run
    active_runs: tuple[ActiveRun, ...] = ()  # assigned, not yet accepted or expired
    assignments_made: int = 0
    settled_runs: frozenset[str] = frozenset()  # accepted in this state, no transition yet
    settled_reviewers: frozenset[str] = frozenset()
    infra_attempts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    last_expiry_at: int | None = None
    note: str | None = None  # why a check blocked the issue
    branch_base: str | None = None  # the branch-creation commit recorded by the PM
    pending_intents: tuple[Intent, ...] = ()
    pr_number: int | None = None
    pr_ready: bool = False
    evidence: tuple[Evidence, ...] = ()  # results and approvals, in order, each bound to a head


class _Conflict(Exception):
    """The event contradicts the state derived so far."""


def derive(raw_events: Sequence[Mapping[str, Any]], budget: int = DEFAULT_BUDGET) -> IssueState:
    state = IssueState(counters=RetryCounters(budget=budget))
    try:
        events = [parse_event(raw) for raw in raw_events]
        verify_chain(events)
    except UnsupportedSchema as error:
        return _blocked(state, str(error))
    except MalformedEvent as error:
        return _blocked(state, f"malformed event: {error}")
    except ChainError as error:
        return _blocked(state, str(error))

    epoch = 0
    for event in events:
        state = replace(state, last_seq=event.seq)
        if event.epoch < epoch:
            continue  # posted by a stale coordinator: ignored in re-derivation
        epoch = event.epoch
        try:
            state = _apply(state, event)
        except _Conflict as error:
            return _blocked(state, str(error))
        except (InvalidTransition, ValueError, KeyError) as error:
            return _blocked(state, _reason(error))
    return state


def _fresh_state_bookkeeping(state: IssueState) -> IssueState:
    """Moving to another state ends the runs, settled results and infra attempts of the old one."""
    return replace(
        state,
        active_runs=(),
        settled_runs=frozenset(),
        settled_reviewers=frozenset(),
        infra_attempts=MappingProxyType({}),
        last_expiry_at=None,
    )


def _reason(error: Exception) -> str:
    if isinstance(error, InvalidTransition):
        return f"invalid transition: {error}"
    return f"malformed event: {error}"


def _blocked(state: IssueState, reason: str) -> IssueState:
    return replace(state, status=Status.BLOCKED, blocked_reason=reason)


def _apply(state: IssueState, event: Event) -> IssueState:
    kind = event.kind
    if kind == "created":
        issue_type = IssueType(event.data["issue_type"])
        return replace(state, issue_type=issue_type, status=initial_status(issue_type))
    if kind == "transition":
        source = Status(event.data["from"])
        target = Status(event.data["to"])
        trigger = Trigger(event.data["trigger"])
        if source is not state.status:
            raise _Conflict(
                f"conflict at seq {event.seq}: transition from {source.value} "
                f"but the issue is in {state.status.value}"
            )
        derived = next_status(source, trigger, state.issue_type)
        if derived is not target:
            raise _Conflict(
                f"conflict at seq {event.seq}: {trigger.value} from {source.value} leads to "
                f"{derived.value}, the event says {target.value}"
            )
        approved = None if target in _UNAPPROVED_TARGETS else state.approved_spec_rev
        note = event.data.get("note")
        return replace(
            _fresh_state_bookkeeping(state),
            status=target,
            approved_spec_rev=approved,
            note=str(note) if note is not None else None,
        )
    if kind == "counter_charged":
        counters, outcome = state.counters.charge(Cause(event.data["cause"]))
        if outcome is Outcome.BLOCKED:
            target_state = charge_target(Cause(event.data["cause"]))
            raise _Conflict(f"retry budget exceeded for {target_state.value}")
        return replace(state, counters=counters)
    if kind == "counter_reset":
        return replace(state, counters=state.counters.reset(AgentState(event.data["state"])))
    if kind == "branch_created":
        return replace(state, branch_base=str(event.data["sha"]))
    if kind == "assignment":
        data = event.data
        run = ActiveRun(
            run_id=str(data["run_id"]),
            runner=str(data["runner"]),
            state=str(data["state"]),
            reviewer=data.get("reviewer"),
            head=data.get("head"),
            issued_at=int(data["issued_at"]),
        )
        return replace(
            state,
            active_runs=(*state.active_runs, run),
            assignments_made=state.assignments_made + 1,
        )
    if kind == "lease_expired":
        data = event.data
        expired_state = str(data["state"])
        attempts = {
            **state.infra_attempts,
            expired_state: state.infra_attempts.get(expired_state, 0) + 1,
        }
        return replace(
            state,
            active_runs=tuple(r for r in state.active_runs if r.run_id != data["run_id"]),
            infra_attempts=MappingProxyType(attempts),
            last_expiry_at=int(data["at"]),
        )
    if kind == "handoff_accepted":
        data = event.data
        finished = next((r for r in state.active_runs if r.run_id == data["run_id"]), None)
        state = replace(
            state,
            accepted_runs=state.accepted_runs | {str(data["run_id"])},
            active_runs=tuple(r for r in state.active_runs if r.run_id != data["run_id"]),
            settled_runs=state.settled_runs | {str(data["run_id"])},
            settled_reviewers=state.settled_reviewers
            | ({finished.reviewer} if finished is not None and finished.reviewer else set()),
        )
        if data.get("state") == "spec" and data.get("artifact_rev") is not None:
            state = replace(state, spec_rev=str(data["artifact_rev"]))
        if data.get("commit_sha") is not None:
            state = replace(state, head_sha=str(data["commit_sha"]))
        return state
    if kind == "intent":
        data = dict(event.data)
        key, action = str(data.pop("key")), str(data.pop("action"))
        return replace(
            state,
            pending_intents=(
                *(i for i in state.pending_intents if i.key != key),
                Intent(key, action, data),
            ),
        )
    if kind == "outcome":
        data = event.data
        key = str(data["key"])
        pending = next((i for i in state.pending_intents if i.key == key), None)
        action = pending.action if pending is not None else str(data.get("action"))
        result: Mapping[str, Any] = data.get("result") or {}
        state = replace(
            state, pending_intents=tuple(i for i in state.pending_intents if i.key != key)
        )
        if action == "open_pr":
            return replace(state, pr_number=int(result["number"]))
        if action == "mark_ready":
            return replace(state, pr_ready=True)
        return state
    if kind == "evidence":
        data = event.data
        found = Evidence(
            kind=str(data["kind"]),
            item=None if data.get("item") is None else str(data["item"]),
            head_sha=str(data["head_sha"]),
            passed=bool(data["passed"]),
            policy_rev=None if data.get("policy_rev") is None else str(data["policy_rev"]),
            workflow_hash=None if data.get("workflow_hash") is None else str(data["workflow_hash"]),
            actor_id=None if data.get("actor_id") is None else int(data["actor_id"]),
        )
        return replace(state, evidence=(*state.evidence, found))
    if kind == "gate_passage":
        data = event.data
        state = replace(
            state, consumed_commands=state.consumed_commands | {int(data["comment_id"])}
        )
        if data.get("verb") == "approve" and data.get("kind") == "spec" and data.get("spec_hash"):
            state = replace(state, approved_spec_rev=str(data["spec_hash"]))
        return state
    if kind == "unblocked":
        if state.status is not Status.BLOCKED:
            raise _Conflict(
                f"conflict at seq {event.seq}: unblocked but the issue is in {state.status.value}"
            )
        return replace(_fresh_state_bookkeeping(state), status=Status(event.data["to"]), note=None)
    if kind in _RECORD_ONLY:
        return state
    raise ValueError(f"unknown event kind {kind!r} at seq {event.seq}")
