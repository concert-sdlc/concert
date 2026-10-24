"""Scripting helpers for runner tests: a PM, an approver and a worker on the fake GitHub."""

from collections.abc import Mapping
from typing import Any

from concert.core.commands import Role
from concert.core.events import append_event, parse_event
from concert.core.spec_hash import spec_tree_hash
from concert.github.api import GitHubApi, Identity
from concert.github.comments import extract_events, render_event_comment, render_handoff_comment
from concert.github.git_facts import GitFacts
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
)

SPEC_FILES = {
    "docs/specs/1/spec.md": "# spec",
    "docs/specs/1/plan.md": "# plan",
    "docs/specs/1/uat.md": "# uat",
}


class World:
    def __init__(self) -> None:
        self.fake = FakeGitHub()
        self.fake.seed_branch("staging", {"README": "x"})
        self.pm: GitHubApi = self.fake.client(PM)
        self.facts = GitFacts(self.pm, base_branch="staging")
        self.runs = 0

    def new_issue(self) -> int:
        return self.fake.client(ALICE).create_issue("Export CSV", "Please").number

    def poll(self, issue: int) -> PollResult:
        self.fake.tick(1)
        return poll_issue(self.pm, issue, CONFIG, self.facts)

    def say(self, who: Identity, issue: int, body: str) -> int:
        return self.fake.client(who).add_comment(issue, body).id

    def pm_post(self, issue: int, kind: str, data: Mapping[str, Any]) -> None:
        extraction = extract_events(self.pm.list_comments(issue), PM)
        chain = [parse_event(r.event) for r in extraction.records]
        event = append_event(chain, epoch=CONFIG.epoch, kind=kind, data=data)
        self.pm.add_comment(issue, render_event_comment([event]))

    def assign(self, issue: int, head: str | None) -> str:
        self.runs += 1
        run_id = f"r-{self.runs}"
        self.pm_post(issue, "assignment", {"run_id": run_id, "runner": "vm-1", "head": head})
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

    def spec_hash(self, sha: str, issue: int = 1) -> str:
        return spec_tree_hash(self.pm.read_tree(sha), issue)
