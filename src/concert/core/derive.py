"""derive(): fold the trusted pm-event chain into the issue state.

Pure. The caller (the github module) has already dropped events that were not authored by
the PM App or that were ever edited; derive() enforces the rest: schema support, chain
integrity (no gap, no hash break), the coordinator epoch rule and the transition table.
Any failure sends the issue to `blocked` with the reason shown, never a guess.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from concert.core.transitions import (
    InvalidTransition,
    IssueType,
    Status,
    Trigger,
    initial_status,
    next_status,
)

DEFAULT_BUDGET = 4

# Event kinds that are part of the log but do not change status or counters.
_RECORD_ONLY = frozenset(
    {"assignment", "intent", "outcome", "reconciled", "admin_command", "gate_passage"}
)


@dataclass(frozen=True)
class IssueState:
    status: Status = Status.INBOX
    issue_type: IssueType = IssueType.CODE
    counters: RetryCounters = field(default_factory=lambda: RetryCounters(budget=DEFAULT_BUDGET))
    blocked_reason: str | None = None
    last_seq: int = 0


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
        return replace(state, status=target)
    if kind == "counter_charged":
        counters, outcome = state.counters.charge(Cause(event.data["cause"]))
        if outcome is Outcome.BLOCKED:
            target_state = charge_target(Cause(event.data["cause"]))
            raise _Conflict(f"retry budget exceeded for {target_state.value}")
        return replace(state, counters=counters)
    if kind == "counter_reset":
        return replace(state, counters=state.counters.reset(AgentState(event.data["state"])))
    if kind in _RECORD_ONLY:
        return state
    raise ValueError(f"unknown event kind {kind!r} at seq {event.seq}")
