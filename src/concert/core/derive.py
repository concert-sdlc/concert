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
from concert.core.dependencies import Dependency
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
    is_terminal,
    next_status,
)

DEFAULT_BUDGET = 4

# Moving back to the spec or to its approval gate voids the approval.
_UNAPPROVED_TARGETS = frozenset({Status.INBOX, Status.SPEC, Status.SPEC_APPROVAL})

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
    branch: str | None = None  # the issue branch (pm/<n>, or pm/<n>-r<k> after a re-landing)
    relands: int = 0  # how many times the merged work was reverted and the issue restarted
    reland_of: str | None = (
        None  # the reverted merge commit whose changes the new branch re-applies
    )
    pending_intents: tuple[Intent, ...] = ()
    kits_posted: frozenset[str] = frozenset()  # keys of the gate kits already posted
    pr_number: int | None = None
    pr_ready: bool = False
    ready_toggles: int = 0  # how many times the PR went ready or back to draft
    revert_pr: int | None = None  # the auto-revert PR
    revert_branch: str | None = None
    revert_head: str | None = None  # its head commit
    revert_ci_passed: bool | None = None  # None until the checks on its head are decided
    revert_merge_sha: str | None = None  # the revert commit on the base branch, once merged
    asked_from: Status | None = None  # where a needs_input issue asked, and goes back to
    decisions: tuple[str, ...] = ()  # the notable choices of the latest accepted run
    closed_posted: frozenset[str] = frozenset()  # keys of issue closes already done
    doubts: tuple[str, ...] = ()  # the questions of the latest accepted run
    question_comment_id: int | None = None  # the PM's question comment, answers come after it
    questions_posted: frozenset[str] = frozenset()
    answers: tuple[str, ...] = ()  # trusted humans' answers, oldest first
    dependencies: tuple[Dependency, ...] = ()  # what this issue waits on, oldest first
    deferrals: int = 0  # runs deferred behind a dependency since the issue entered this state
    close_noted: bool = False  # the issue was closed after its work merged; noted once
    dispatched_at: int = 0  # when the latest /dispatch was posted
    froze_queue: bool = False  # a failed staging verification froze the queue; cleared on unblock
    verify_failures: int = 0  # failed staging verifications so far, re-runs included
    verify_failed: bool = False  # the failure is final: no re-run is left
    merge_sha: str | None = None  # the merge commit on the base branch, once the PM merged
    evidence: tuple[Evidence, ...] = ()  # results and approvals, in order, each bound to a head
    refreshes: int = 0  # pre-gate refreshes done (they are bounded and never charged)
    refresh_conflicts: int = 0  # rebases onto staging that conflicted
    refresh_result: str | None = None  # "clean" or "conflict" until the issue has moved on
    refresh_note: str | None = None  # the conflicting paths
    pm_commits: tuple[str, ...] = ()  # commits the PM itself made on the issue branch (rebases)


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
        verify_failures=0,
        verify_failed=False,
        deferrals=0,
        infra_attempts=MappingProxyType({}),
        last_expiry_at=None,
        refresh_result=None,
        refresh_note=None,
    )


def _relanded(state: IssueState) -> IssueState:
    """The merge was reverted: the issue starts over on a new branch with a new PR and fresh
    evidence. The approved spec still holds; the reverted merge is remembered so the new
    branch can re-apply its changes."""
    return replace(
        state,
        relands=state.relands + 1,
        reland_of=state.merge_sha,
        branch_base=None,
        head_sha=None,
        pr_number=None,
        pr_ready=False,
        ready_toggles=0,
        merge_sha=None,
        revert_pr=None,
        revert_branch=None,
        revert_head=None,
        revert_ci_passed=None,
        revert_merge_sha=None,
        evidence=(),
    )


