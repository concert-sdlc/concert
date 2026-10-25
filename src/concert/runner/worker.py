"""The worker side: execute the runs the PM assigned to this runner and report them.

A worker never claims work: it acts only on assignment events authored by the PM App (and never
edited), for its own runner name, in the state the issue is still in. It runs an agent on the
run's starting tree, pushes any changes to its own WIP ref (never the issue branch: the PM
advances that after accepting the handoff), and posts a `pm-handoff` block. The agent itself is
behind a small protocol, so the real Claude Code adapter and the test doubles share one loop.
"""

import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

from concert.core.derive import ActiveRun, derive
from concert.core.handoff import RunState
from concert.core.lessons import Lesson, load_lessons, select_lessons
from concert.core.paths import WORKFLOW_PATHS, matches_any
from concert.core.spec_hash import spec_tree_hash
from concert.github.api import BinaryBlob, GitHubApi, GitHubError, Identity
from concert.github.comments import extract_events, render_handoff_comment
from concert.github.heartbeats import write_heartbeat

_COMMITTING = frozenset({RunState.SPEC, RunState.IMPLEMENTING, RunState.INTEGRATING})
MAX_COMMENT = 60_000  # GitHub refuses a comment over 65,536 characters
PAUSE_DEFAULT = 900  # seconds to wait when the limit names no reset time
MAX_PAUSES = 24  # pauses per run; past it the run is left to lapse and be charged like a failure
PAUSE_BUDGET = 6 * 3600  # a reset further away than this is not waited for: the PM reassigns
_ITEM = 1_000  # one decision, doubt or path in a handoff
_LIST = 8_000  # all the items of one list


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
    answers: tuple[str, ...] = ()  # trusted humans' replies to earlier questions, oldest first
    lessons: tuple[Lesson, ...] = ()  # accepted lessons that apply to this run, best first


@dataclass(frozen=True)
class AgentResult:
    exit: str
    changes: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    produced: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    doubts: tuple[str, ...] = ()
    lessons_applied: tuple[str, ...] = ()  # ids of the lessons the run followed
    lessons_proposed: tuple[str, ...] = ()  # what the run learned that the next one should know
    plan_deviations: tuple[str, ...] = ()  # reviewers: where the change departs from the spec


class QuotaPause(Exception):
    """The agent hit a usage or rate limit. This is a pause, not a failure: the run stays in the
    heartbeat (so its lease holds and no retry is charged) and is tried again after `until`."""

    def __init__(self, reason: str, until: int | None = None) -> None:
        super().__init__(reason)
        self.until = until  # epoch seconds, when the limit says it resets


class AgentFailure(Exception):
    """The agent could not produce a usable result. The worker posts nothing: the lease lapses
    and the PM retries the run on its infrastructure-retry budget."""


class Agent(Protocol):
    def run(self, task: AgentTask) -> AgentResult: ...


