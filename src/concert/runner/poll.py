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
from concert.core.ci import plan_ci_evidence, plan_review_evidence
from concert.core.commands import IssueContext, Role, Valid, parse_command, validate_command
from concert.core.commit_binding import ForeignCommits, check_commit_binding
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
from concert.core.kits import (
    KitFacts,
    has_marker,
    kit_marker,
    question_marker,
    render_question,
    render_spec_kit,
    render_uat_kit,
)
from concert.core.paths import changed_paths, matches_any
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
    sensitive_paths: Sequence[str] = ()  # a change to these also needs a security approval
    protected_paths: Sequence[str] = ()  # an auto-revert touching these is never merged by the PM
    on_failure: str = "freeze"  # staging verification: "freeze" or "auto_revert"
    verify_reruns: int = 1  # automatic re-runs of a failed verification (flaky tests)


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
    state: IssueState = field(default_factory=IssueState)
    ready_for_integration: bool = False  # in human_uat with every merge requirement met


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
        self.posted = 0
        self.rejected: list[str] = []
        self.ignored: list[str] = []
        self.invalid: list[str] = []
        self.raw: list[dict[str, object]] = []
        self.chain: list[Event] = []
        self.records: list[EventRecord] = []
        self.handoff_items: list[HandoffComment] = []
        self.state = IssueState()

    @property
    def branch(self) -> str:
        return self.state.branch or f"pm/{self.issue}"

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
        self._sync_closed()
        if is_terminal(self.state.status):
            self._effects()  # a duplicate still has to be closed on GitHub
            return self._finish()
        self._create_branch()
        if self._check_branch():
            return self._finish()
        self._intake()
        self._create_branch()  # intake may have moved the issue into spec
        self._evidence()
        self._admin_reviews()
        self._revert_ci()
        self._effects()
        self._integrate()
        self._after_merge()
        self._after_verify()
        self._runs()
        return self._finish()

    # --- steps -----------------------------------------------------------------------------------

    def _sync_closed(self) -> None:
        """Follow the issue being closed or reopened on GitHub: close before the merge ends the
        work, a close after the merge changes nothing (noted once), a reopen starts over."""
        state = self.state
        closed = self.github.get_issue(self.issue).state == "closed"
        if not closed:
            if state.status is Status.CLOSED:
                self._post([self._move(Status.CLOSED, Status.INBOX, Trigger.REOPEN)])
            return
        if is_terminal(state.status) or state.pending_intents:
            return  # a merge that may have happened is resolved before anything else
        if state.merge_sha is None:
            self._post([self._move(state.status, Status.CLOSED, Trigger.CLOSE)])
        elif not state.close_noted:
            self._post([Proposed("closed_after_merge", {})])

    def _move(self, source: Status, target: Status, trigger: Trigger) -> Proposed:
        return Proposed(
            "transition", {"from": source.value, "to": target.value, "trigger": trigger.value}
        )

    def _create_branch(self) -> None:
        """Entering spec creates the issue branch from the base and records its first commit. A
        re-landed issue gets a new branch whose first commit re-applies the reverted work."""
        state = self.state
        if state.branch_base is not None or state.issue_type is IssueType.REVERT:
            return
        if state.status is Status.SPEC:
            name = f"pm/{self.issue}" if state.relands == 0 else f"pm/{self.issue}-r{state.relands}"
            sha = self.github.get_ref(name) or self.github.create_branch(
                name, self.config.base_branch
            )
            self._post([Proposed("branch_created", {"branch": name, "sha": sha})])
        elif state.status is Status.IMPLEMENTING and state.reland_of is not None:
            name = f"pm/{self.issue}-r{state.relands}"
            sha = self._reland_branch(name, state.reland_of)
            self._post([Proposed("branch_created", {"branch": name, "sha": sha})])

    def _reland_branch(self, name: str, reverted_merge: str) -> str:
        """A branch off the base whose first commit re-applies the changes of the reverted merge
        (a revert of the revert). Safe to repeat after a crash: each step checks its own result."""
        if self.github.get_ref(name) is None:
            self.github.create_branch(name, self.config.base_branch)
        before = self.github.read_tree(self.github.commit_parents(reverted_merge)[0])
        merged = self.github.read_tree(reverted_merge)
        changes = {
            p: merged.get(p) for p in set(merged) | set(before) if merged.get(p) != before.get(p)
        }
        tree = self.github.read_tree(name)
        if any(tree.get(p) != content for p, content in changes.items()):
            return self.github.commit(
                name,
                f"Re-apply #{self.issue} (revert of the revert)",
                changes,
                author=self.config.pm.login,
                committer=self.config.pm.login,
            )
        return self.github.get_ref(name) or ""

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
        proposals = decide(
            state, accepted, valid, self.config.verify_reruns, self._answers(all_comments)
        )
        if proposals:
            self._post(proposals)
            self._check_branch()

    def _answers(self, comments: Sequence[Comment]) -> list[Answer]:
        """Replies to the PM's question from trusted humans: unedited, not a bot, and posted
        after the question. `/answer <text>` counts; any other command does not."""
        question = self.state.question_comment_id
        if self.state.status is not Status.NEEDS_INPUT or question is None:
            return []
        found: list[Answer] = []
        for comment in comments:
            role = self.config.roles.get(comment.author.user_id, Role.NONE)
            if (
                comment.id <= question
                or comment.edited_at is not None
                or comment.author.is_bot
                or role < Role.CONTRIBUTOR
            ):
                continue
            command = parse_command(comment.body)
            if command is None:
                text = comment.body.strip()
            elif command.verb == "answer":
                text = command.arg.strip()
            else:
                continue
            if text:
                found.append(Answer(comment.id, comment.author.user_id, text))
        return found

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
                c.sha
                for c in self.github.list_commits(
                    self.state.branch_base or self.config.base_branch, handoff.commit_sha
                )
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
                self.state,
                self.issue,
                self.config.base_branch,
                gate_ready=self._gate_ready(),
                awaiting_human=self._awaiting_human(),
                auto_revert=self.config.on_failure == "auto_revert",
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
            self._revert_ci()

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
        if effect.action == "close_issue":
            return self._close_duplicate([str(d) for d in data["decisions"]])
        if effect.action == "post_question":
            return self._post_question(effect.key, [str(d) for d in data["doubts"]])
        if effect.action == "post_kit":
            return self._post_kit(str(data["kind"]), str(data["rev"]))
        if effect.action == "revert_pr":
            return self._open_revert(int(data["number"]))
        if effect.action == "merge_revert":
            return self._merge_revert(
                int(data["number"]), str(data["expected_head"]), str(data["reverts"])
            )
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

    def _close_duplicate(self, decisions: list[str]) -> dict[str, int | str | None]:
        """Explain, then close. Safe to repeat: a closed issue stays closed and the note is
        posted only if the PM has not already posted it."""
        said = " ".join(d.replace("\n", " ").replace("@", "@\u200b") for d in decisions)  # no pings
        note = "Closed as a duplicate. " + said
        text = note.strip()
        if not any(
            c.author.user_id == self.config.pm.user_id and c.body == text
            for c in self.github.list_comments(self.issue)
        ):
            self.github.add_comment(self.issue, text)
        self.github.close_issue(self.issue)
        return {"closed": 1}

    def _post_question(self, key: str, doubts: list[str]) -> dict[str, int | str | None]:
        marker = question_marker(key)
        for comment in self.github.list_comments(self.issue):
            if comment.author.user_id == self.config.pm.user_id and has_marker(
                comment.body, marker
            ):
                return {"comment_id": comment.id}
        return {"comment_id": self.github.add_comment(self.issue, render_question(key, doubts)).id}

    def _post_kit(self, kind: str, revision: str) -> dict[str, int | str | None]:
        """Post the gate kit unless this revision's kit is already there (a crash may have hidden it)."""
        marker = kit_marker(kind, revision)
        for comment in self.github.list_comments(self.issue):
            if comment.author.user_id == self.config.pm.user_id and has_marker(
                comment.body, marker
            ):
                return {"comment_id": comment.id}
        state = self.state
        ref = state.head_sha or self.branch
        facts = self._kit_facts()
        if kind == "spec":
            summary = self.github.read_file(ref, f"docs/specs/{self.issue}/spec.md") or ""
            body = render_spec_kit(facts, revision, summary)
        else:
            summary = self.github.read_file(ref, f"docs/specs/{self.issue}/uat.md") or ""
            body = render_uat_kit(facts, revision, state.evidence, summary)
        return {"comment_id": self.github.add_comment(self.issue, body).id}

    def _kit_facts(self) -> KitFacts:
        policy = self._gate_policy()
        touched = self._changed_paths()
        if not touched and self.state.branch_base is not None:
            head = self.state.head_sha or self.github.get_ref(self.branch)
            if head is not None:
                touched = changed_paths(
                    self.github.read_tree(self.state.branch_base), self.github.read_tree(head)
                )
        return KitFacts(
            issue=self.issue,
            changed_files=touched,
            security_sensitive=policy.security_sensitive if policy else False,
            protected_files=[p for p in touched if matches_any(p, self.config.protected_paths)],
        )

    def _open_revert(self, number: int) -> dict[str, int | str | None]:
        branch = f"revert-pr-{number}"
        try:
            pr = self.github.find_pr(branch) or self.github.revert_pr(number)
        except Conflict as error:
            return {"error": str(error)}
        return {"number": pr.number, "branch": pr.head, "sha": self.github.get_ref(pr.head)}

    def _merge_revert(
        self, number: int, expected_head: str, reverts: str
    ) -> dict[str, int | str | None]:
        """The PM merges its own auto-revert without a human, so it merges only a PR whose tree is
        exactly the base with the failed merge's changes undone, and which leaves protected paths
        alone. Anything else (including a look-alike PR someone else opened) is refused."""
        base = dict(self.github.read_tree(self.config.base_branch))
        before = self.github.read_tree(self.github.commit_parents(reverts)[0])
        merged = self.github.read_tree(reverts)
        expected = dict(base)
        for path in set(merged) | set(before):
            if merged.get(path) == before.get(path):
                continue
            if before.get(path) is None:
                expected.pop(path, None)
            else:
                expected[path] = before[path]
        reverted = dict(self.github.read_tree(expected_head))
        if reverted != expected:
            return {"error": "the revert PR is not the inverse of the failed merge"}
        changed = changed_paths(base, reverted)
        touched = [p for p in changed if matches_any(p, self.config.protected_paths)]
        if touched:
            return {"error": f"the revert touches protected paths: {', '.join(touched)}"}
        return self._merge(number, expected_head)

    def _revert_ci(self) -> None:
        state = self.state
        policy = self.config.gate_policy
        if policy is None or state.revert_head is None:
            return
        runs = self.github.list_check_runs(state.revert_head)
        planned = plan_revert_ci(state, runs, policy.dod_items)
        if planned:
            self._post(planned)

    def _gate_policy(self) -> GatePolicy | None:
        policy = self.config.gate_policy
        if policy is None:
            return None
        is_revert = self.state.issue_type is IssueType.REVERT
        touched = self._changed_paths()
        return replace(
            policy,
            issue_type=self.state.issue_type,
            require_reviews=not is_revert,
            security_sensitive=any(matches_any(p, self.config.sensitive_paths) for p in touched),
            protected_changed=any(matches_any(p, self.config.protected_paths) for p in touched),
        )

    def _changed_paths(self) -> list[str]:
        """What the issue branch changed since it was created (empty before it has a head)."""
        state = self.state
        if state.head_sha is None or state.branch_base is None:
            return []
        return changed_paths(
            self.github.read_tree(state.branch_base), self.github.read_tree(state.head_sha)
        )

    def _admin_reviews(self) -> None:
        policy, state = self.config.gate_policy, self.state
        if policy is None or state.pr_number is None or state.status is Status.BLOCKED:
            return
        reviews = self.github.list_pr_reviews(state.pr_number)
        planned = plan_review_evidence(state.head_sha, reviews, state.evidence, policy.admins)
        if planned:
            self._post(planned)

    def _awaiting_human(self) -> bool:
        """Every automated requirement holds; only human approvals are still missing."""
        policy = self._gate_policy()
        if policy is None or self.state.head_sha is None:
            return False
        verdict = evaluate_gate(self.state.head_sha, self.state.evidence, policy)
        if isinstance(verdict, Ready):
            return True
        human = ("approval:", "admin_review")
        return not verdict.failing and all(item.startswith(human) for item in verdict.missing)

    def _gate_ready(self) -> bool:
        policy = self._gate_policy()
        if policy is None or self.state.head_sha is None:
            return False
        return isinstance(evaluate_gate(self.state.head_sha, self.state.evidence, policy), Ready)

    def _after_verify(self) -> None:
        planned = plan_verify_failure(self.state, self.config.on_failure)
        if planned:
            self._post(planned)

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
            self.state.revert_merge_sha or self.state.merge_sha
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
            state=state,
            ready_for_integration=state.status is Status.HUMAN_UAT
            and state.blocked_reason is None
            and self._gate_ready(),
        )


def unblock_targets(state: IssueState) -> frozenset[Status]:
    """Where `/unblock` and `/pm reset` may send an issue (design: Reset and unblock targets)."""
    targets = {Status.TRIAGE, Status.SPEC}
    if state.approved_spec_rev is not None:
        targets.add(Status.IMPLEMENTING)
        if state.head_sha is not None:
            targets.add(Status.IN_REVIEW)
    return frozenset(targets)
