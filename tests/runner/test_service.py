"""The service loop: load process.yaml, beat, run the cycle, let the worker execute."""

from dataclasses import dataclass

import pytest

from concert.core.transitions import Status
from concert.github.heartbeats import read_heartbeat
from concert.runner.config import ConfigError
from concert.runner.service import Service
from concert.runner.worker import AgentResult, Worker
from tests.runner.helpers import ALICE, PM, VM1, World
from tests.runner.test_journey import agent_for

PROCESS_YAML = f"""
version: 0
humans:
  admins:    [{{ login: alice, id: {ALICE.user_id} }}]
  approvers: [{{ login: alice, id: {ALICE.user_id} }}]
runners:
  - {{ name: vm-1, identity: "concert-vm1[bot]", bot_user_id: {VM1.user_id},
      caps: [docker, browser, claude-code], max_parallel: 2 }}
pm_identity: {{ login: "{PM.login}", bot_user_id: {PM.user_id} }}
dod:
  tests: pass
workflows:
  allowed_hashes:
    concert-ci.yml:
      - {{ sha256: w1, template: concert-ci@1.0.0, inputs: x }}
"""


@dataclass
class Rig:
    world: World
    service: Service
    issue: int


def rig(yaml: str | None = PROCESS_YAML) -> Rig:
    world = World()
    if yaml is not None:
        world.fake.client(PM).commit(
            "staging", "config", {"process.yaml": yaml}, author="admin", committer="admin"
        )
    issue = world.new_issue()
    agent = agent_for(issue)
    worker = Worker(world.fake.client(VM1), "vm-1", VM1, PM, agent)
    service = Service(
        pm_github=world.pm,
        mirror=world.mirror,
        facts=world.facts,
        worker=worker,
        runner="vm-1",
        base_branch="staging",
        clock=lambda: world.fake.now,
    )
    return Rig(world, service, issue)


def tick(r: Rig, times: int = 1) -> None:
    for _ in range(times):
        r.world.fake.tick(1)
        r.service.tick()


def test_a_tick_adopts_open_issues_and_beats_the_heart_of_the_runner_and_the_pm() -> None:
    r = rig()
    tick(r)
    assert r.world.state(r.issue).status is Status.INBOX
    beat = read_heartbeat(r.world.pm, "vm-1")
    assert beat is not None and beat.at == r.world.fake.now
    assert read_heartbeat(r.world.pm, "pm") is not None


def test_a_missing_or_invalid_process_yaml_stops_the_tick_with_the_reason() -> None:
    with pytest.raises(ConfigError, match="process.yaml"):
        tick(rig(None))
    with pytest.raises(ConfigError, match="version"):
        tick(rig("version: 9\nhumans: {}\npm_identity: {login: p, bot_user_id: 1}"))


def test_the_whole_journey_runs_through_the_service() -> None:
    r = rig()
    world, issue = r.world, r.issue
    tick(r)
    world.say(ALICE, issue, "/dispatch")
    for _ in range(40):
        tick(r)
        if world.state(issue).status is Status.SPEC_APPROVAL:
            break
    world.say(ALICE, issue, f"/approve spec {world.state(issue).spec_rev}")
    for _ in range(40):
        tick(r)
        if world.state(issue).status is Status.HUMAN_UAT:
            break
    head = world.state(issue).head_sha or ""
    world.fake.add_check_run(head, "tests", "success", "w1")
    tick(r)
    world.say(ALICE, issue, f"/approve uat {head}")
    for _ in range(40):
        tick(r)
        if world.state(issue).status is Status.ON_STAGING:
            break
    assert world.state(issue).status is Status.ON_STAGING


def test_a_worker_failure_does_not_stop_the_loop() -> None:
    r = rig()

    class Exploding:
        def run(self, task: object) -> AgentResult:
            raise RuntimeError("agent crashed")

    r.service.worker.agent = Exploding()
    tick(r)
    r.world.say(ALICE, r.issue, "/dispatch")
    tick(r, 3)  # the PM keeps going; the failed run is only logged
    assert r.world.state(r.issue).status is Status.TRIAGE
    assert r.service.errors and "agent crashed" in r.service.errors[-1]


def test_one_issue_failing_does_not_stop_the_others() -> None:
    r = rig()
    second = r.world.new_issue()
    r.world.say(ALICE, r.issue, "/dispatch")
    r.world.say(ALICE, second, "/dispatch")
    real = r.service.pm_github

    class BrokenFor:
        def __init__(self, broken: int) -> None:
            self.broken = broken

        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

        def list_comments(self, issue: int, after_id: int = 0) -> object:
            if issue == self.broken:
                raise RuntimeError("GitHub exploded")
            return real.list_comments(issue, after_id)

    r.service.pm_github = BrokenFor(r.issue)  # type: ignore[assignment]
    tick(r, 3)
    assert r.world.state(second).status in (Status.TRIAGE, Status.SPEC, Status.SPEC_APPROVAL)
    assert any("#1" in e and "GitHub exploded" in e for e in r.service.errors)


def test_a_runner_that_is_not_registered_does_not_act() -> None:
    r = rig()
    r.service.runner = "intruder"
    with pytest.raises(ConfigError, match="not registered"):
        tick(r)


def test_the_worker_takes_its_identities_from_the_configuration() -> None:
    r = rig()
    r.service.worker.pm = PM.__class__(user_id=0, login="x", is_bot=True)
    r.service.worker.me = PM.__class__(user_id=0, login="x", is_bot=True)
    tick(r)
    assert r.service.worker.pm.user_id == PM.user_id and r.service.worker.me.user_id == VM1.user_id
