"""decide(): the next events to post for one issue.

Pure. Input is the state derived from trusted events, the handoffs `accept_handoff` accepted
and the commands `validate_command` accepted. Output is the events the PM should append
(it assigns seq, prev and epoch). One step per call: one accepted handoff (or one review
round) or one command, so every poll is small and re-derivable.

Gates that need more evidence than a command (`approve uat`, `approve security`,
protected-path approvals, merge preconditions) are not decided here yet.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from concert.core.commands import Actor, Command
from concert.core.counters import AgentState, Cause, Outcome
from concert.core.derive import IssueState
from concert.core.evidence import GatePolicy, Ready, evaluate_gate
from concert.core.handoff import Accepted, Handoff, consolidate_reviews
from concert.core.transitions import IssueType, Status, Trigger, is_terminal, next_status

if TYPE_CHECKING:  # ci imports Proposed from here
    from concert.core.ci import CheckRun

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
class Answer:
    """A trusted human's reply to the PM's question (the caller checked who wrote it)."""

    comment_id: int
    author_id: int
    text: str


_ANSWER_LIMIT = 8_000


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
    (Status.HUMAN_UAT, IssueType.REVERT, "reject", "revert"): Trigger.REJECT_REVERT,
}

# (status, verb, kind) -> approval evidence kept in the ledger; the move to integrating is
# planned separately once the whole gate holds and the issue is at the head of the queue.
_APPROVALS = frozenset(
    {
        (Status.HUMAN_UAT, "approve", "uat"),
        (Status.HUMAN_UAT, "approve", "security"),
        (Status.HUMAN_UAT, "approve", "revert"),
    }
)

_COUNTED_STATES = {state.value: state for state in AgentState}


def decide(
    state: IssueState,
    handoffs: Sequence[AcceptedHandoff],
    commands: Sequence[ValidCommand],
    verify_reruns: int = 1,
    answers: Sequence[Answer] = (),
) -> list[Proposed]:
    if state.blocked_reason is not None or is_terminal(state.status):
        return []
    from_handoffs = _from_handoffs(state, handoffs, verify_reruns)
    if from_handoffs:
        return from_handoffs
    if state.status is Status.NEEDS_INPUT:
        return _from_answers(state, answers)
    return _from_commands(state, commands)


def _from_answers(state: IssueState, answers: Sequence[Answer]) -> list[Proposed]:
    if state.asked_from is None or not answers:
        return []
    first = min(answers, key=lambda a: a.comment_id)
    data = {
        "to": state.asked_from.value,
        "comment_id": first.comment_id,
        "author_id": first.author_id,
        "text": first.text[:_ANSWER_LIMIT],
    }
    return [Proposed("answered", data)]


# --- handoffs --------------------------------------------------------------------------------


def _from_handoffs(
    state: IssueState, handoffs: Sequence[AcceptedHandoff], verify_reruns: int
) -> list[Proposed]:
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
    if (
        state.status is Status.VERIFYING_STAGING
        and state.revert_merge_sha is not None
        and first.decision.trigger is Trigger.PASSED
    ):  # the verified commit is the revert: the issue goes back to implementing
        return [_accepted(first), *_trigger_events(state, Trigger.FAILED_REVERTED)]
    if state.status is Status.VERIFYING_STAGING and first.handoff.exit == "failed":
        final = state.verify_failures >= verify_reruns
        again = Proposed(
            "verify_failed" if final else "verify_rerun", {"run_id": first.handoff.run_id}
        )
        return [_accepted(first), again]
    if state.status is Status.INTEGRATING and first.decision.trigger is Trigger.READY_TO_MERGE:
        return [_accepted(first)]  # the issue moves on once the PM has merged
    return [
        _accepted(first),
        *_run_evidence(first),
        *_trigger_events(state, first.decision.trigger),
    ]


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
    return [
        *(_accepted(item) for item in used),
        *(e for item in used for e in _run_evidence(item)),
        *_trigger_events(state, trigger),
    ]


def _run_evidence(item: AcceptedHandoff) -> list[Proposed]:
    """A reviewer's verdict or an automated UAT result, bound to the head the run saw."""
    handoff = item.handoff
    if handoff.commit_sha is None:
        return []
    if handoff.state.value == "uat":
        return [
            Proposed(
                "evidence",
                {
                    "kind": "uat",
                    "item": None,
                    "head_sha": handoff.commit_sha,
                    "passed": handoff.exit == "passed",
                },
            )
        ]
    if handoff.state.value == "in_review" and handoff.reviewer is not None:
        return [
            Proposed(
                "evidence",
                {
                    "kind": "review",
                    "item": handoff.reviewer,
                    "head_sha": handoff.commit_sha,
                    "passed": handoff.exit == "clean",
                },
            )
        ]
    return []


