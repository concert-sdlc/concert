"""The worker side: execute the runs the PM assigned to this runner and report them.

A worker never claims work: it acts only on assignment events authored by the PM App (and never
edited), for its own runner name, in the state the issue is still in. It runs an agent on the
run's starting tree, pushes any changes to its own WIP ref (never the issue branch: the PM
advances that after accepting the handoff), and posts a `pm-handoff` block. The agent itself is
behind a small protocol, so the real Claude Code adapter and the test doubles share one loop.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from concert.core.derive import ActiveRun, derive
from concert.core.handoff import RunState
from concert.core.spec_hash import spec_tree_hash
from concert.github.api import GitHubApi, Identity
from concert.github.comments import extract_events, render_handoff_comment
from concert.github.heartbeats import write_heartbeat

_COMMITTING = frozenset({RunState.SPEC, RunState.IMPLEMENTING, RunState.INTEGRATING})


@dataclass(frozen=True)
class AgentTask:
    issue: int
    run_id: str
    state: str
    reviewer: str | None
    head: str | None  # the commit the run starts from (None before the issue has a branch)
    tree: Mapping[str, str]  # the repository files at `head`
    title: str  # untrusted: whoever wrote the issue
    body: str  # untrusted
    spec_rev: str | None
    approved_spec_rev: str | None


@dataclass(frozen=True)
class AgentResult:
    exit: str
    changes: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    produced: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    doubts: tuple[str, ...] = ()


class AgentFailure(Exception):
    """The agent could not produce a usable result. The worker posts nothing: the lease lapses
    and the PM retries the run on its infrastructure-retry budget."""


class Agent(Protocol):
    def run(self, task: AgentTask) -> AgentResult: ...


class Worker:
    def __init__(
        self, github: GitHubApi, runner: str, me: Identity, pm: Identity, agent: Agent
    ) -> None:
        self.github = github
        self.runner = runner
        self.me = me  # this runner's Worker App identity
        self.pm = pm
        self.agent = agent

    def run_once(self, issue: int, now: int) -> list[str]:
        """Execute every run assigned to this runner for the issue that has no handoff yet.
        Returns the run ids it reported."""
        comments = self.github.list_comments(issue)
        extraction = extract_events(comments, self.pm)
        state = derive([record.event for record in extraction.records])
        if state.blocked_reason is not None:
            return []
        reported = {
            run
            for c in comments
            if c.author.user_id == self.me.user_id
            for run in _run_markers(c.body)
        }
        mine = [
            run
            for run in state.active_runs
            if run.runner == self.runner
            and run.run_id not in reported
            and run.state == state.status.value
        ]
        done: list[str] = []
        for run in mine:
            write_heartbeat(self.github, self.runner, [run.run_id], now)
            try:
                self._execute(issue, run, state.spec_rev, state.approved_spec_rev)
            except AgentFailure:
                continue
            done.append(run.run_id)
        return done

    def _execute(
        self, issue: int, run: ActiveRun, spec_rev: str | None, approved: str | None
    ) -> None:
        info = self.github.get_issue(issue)
        tree = dict(self.github.read_tree(run.head)) if run.head else {}
        result = self.agent.run(
            AgentTask(
                issue=issue,
                run_id=run.run_id,
                state=run.state,
                reviewer=run.reviewer,
                head=run.head,
                tree=tree,
                title=info.title,
                body=info.body,
                spec_rev=spec_rev,
                approved_spec_rev=approved,
            )
        )
        state = RunState(run.state)
        commit = run.head
        commits: tuple[str, ...] = ()
        if state in _COMMITTING and result.changes and run.head is not None:
            commit = self._commit(issue, run, result)
            commits = (commit,)
        artifact: str | None = None
        if state is RunState.SPEC and commit is not None:
            artifact = spec_tree_hash(self.github.read_tree(commit), issue)
        elif state is not RunState.TRIAGE:
            artifact = approved
        raw: dict[str, object] = {
            "schema": 1,
            "state": run.state,
            "run_id": run.run_id,
            "runner": self.runner,
            "exit": result.exit,
            "commit_sha": commit if state is not RunState.TRIAGE else None,
            "artifact_rev": artifact,
            "produced": list(result.produced),
            "decisions": list(result.decisions),
            "doubts": list(result.doubts),
            "run_commits": list(commits),
            "reviewer": run.reviewer,
        }
        self.github.add_comment(issue, render_handoff_comment(raw))

    def _commit(self, issue: int, run: ActiveRun, result: AgentResult) -> str:
        ref = f"pm-wip/{self.runner}/{issue}/{run.run_id}"
        existing = self.github.get_ref(ref)
        if existing is not None:  # a retry after a crash: keep what the first attempt pushed
            return existing
        assert run.head is not None
        self.github.create_branch(ref, run.head)
        return self.github.commit(
            ref,
            f"{run.state}: #{issue} ({run.run_id})",
            result.changes,
            author=self.runner,
            committer=self.runner,
        )


def _run_markers(body: str) -> list[str]:
    import re

    return re.findall(r"<!-- pm-run:([^ ]+) -->", body)