def _reopened(state: IssueState) -> IssueState:
    """A closed issue that is reopened starts over from the inbox: old approvals, evidence, PR
    and branch no longer count, and its work happens on a new branch generation."""
    fresh = _relanded(state)
    return replace(fresh, reland_of=None, branch=None)


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
        moved = replace(
            _fresh_state_bookkeeping(state),
            status=target,
            approved_spec_rev=approved,
            note=str(note) if note is not None else None,
        )
        if target is Status.NEEDS_INPUT:
            moved = replace(moved, asked_from=source, question_comment_id=None)
        if trigger is Trigger.FAILED_REVERTED:
            return _relanded(moved)
        if trigger is Trigger.REOPEN:
            return _reopened(moved)
        if trigger is Trigger.VERIFY_FAILED:
            return replace(moved, froze_queue=True)
        return moved
    if kind == "counter_charged":
        counters, outcome = state.counters.charge(Cause(event.data["cause"]))
        if outcome is Outcome.BLOCKED:
            target_state = charge_target(Cause(event.data["cause"]))
            raise _Conflict(f"retry budget exceeded for {target_state.value}")
        return replace(state, counters=counters)
    if kind == "counter_reset":
        return replace(state, counters=state.counters.reset(AgentState(event.data["state"])))
    if kind == "branch_created":
        return replace(state, branch_base=str(event.data["sha"]), branch=str(event.data["branch"]))
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
        if "decisions" in data:
            state = replace(state, decisions=tuple(str(d) for d in data["decisions"]))
        if "doubts" in data:
            state = replace(state, doubts=tuple(str(d) for d in data["doubts"]))
        if data.get("state") == "spec" and data.get("artifact_rev") is not None:
            state = replace(state, spec_rev=str(data["artifact_rev"]))
        if data.get("commit_sha") is not None:
            state = replace(state, head_sha=str(data["commit_sha"]))
        return state
    if kind == "admin_reset":
        target = Status(event.data["to"])
        if is_terminal(state.status):
            raise _Conflict(f"conflict at seq {event.seq}: a {state.status.value} issue is final")
        counters = state.counters
        for counted in AgentState:  # a reset starts every budget over
            counters = counters.reset(counted)
        return replace(
            _fresh_state_bookkeeping(state),
            status=target,
            counters=counters,
            approved_spec_rev=None if target in _UNAPPROVED_TARGETS else state.approved_spec_rev,
            note=None,
            froze_queue=False,
            asked_from=None,
            question_comment_id=None,
        )
    if kind == "issue_typed":
        if state.status is not Status.INBOX:
            raise _Conflict(f"conflict at seq {event.seq}: an issue is typed only in the inbox")
        return replace(state, issue_type=IssueType(event.data["issue_type"]))
    if kind == "dependency_added":
        added = Dependency(int(event.data["issue"]), str(event.data["kind"]))
        kept = tuple(d for d in state.dependencies if d.issue != added.issue)
        return replace(state, dependencies=(*kept, added))
    if kind == "dependency_dropped":
        dropped = int(event.data["issue"])
        return replace(
            state, dependencies=tuple(d for d in state.dependencies if d.issue != dropped)
        )
    if kind == "run_deferred":
        return replace(
            state,
            settled_runs=frozenset(),
            settled_reviewers=frozenset(),
            deferrals=state.deferrals + 1,
        )
    if kind == "closed_after_merge":
        return replace(state, close_noted=True)
    if kind == "verify_rerun":
        return replace(
            state,
            verify_failures=state.verify_failures + 1,
            settled_runs=frozenset(),
            settled_reviewers=frozenset(),
        )
    if kind == "revert_ci":
        return replace(state, revert_ci_passed=bool(event.data["passed"]))
    if kind == "verify_failed":
        return replace(state, verify_failed=True)
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
        if action == "close_issue":
            return replace(state, closed_posted=state.closed_posted | {key})
        if action == "post_question":
            return replace(
                state,
                question_comment_id=int(result["comment_id"]),
                questions_posted=state.questions_posted | {key},
            )
        if action == "post_kit":
            return replace(state, kits_posted=state.kits_posted | {key})
        if action == "mark_ready":
            return replace(state, pr_ready=True, ready_toggles=state.ready_toggles + 1)
        if action == "mark_draft":
            return replace(state, pr_ready=False, ready_toggles=state.ready_toggles + 1)
        if action == "revert_pr":
            return replace(
                state,
                revert_pr=int(result["number"]),
                revert_branch=str(result["branch"]),
                revert_head=str(result["sha"]),
            )
        if action == "rebase":
            if result.get("conflict"):
                return replace(
                    state,
                    refresh_result="conflict",
                    refresh_note=str(result["conflict"]),
                    refresh_conflicts=state.refresh_conflicts + 1,
                )
            pre_gate = pending is not None and pending.data.get("stage") == "uat"
            return replace(
                state,
                head_sha=str(result["sha"]),
                branch_base=str(result["base"]),
                pm_commits=(*state.pm_commits, str(result["sha"])),
                refresh_result="clean",
                refreshes=state.refreshes + (1 if pre_gate else 0),
                settled_runs=frozenset(),
                settled_reviewers=frozenset(),
            )
        if action == "merge_revert" and result.get("sha"):
            return replace(
                state,
                revert_merge_sha=str(result["sha"]),
                verify_failed=False,
                verify_failures=0,
                settled_runs=frozenset(),
                settled_reviewers=frozenset(),
                active_runs=(),
            )
        if action == "merge" and result.get("sha"):
            return replace(state, merge_sha=str(result["sha"]))
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
        if data.get("verb") == "dispatch" and "created_at" in data:
            state = replace(state, dispatched_at=int(data["created_at"]))
        if data.get("verb") == "approve" and data.get("kind") == "spec" and data.get("spec_hash"):
            state = replace(state, approved_spec_rev=str(data["spec_hash"]))
        return state
    if kind == "answered":
        data = event.data
        if state.status is not Status.NEEDS_INPUT or Status(data["to"]) is not state.asked_from:
            raise _Conflict(
                f"conflict at seq {event.seq}: answer for {data['to']} "
                f"but the issue is in {state.status.value} (asked from "
                f"{state.asked_from.value if state.asked_from else 'nowhere'})"
            )
        return replace(
            _fresh_state_bookkeeping(state),
            status=Status(data["to"]),
            asked_from=None,
            question_comment_id=None,
            answers=(*state.answers, str(data["text"])),
        )
    if kind == "unblocked":
        if state.status is not Status.BLOCKED:
            raise _Conflict(
                f"conflict at seq {event.seq}: unblocked but the issue is in {state.status.value}"
            )
        return replace(
            _fresh_state_bookkeeping(state),
            status=Status(event.data["to"]),
            note=None,
            froze_queue=False,
        )
    if kind in _RECORD_ONLY:
        return state
    raise ValueError(f"unknown event kind {kind!r} at seq {event.seq}")