class Worker:
    def __init__(
        self,
        github: GitHubApi,
        runner: str,
        me: Identity,
        pm: Identity,
        agent: Agent,
        heartbeat_every: float = 60.0,
    ) -> None:
        self.github = github
        self.heartbeat_every = heartbeat_every
        self._unpostable: set[str] = set()  # runs whose report GitHub refused: not paid for twice
        self._paused: dict[str, int] = {}  # run_id -> when a usage limit lets it run again
        self._pauses: dict[str, int] = {}  # run_id -> how many times it was paused
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
        self._paused = {
            r: until
            for r, until in self._paused.items()
            if r in {a.run_id for a in state.active_runs}
        }
        mine = [
            run
            for run in state.active_runs
            if run.runner == self.runner
            and run.run_id not in reported
            and run.run_id not in self._unpostable
            and run.state == state.status.value
        ]
        done: list[str] = []
        for run in mine:
            if now < self._paused.get(run.run_id, 0):
                continue  # still waiting for a usage limit to reset
            self._paused.pop(run.run_id, None)
            self.beat(now, running=[run.run_id])
            try:
                self._execute(issue, run, state.spec_rev, state.approved_spec_rev, state.answers)
            except QuotaPause as pause:
                resume = pause.until if pause.until is not None else now + PAUSE_DEFAULT
                self._pauses[run.run_id] = self._pauses.get(run.run_id, 0) + 1
                if resume - now <= PAUSE_BUDGET and self._pauses[run.run_id] <= MAX_PAUSES:
                    self._paused[run.run_id] = resume
                continue
            except AgentFailure:
                continue
            done.append(run.run_id)
        return done

    def beat(self, now: int, running: Sequence[str] = ()) -> None:
        """Report the runs this runner holds: the one being executed and any paused by a limit."""
        paused = {r: until for r, until in self._paused.items() if until - now <= PAUSE_BUDGET}
        write_heartbeat(self.github, self.runner, [*running, *paused], now, paused)

    def _execute(
        self,
        issue: int,
        run: ActiveRun,
        spec_rev: str | None,
        approved: str | None,
        answers: tuple[str, ...],
    ) -> None:
        info = self.github.get_issue(issue)
        # the agent works on text only: a binary file stays in the repository, out of its sight
        tree = (
            {
                p: c
                for p, c in self.github.read_tree(run.head).items()
                if not isinstance(c, BinaryBlob)
            }
            if run.head
            else {}
        )
        task = AgentTask(
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
            answers=answers,
            lessons=tuple(select_lessons(load_lessons(tree)[0], run.state, tree.keys())),
        )
        with self._beating(run.run_id):
            result = self.agent.run(task)
        state = RunState(run.state)
        commit = run.head
        commits: tuple[str, ...] = ()
        pushable = {p: c for p, c in result.changes.items() if not matches_any(p, WORKFLOW_PATHS)}
        proposed = {
            p: c
            for p, c in result.changes.items()
            if matches_any(p, WORKFLOW_PATHS) and c is not None
        }
        if state in _COMMITTING and pushable and run.head is not None:
            commit = self._commit(issue, run, pushable)
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
            "produced": _fit(result.produced),
            "decisions": _fit(result.decisions),
            "doubts": _fit(result.doubts),
            "lessons_applied": _fit(result.lessons_applied),
            "lessons_proposed": _fit(result.lessons_proposed),
            "plan_deviations": _fit(result.plan_deviations),
            "run_commits": list(commits),
            "reviewer": run.reviewer,
            "workflow_change_request": proposed,
        }
        body = render_handoff_comment(raw)
        if len(body) > MAX_COMMENT:  # only a workflow change request can be this large
            raw["workflow_change_request"] = {}
            raw["doubts"] = [*raw["doubts"], "A workflow change request was too large to attach."]  # type: ignore[misc]
            body = render_handoff_comment(raw)
        try:
            self.github.add_comment(issue, body)
        except GitHubError as error:
            self._unpostable.add(run.run_id)
            raise AgentFailure(f"the report was not accepted by GitHub: {error}") from error

    @contextmanager
    def _beating(self, run_id: str) -> Generator[None]:
        """Keep the run's lease alive while the agent works: a run can outlast the lease (3 min)
        many times over, and a lapsed lease makes the PM discard its report."""
        stop = threading.Event()
        started = time.monotonic()
        base = int(time.time())

        def beat() -> None:
            while not stop.wait(self.heartbeat_every):
                try:
                    write_heartbeat(
                        self.github, self.runner, [run_id], base + int(time.monotonic() - started)
                    )
                except Exception:  # noqa: BLE001, S110  a missed beat must not kill the run
                    pass

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def _commit(self, issue: int, run: ActiveRun, changes: Mapping[str, str | None]) -> str:
        ref = f"pm-wip/{self.runner}/{issue}/{run.run_id}"
        existing = self.github.get_ref(ref)
        # A ref already ahead of the run's head holds what an earlier attempt pushed. One still at
        # the head is a crash between creating the ref and committing: commit now, or the report
        # would name the head as the agent's work and the PM would accept it.
        if existing is not None and existing != run.head:
            return existing
        assert run.head is not None
        if existing is None:
            self.github.create_branch(ref, run.head)
        return self.github.commit(
            ref,
            f"{run.state}: #{issue} ({run.run_id})",
            changes,
            author=self.runner,
            committer=self.runner,
        )


def _fit(items: Sequence[str]) -> list[str]:
    """Agent text is bounded so the report always fits in a comment."""
    kept: list[str] = []
    used = 0
    for index, item in enumerate(items):
        text = item if len(item) <= _ITEM else item[:_ITEM] + "... (truncated)"
        if used + len(text) > _LIST:
            kept.append(f"... ({len(items) - index} more omitted)")
            break
        kept.append(text)
        used += len(text)
    return kept


def _run_markers(body: str) -> list[str]:
    import re

    return re.findall(r"<!-- pm-run:([^ ]+) -->", body)
