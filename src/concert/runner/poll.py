"""The poll loop for one issue.

Reads the issue's comments, trusts only what the mirror and GitHub's facts support, derives the
state from the PM's own events, validates handoffs and commands against it, asks `decide` for
the next events, assigns runs, expires leases, and reconciles the Status card with the derived
state. Everything that decides lives in `core`; this module moves data and performs the
idempotent side effects (creating the issue branch, fast-forwarding it to an accepted commit).
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from concert.core.assign import Heartbeat, RunnerInfo, expire_leases, plan_assignments
from concert.core.ci import plan_ci_evidence
from concert.core.commands import IssueContext, Role, Valid, validate_command
from concert.core.commit_binding import ForeignCommits, check_commit_binding
from concert.core.decide import (
    AcceptedHandoff,
    Proposed,
    ValidCommand,
    decide,
    plan_integration,
    plan_merged,
)
from concert.core.derive import IssueState, derive
from concert.core.effects import Effect, plan_effect
from concert.core.events import Event, append_event, parse_event
from concert.core.evidence import GatePolicy, Ready, evaluate_gate
from concert.core.handoff import (
    Accepted,
    Handoff,
    Ignored,
    RunFacts,
    RunState,
    accept_handoff,
)
from concert.core.transitions import IssueType, Status, Trigger, is_terminal
from concert.github.api import Comment, Conflict, GitHubApi, Identity
from concert.github.comments import (
    EventRecord,
    HandoffComment,
    assignment_heads,
    assignments_from,
    extract_commands,
    extract_events,
    extract_handoffs,
    lease_expiry_comment_ids,
)
from concert.github.heartbeats import read_heartbeat
from concert.github.mirror import Mirror, publish_events, reconcile

_COMMITTING = frozenset({RunState.SPEC, RunState.IMPLEMENTING, RunState.INTEGRATING})


@dataclass(frozen=True)
class RunnerConfig:
    pm: Identity
    runner_ids: Mapping[str, int]  # runner name -> Worker App bot user id
    roles: Mapping[int, Role]  # GitHub user id -> role, from humans.*
    epoch: int
    budget: int
    base_branch: str
    runners: Sequence[RunnerInfo] = ()  # runners the PM may assign to; empty: no assignments
    gate_policy: GatePolicy | None = None  # None: CI is not read and nothing integrates


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
    github: GitHubApi,
    mirror: Mirror,
    issue: int,
    config: RunnerConfig,
    facts: FactsProvider,
    now: int = 0,
    load: Mapping[str, int] | None = None,
    at_queue_head: bool = True,
) -> PollResult:
    return _Poll(github, mirror, issue, config, facts, now, load, at_queue_head).run()


class _Poll:
    def __init__(
        self,
        github: GitHubApi,
        mirror: Mirror,
        issue: int,
        config: RunnerConfig,
        facts: FactsProvider,
        now: int,
        load: Mapping[str, int] | None,
        at_queue_head: bool,
    ) -> None:
        self.at_queue_head = at_queue_head
        self.github = github
        self.mirror = mirror
        self.issue = issue
        self.config = config
        self.facts = facts
        self.now = now
        self.load = load
        self.branch = f"pm/{issue}"
        self.posted = 0
        self.rejected: list[str] = []
        self.ignored: list[str] = []
        self.invalid: list[str] = []
        self.raw: list[dict[str, object]] = []
        self.chain: list[Event] = []
        self.records: list[EventRecord] = []
        self.handoff_items: list[HandoffComment] = []
        self.state = IssueState()

    # --- the poll ------------------------------------------------------------------------------

    def run(self) -> PollResult:
        comments = self.github.list_comments(self.issue)
        reconciled = reconcile(self.github, self.mirror, self.issue, comments, self.config.pm)
        if reconciled.republished:
            comments = self.github.list_comments(self.issue)
        extraction = extract_events(comments, self.config.pm)
        self.records = [r for r in extraction.records if r.event in reconciled.events]
        self.raw = list(reconciled.events)
        self.state = derive(self.raw, budget=self.config.budget)
        card = self.github.get_status(self.issue)
        self.card_was_reverted = card is not None and card != self.state.status.value

        if reconciled.blocked_reason is not None:
            return self._finish(blocked=reconciled.blocked_reason)
        if reconciled.problems:
            return self._finish(blocked="; ".join(reconciled.problems))
        if self.state.blocked_reason is not None:
            return self._finish()

        self.chain = [parse_event(event) for event in self.raw]
        if not self.chain:
            self._post([Proposed("created", {"issue_type": "code"})])
        self._create_branch()
        if self._check_branch():
            return self._finish()
        self._intake()
        self._create_branch()  # intake may have moved the issue into spec
        self._evidence()
        self._effects()
        self._integrate()
        self._after_merge()
        self._runs()
        return self._finish()

    # --- steps -----------------------------------------------------------------------------------

    def _create_branch(self) -> None:
        """Entering spec creates the issue branch from the base and records its first commit."""
        if (
            self.state.status is Status.SPEC
            and self.state.branch_base is None
            and self.state.issue_type is not IssueType.REVERT
        ):
            sha = self.github.get_ref(self.branch) or self.github.create_branch(
                self.branch, self.config.base_branch
            )
            self._post([Proposed("branch_created", {"branch": self.branch, "sha": sha})])

    def _check_branch(self) -> bool:
        """On every poll the issue branch must be at the last accepted commit: workers push only
        their own WIP refs. A branch that merely lags it (the PM died before advancing it) is
        repaired; anything else is a foreign push."""
        state = self.state
        if (
            state.branch_base is None
            or state.status in (Status.INBOX, Status.BLOCKED)
            or is_terminal(state.status)
        ):
            return False
        head = self.github.get_ref(self.branch)
        if head is None:
            return False
        expected = state.head_sha or state.branch_base
        if head == expected:
            return False
        if self._behind(head, expected):
            self.github.update_ref(self.branch, expected)
            return False
        return self._block(f"unexpected push: {head} (expected {expected})")

    def _behind(self, head: str, expected: str) -> bool:
        ahead = self.github.list_commits(head, expected)
        return bool(ahead) and not self.github.list_commits(expected, head)

    def _intake(self) -> None:
        state = self.state
        all_comments = self.github.list_comments(self.issue)
        assignments = assignments_from(self.records)
        heads = assignment_heads(self.records)
        lease_expiry = lease_expiry_comment_ids(self.records)
        extraction = extract_handoffs(all_comments, self.config.runner_ids, lease_expiry)
        self.handoff_items = extraction.items
        self.invalid.extend(extraction.problems)

        accepted: list[AcceptedHandoff] = []
        for item in extraction.items:
            handoff = item.handoff
            if handoff.run_id in state.accepted_runs or handoff.state.value != state.status.value:
                continue
            run_facts = self.facts.facts(self.issue, handoff, state, heads.get(handoff.run_id))
            decision = accept_handoff(handoff, assignments, item.block, run_facts)
            if isinstance(decision, Accepted):
                accepted.append(AcceptedHandoff(handoff, decision, item.comment_id))
            elif isinstance(decision, Ignored):
                self.ignored.append(f"comment {item.comment_id}: {decision.reason}")
            else:
                self.invalid.append(f"comment {item.comment_id}: {decision.reason}")

        if self._foreign_commits(accepted):
            return

        valid = self._valid_commands(all_comments)
        proposals = decide(state, accepted, valid)
        if proposals:
            self._post(proposals)
            self._check_branch()

    def _foreign_commits(self, accepted: Sequence[AcceptedHandoff]) -> bool:
        """Every commit a committing run brings must be one the worker reported."""
        prior = [
            i.handoff for i in self.handoff_items if i.handoff.run_id in self.state.accepted_runs
        ]
        for item in accepted:
            handoff = item.handoff
            if handoff.state not in _COMMITTING or handoff.commit_sha is None:
                continue
            commits = [
                c.sha for c in self.github.list_commits(self.config.base_branch, handoff.commit_sha)
            ]
            verdict = check_commit_binding(commits, [*prior, handoff])
            if isinstance(verdict, ForeignCommits):
                return self._block(f"foreign commit: {', '.join(verdict.shas)}")
        return False

    def _valid_commands(self, comments: Sequence[Comment]) -> list[ValidCommand]:
        state = self.state
        context = IssueContext(
            status=state.status,
            issue_type=state.issue_type,
            spec_hash=state.spec_rev,
            head_sha=state.head_sha,
            adoption_report_hash=None,
            workflow_diff_hash=None,
            unblock_targets=unblock_targets(state),
            existing_issues=frozenset({self.issue}),
        )
        valid: list[ValidCommand] = []
        for command in extract_commands(comments, self.config.roles):
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
                self.rejected.append(f"comment {command.comment_id}: {verdict.reason}")
        return valid

    def _effects(self) -> None:
        """Perform the side effects the state calls for: announce, act, record the outcome.
        Every action is idempotent, so a pending intent left by a crash is simply run again."""
        for _ in range(4):  # a bound, never a loop on a failing effect
            effect = plan_effect(
                self.state, self.issue, self.config.base_branch, gate_ready=self._gate_ready()
            )
            if effect is None:
                return
            if not effect.resumed:
                self._post(
                    [
                        Proposed(
                            "intent", {"key": effect.key, "action": effect.action, **effect.data}
                        )
                    ]
                )
            try:
                result = self._perform(effect)
            except ValueError as error:
                self._block(str(error))
                return
            self._post([Proposed("outcome", {"key": effect.key, "result": result})])
            if "error" in result:
                self._block(f"{effect.action} failed: {result['error']}")
                return

    def _perform(self, effect: Effect) -> dict[str, int | str | None]:
        data = effect.data
        if effect.action == "open_pr":
            existing = self.github.find_pr(str(data["head"]))
            pr = existing or self.github.create_pr(
                str(data["head"]), str(data["base"]), str(data["title"])
            )
            return {"number": pr.number}
        if effect.action == "mark_ready":
            number = int(data["number"])
            self.github.mark_ready(number)
            return {"number": number}
        if effect.action == "merge":
            return self._merge(int(data["number"]), str(data["expected_head"]))
        raise ValueError(f"unknown side effect {effect.action!r}")

    def _merge(self, number: int, expected_head: str) -> dict[str, int | str | None]:
        """Merge only if the PR is still open at the expected head; a merge a crash hid is found."""
        pr = self.github.get_pr(number)
        if pr.state == "merged":
            if pr.merge_sha is None:
                return {"error": f"pull request #{number} is merged with no merge commit"}
            return {"sha": pr.merge_sha}
        try:
            return {"sha": self.github.merge_pr(number, expected_head)}
        except Conflict as error:  # includes StaleHead
            return {"error": str(error)}

    def _gate_policy(self) -> GatePolicy | None:
        policy = self.config.gate_policy
        if policy is None:
            return None
        is_revert = self.state.issue_type is IssueType.REVERT
        return replace(policy, issue_type=self.state.issue_type, require_reviews=not is_revert)

    def _gate_ready(self) -> bool:
        policy = self._gate_policy()
        if policy is None or self.state.head_sha is None:
            return False
        return isinstance(evaluate_gate(self.state.head_sha, self.state.evidence, policy), Ready)

    def _after_merge(self) -> None:
        planned = plan_merged(self.state)
        if planned:
            self._post(planned)

    def _evidence(self) -> None:
        """Record finished CI results for the current head (needs a gate policy)."""
        policy, state = self.config.gate_policy, self.state
        if policy is None or state.head_sha is None or is_terminal(state.status):
            return
        runs = self.github.list_check_runs(state.head_sha)
        planned = plan_ci_evidence(
            state.head_sha, runs, state.evidence, policy.dod_items, policy.policy_rev
        )
        if planned:
            self._post(planned)

    def _integrate(self) -> None:
        policy = self._gate_policy()
        if policy is None:
            return
        planned = plan_integration(self.state, policy, self.at_queue_head)
        if planned:
            self._post(planned)

    def _runs(self) -> None:
        """Expire lapsed leases, then assign runs to open slots."""
        if not self.config.runners:
            return
        heartbeats: dict[str, Heartbeat] = {}
        for runner in self.config.runners:
            beat = read_heartbeat(self.github, runner.name)
            if beat is not None:
                heartbeats[runner.name] = beat
        expired = expire_leases(self.state, heartbeats, self.now)
        if expired:
            self._post(expired)
        if self.state.blocked_reason is not None or self.state.status is Status.BLOCKED:
            return
        load = (
            self.load
            if self.load is not None
            else dict(Counter(run.runner for run in self.state.active_runs))
        )
        # staging verification runs on the merge commit; every other run on the issue branch
        head = (
            self.state.merge_sha
            if self.state.status is Status.VERIFYING_STAGING
            else self.github.get_ref(self.branch)
        )
        assignments = plan_assignments(
            self.state, self.issue, self.config.runners, heartbeats, load, head, self.now
        )
        if assignments:
            self._post(assignments)

    # --- helpers ---------------------------------------------------------------------------------

    def _block(self, note: str) -> bool:
        proposal = Proposed(
            "transition",
            {
                "from": self.state.status.value,
                "to": Status.BLOCKED.value,
                "trigger": Trigger.CHECK_FAILED.value,
                "note": note,
            },
        )
        self._post([proposal])
        return True

    def _post(self, proposals: Sequence[Proposed]) -> None:
        new_events: list[Event] = []
        for proposal in proposals:
            new_events.append(
                append_event(
                    [*self.chain, *new_events],
                    epoch=self.config.epoch,
                    kind=proposal.kind,
                    data=proposal.data,
                )
            )
        publish_events(self.github, self.mirror, self.issue, new_events)
        self.chain.extend(new_events)
        self.raw.extend(event.to_dict() for event in new_events)
        self.posted += len(new_events)
        self.state = derive(self.raw, budget=self.config.budget)

    def _finish(self, blocked: str | None = None) -> PollResult:
        state = (
            replace(self.state, status=Status.BLOCKED, blocked_reason=blocked)
            if blocked
            else self.state
        )
        if self.github.get_status(self.issue) != state.status.value:
            self.github.set_status(self.issue, state.status.value)
        return PollResult(
            status=state.status,
            posted=self.posted,
            blocked_reason=state.blocked_reason,
            rejected_commands=self.rejected,
            ignored_handoffs=self.ignored,
            invalid_handoffs=self.invalid,
            status_reverted=self.card_was_reverted,
        )


def unblock_targets(state: IssueState) -> frozenset[Status]:
    """Where `/unblock` and `/pm reset` may send an issue (design: Reset and unblock targets)."""
    targets = {Status.TRIAGE, Status.SPEC}
    if state.approved_spec_rev is not None:
        targets.add(Status.IMPLEMENTING)
        if state.head_sha is not None:
            targets.add(Status.IN_REVIEW)
    return frozenset(targets)