def _accepted(item: AcceptedHandoff) -> Proposed:
    handoff = item.handoff
    return Proposed(
        "handoff_accepted",
        {
            "run_id": handoff.run_id,
            "state": handoff.state.value,
            "commit_sha": handoff.commit_sha,
            "artifact_rev": handoff.artifact_rev,
            "doubts": list(handoff.doubts),
            "decisions": list(handoff.decisions),
        },
    )


# --- commands --------------------------------------------------------------------------------


def _from_commands(state: IssueState, commands: Sequence[ValidCommand]) -> list[Proposed]:
    for item in sorted(commands, key=lambda c: c.comment_id):
        if item.comment_id in state.consumed_commands:
            continue
        passage = Proposed("gate_passage", _snapshot(item, state))
        if state.status is Status.BLOCKED and item.command.verb == "unblock":
            return [passage, *_unblock_events(item.command.arg.strip())]
        if (state.status, item.command.verb, item.command.kind) in _APPROVALS:
            if state.head_sha is None:
                continue
            return [passage, _approval_evidence(item, state.head_sha)]
        key = (state.status, state.issue_type, item.command.verb, item.command.kind)
        trigger = _GATES.get(key)
        if trigger is not None:
            return [passage, *_trigger_events(state, trigger)]
    return []


def _approval_evidence(item: ValidCommand, head_sha: str) -> Proposed:
    return Proposed(
        "evidence",
        {
            "kind": "approval",
            "item": item.command.kind,
            "head_sha": head_sha,
            "passed": True,
            "actor_id": item.actor.user_id,
        },
    )


def _snapshot(item: ValidCommand, state: IssueState) -> dict[str, Any]:
    """Gate-passage events snapshot the command they consumed. Approving the spec also
    records the full spec hash that was approved (the command may carry only a prefix)."""
    approved = {"spec_hash": state.spec_rev} if _is_spec_approval(item) else {}
    return approved | {
        "comment_id": item.comment_id,
        "author_id": item.actor.user_id,
        "author_login": item.actor.login,
        "verb": item.command.verb,
        "kind": item.command.kind,
        "arg": item.command.arg,
        "body": item.body,
        "created_at": item.created_at,
    }


def _is_spec_approval(item: ValidCommand) -> bool:
    return item.command.verb == "approve" and item.command.kind == "spec"


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


# --- integration -----------------------------------------------------------------------------


def plan_integration(state: IssueState, policy: GatePolicy, at_queue_head: bool) -> list[Proposed]:
    """Move an issue from human_uat to integrating once every gate requirement holds on the
    current head and the issue is first in the integration queue."""
    if state.status is not Status.HUMAN_UAT or state.blocked_reason is not None:
        return []
    if state.head_sha is None or not at_queue_head:
        return []
    if not isinstance(evaluate_gate(state.head_sha, state.evidence, policy), Ready):
        return []
    trigger = (
        Trigger.APPROVE_REVERT if state.issue_type is IssueType.REVERT else Trigger.APPROVE_UAT
    )
    return _trigger_events(state, trigger)


def plan_merged(state: IssueState) -> list[Proposed]:
    """After the PM merged the PR, the issue moves on to staging verification."""
    if state.status is not Status.INTEGRATING or state.merge_sha is None:
        return []
    return _trigger_events(state, Trigger.READY_TO_MERGE)


def plan_revert_ci(
    state: IssueState, runs: "Sequence[CheckRun]", dod_items: Collection[str]
) -> list[Proposed]:
    """Decide whether the auto-revert PR's checks are green: every definition-of-done check
    finished on the revert head, all successful (clear to merge) or any failed (freeze)."""
    if (
        state.status is not Status.VERIFYING_STAGING
        or state.revert_head is None
        or state.revert_ci_passed is not None
    ):
        return []
    latest = {r.name: r for r in runs if r.head_sha == state.revert_head and r.conclusion}
    results = [latest[name].conclusion == "success" for name in dod_items if name in latest]
    if False in results:
        passed = False
    elif len(results) == len(dod_items):
        passed = True
    else:
        return []
    return [Proposed("revert_ci", {"head": state.revert_head, "passed": passed})]


def plan_verify_failure(state: IssueState, on_failure: str) -> list[Proposed]:
    """Freeze (block) the issue when the failure cannot be auto-reverted: the policy is `freeze`,
    the issue is a hotfix or revert (reverting a revert would re-apply the breaking merge), the
    revert's checks failed, or the revert commit itself failed verification. Otherwise a code
    issue under `auto_revert` waits for its revert."""
    if state.status is not Status.VERIFYING_STAGING:
        return []
    revert_failed = state.revert_ci_passed is False
    unrevertable = (
        on_failure != "auto_revert"
        or state.issue_type is not IssueType.CODE
        or state.revert_merge_sha is not None
    )
    if not (revert_failed or (state.verify_failed and unrevertable)):
        return []
    return _trigger_events(state, Trigger.VERIFY_FAILED)
