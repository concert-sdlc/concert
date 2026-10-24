"""Scripting helpers for runner tests: a PM, an approver and a worker on the fake GitHub."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from concert.core.assign import RunnerInfo
from concert.core.commands import Role
from concert.core.derive import ActiveRun, IssueState, derive
from concert.core.events import append_event, parse_event
from concert.core.spec_hash import spec_tree_hash
from concert.github.api import GitHubApi, Identity
from concert.github.comments import render_handoff_comment
from concert.github.git_facts import GitFacts
from concert.github.heartbeats import heartbeat_ref, write_heartbeat
from concert.github.mirror import Mirror, publish_events, reconcile
from concert.runner.poll import PollResult, RunnerConfig, poll_issue
from tests.fakes.github import FakeGitHub

PM = Identity(user_id=900, login="concert-pm[bot]", is_bot=True)
VM1 = Identity(user_id=346900002, login="concert-vm1[bot]", is_bot=True)
ALICE = Identity(user_id=1, login="alice", is_bot=False)
MALLORY = Identity(user_id=666, login="mallory", is_bot=False)

CONFIG = RunnerConfig(
    pm=PM,
    runner_ids={"vm-1": VM1.user_id},
    roles={ALICE.user_id: Role.APPROVER},
    epoch=1,
    budget=4,
    base_branch="staging",
    runners=(),
)

# The same project with real assignments: one runner that can run Docker and a browser.
CONFIG_AUTO = replace(
    CONFIG, runners=(RunnerInfo("vm-1", caps=frozenset({"docker", "browser"}), max_parallel=2),)
)

SPEC_FILES = {
    "docs/specs/1/spec.md": "# spec",
    "docs/specs/1/plan.md": "# plan",
    "docs/specs/1/uat.md": "# uat",
}


def spec_files(issue: int) -> dict[str, str]:
    return {path.replace("/1/", f"/{issue}/"): body for path, body in SPEC_FILES.items()}


class World:
    def __init__(self, auto: bool = False) -> None:
        self.config = CONFIG_AUTO if auto else CONFIG
        self.fake = FakeGitHub()
        self.fake.seed_branch("staging", {"README": "x"})
        self.fake.seed_branch("concert-events", {})
        self.fake.seed_branch(heartbeat_ref("vm-1"), {})
        self.pm: GitHubApi = self.fake.client(PM)
        self.pm_identity = PM
        self.fake_vm1 = VM1
        self.mirror = Mirror(self.pm, author=PM.login)
        self.facts = GitFacts(self.pm, base_branch="staging")
        self.runs = 0

    def new_issue(self) -> int:
        return self.fake.client(ALICE).create_issue("Export CSV", "Please").number

    def poll(
        self, issue: int, load: dict[str, int] | None = None, at_queue_head: bool = True
    ) -> PollResult:
        self.fake.tick(1)
        return poll_issue(
            self.pm,
            self.mirror,
            issue,
            self.config,
            self.facts,
            now=self.fake.now,
            load=load,
            at_queue_head=at_queue_head,
        )

    def heartbeat(self, *run_ids: str) -> None:
        """The runner vm-1 reports the runs it is executing."""
        write_heartbeat(self.fake.client(VM1), "vm-1", run_ids, now=self.fake.now)

    def state(self, issue: int) -> IssueState:
        trusted = reconcile(self.pm, self.mirror, issue, self.pm.list_comments(issue), PM)
        return derive(trusted.events)

    def active(self, issue: int) -> list[ActiveRun]:
        return list(self.state(issue).active_runs)

    def say(self, who: Identity, issue: int, body: str) -> int:
        return self.fake.client(who).add_comment(issue, body).id

    def pm_post(self, issue: int, kind: str, data: Mapping[str, Any]) -> None:
        trusted = reconcile(self.pm, self.mirror, issue, self.pm.list_comments(issue), PM)
        chain = [parse_event(event) for event in trusted.events]
        event = append_event(chain, epoch=CONFIG.epoch, kind=kind, data=data)
        publish_events(self.pm, self.mirror, issue, [event])

    def assign(self, issue: int, head: str | None, reviewer: str | None = None) -> str:
        trusted = reconcile(self.pm, self.mirror, issue, self.pm.list_comments(issue), PM)
        status = derive(trusted.events).status.value
        self.runs += 1
        run_id = f"r-{self.runs}"
        self.pm_post(
            issue,
            "assignment",
            {
                "run_id": run_id,
                "runner": "vm-1",
                "state": status,
                "reviewer": reviewer,
                "head": head,
                "issued_at": self.fake.now,
            },
        )
        return run_id

    def handoff(self, issue: int, run_id: str, state: str, exit_name: str, **fields: Any) -> int:
        raw: dict[str, Any] = {
            "schema": 1,
            "state": state,
            "run_id": run_id,
            "runner": "vm-1",
            "exit": exit_name,
            "commit_sha": None,
            "artifact_rev": None,
        } | fields
        return self.fake.client(VM1).add_comment(issue, render_handoff_comment(raw)).id

    def worker_commit(self, branch: str, files: Mapping[str, str | None]) -> str:
        return self.fake.client(VM1).commit(
            branch, "work", files, author=VM1.login, committer=VM1.login
        )

    def wip_commit(self, issue: int, run_id: str, files: Mapping[str, str | None]) -> str:
        """The worker commits to its own WIP ref for the run, created from the issue branch."""
        ref = f"pm-wip/vm-1/{issue}/{run_id}"
        worker = self.fake.client(VM1)
        if worker.get_ref(ref) is None:
            worker.create_branch(ref, f"pm/{issue}")
        return worker.commit(ref, "work", files, author=VM1.login, committer=VM1.login)

    def spec_hash(self, sha: str, issue: int = 1) -> str:
        return spec_tree_hash(self.pm.read_tree(sha), issue)
