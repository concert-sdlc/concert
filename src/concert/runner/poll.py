"""The poll loop for one issue.

Reads the issue's comments, trusts only what GitHub's facts support, derives the state from
the PM's own events, validates handoffs and commands against it, asks `decide` for the next
events and posts them as one coalesced comment, then reconciles the Status card with the
derived state. Everything that decides lives in `core`; this module only moves data.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from concert.core.commands import IssueContext, Role, Valid, validate_command
from concert.core.decide import AcceptedHandoff, Proposed, ValidCommand, decide
from concert.core.derive import IssueState, derive
from concert.core.events import Event, append_event, parse_event
from concert.core.handoff import Accepted, Handoff, Ignored, RunFacts, accept_handoff
from concert.core.transitions import Status
from concert.github.api import GitHubApi, Identity
from concert.github.comments import (
    assignment_heads,
    assignments_from,
    extract_commands,
    extract_events,
    extract_handoffs,
    lease_expiry_comment_ids,
)
from concert.github.mirror import Mirror, publish_events, reconcile


@dataclass(frozen=True)
class RunnerConfig:
    pm: Identity
    runner_ids: Mapping[str, int]  # runner name -> Worker App bot user id
    roles: Mapping[int, Role]  # GitHub user id -> role, from humans.*
    epoch: int
    budget: int
    base_branch: str


class FactsProvider(Protocol):
    def facts(
        self, issue: int, handoff: Handoff, state: IssueState, run_start_head: str | None
    ) -> RunFacts: ...


@dataclass(frozen=True)
class PollResult:
    status: Status
    posted: int = 0
    blocked_reason: str | None = None
    rejected_commands: list[str] = field(default_factory=list[str])
    ignored_handoffs: list[str] = field(default_factory=list[str])
    invalid_handoffs: list[str] = field(default_factory=list[str])
    status_reverted: bool = False


def poll_issue(
    github: GitHubApi, mirror: Mirror, issue: int, config: RunnerConfig, facts: FactsProvider
) -> PollResult:
    comments = github.list_comments(issue)
    reconciled = reconcile(github, mirror, issue, comments, config.pm)
    if reconciled.republished:
        comments = github.list_comments(issue)  # a crash was repaired: read the new comment
    extraction = extract_events(comments, config.pm)
    # only events the mirror confirms are history; comment ids come from the same comments
    records = [r for r in extraction.records if r.event in reconciled.events]
    raw = reconciled.events
    state = derive(raw, budget=config.budget)
    card = github.get_status(issue)
    card_was_reverted = card is not None and card != state.status.value

    if reconciled.blocked_reason is not None:
        return _finish(
            github, issue, replace_blocked(state, reconciled.blocked_reason), card_was_reverted, 0
        )
    if reconciled.problems:
        reason = "; ".join(reconciled.problems)
        return _finish(github, issue, replace_blocked(state, reason), card_was_reverted, 0)
    if state.blocked_reason is not None:
        return _finish(github, issue, state, card_was_reverted, 0)

    chain = [parse_event(event) for event in raw]
    posted = 0
    if not chain:
        created = append_event([], epoch=config.epoch, kind="created", data={"issue_type": "code"})
        publish_events(github, mirror, issue, [created])
        chain, posted = [created], 1
        raw = [created.to_dict()]
        state = derive(raw, budget=config.budget)

    assignments = assignments_from(records)
    heads = assignment_heads(records)
    lease_expiry = lease_expiry_comment_ids(records)
    handoff_extraction = extract_handoffs(comments, config.runner_ids, lease_expiry)
    invalid = [problem for problem in handoff_extraction.problems]
    ignored: list[str] = []
    accepted: list[AcceptedHandoff] = []
    for item in handoff_extraction.items:
        handoff = item.handoff
        if handoff.run_id in state.accepted_runs or handoff.state.value != state.status.value:
            continue
        run_facts = facts.facts(issue, handoff, state, heads.get(handoff.run_id))
        decision = accept_handoff(handoff, assignments, item.block, run_facts)
        if isinstance(decision, Accepted):
            accepted.append(AcceptedHandoff(handoff, decision, item.comment_id))
        elif isinstance(decision, Ignored):
            ignored.append(f"comment {item.comment_id}: {decision.reason}")
        else:
            invalid.append(f"comment {item.comment_id}: {decision.reason}")

    context = IssueContext(
        status=state.status,
        issue_type=state.issue_type,
        spec_hash=state.spec_rev,
        head_sha=state.head_sha,
        adoption_report_hash=None,
        workflow_diff_hash=None,
        unblock_targets=unblock_targets(state),
        existing_issues=frozenset({issue}),
    )
    valid: list[ValidCommand] = []
    rejected: list[str] = []
    for command in extract_commands(comments, config.roles):
        if command.comment_id in state.consumed_commands:
            continue
        verdict = validate_command(command.command, command.actor, command.facts, context)
        if isinstance(verdict, Valid):
            valid.append(
                ValidCommand(
                    command.comment_id,
                    command.command,
                    command.actor,
                    command.body,
                    command.created_at,
                )
            )
        else:
            rejected.append(f"comment {command.comment_id}: {verdict.reason}")

    proposals = decide(state, accepted, valid)
    if proposals:
        new_events = _chain_events(chain, proposals, config.epoch)
        publish_events(github, mirror, issue, new_events)
        posted += len(new_events)
        raw = [*raw, *(event.to_dict() for event in new_events)]
        state = derive(raw, budget=config.budget)

    result = _finish(github, issue, state, card_was_reverted, posted)
    return PollResult(
        status=result.status,
        posted=result.posted,
        blocked_reason=result.blocked_reason,
        rejected_commands=rejected,
        ignored_handoffs=ignored,
        invalid_handoffs=invalid,
        status_reverted=result.status_reverted,
    )


def unblock_targets(state: IssueState) -> frozenset[Status]:
    """Where `/unblock` and `/pm reset` may send an issue (design: Reset and unblock targets)."""
    targets = {Status.TRIAGE, Status.SPEC}
    if state.approved_spec_rev is not None:
        targets.add(Status.IMPLEMENTING)
        if state.head_sha is not None:
            targets.add(Status.IN_REVIEW)
    return frozenset(targets)


def replace_blocked(state: IssueState, reason: str) -> IssueState:
    from dataclasses import replace

    return replace(state, status=Status.BLOCKED, blocked_reason=reason)


def _chain_events(chain: Sequence[Event], proposals: Sequence[Proposed], epoch: int) -> list[Event]:
    events: list[Event] = []
    for proposal in proposals:
        events.append(
            append_event([*chain, *events], epoch=epoch, kind=proposal.kind, data=proposal.data)
        )
    return events


def _finish(
    github: GitHubApi, issue: int, state: IssueState, card_was_reverted: bool, posted: int
) -> PollResult:
    """Reconcile the Status card with the derived state: whatever differs is reverted."""
    if github.get_status(issue) != state.status.value:
        github.set_status(issue, state.status.value)
    return PollResult(
        status=state.status,
        posted=posted,
        blocked_reason=state.blocked_reason,
        status_reverted=card_was_reverted,
    )
