"""A quota pause keeps the lease (the run stays in the heartbeat) and costs no retry."""

from concert.core.assign import ASSIGN_GRACE
from concert.github.heartbeats import read_heartbeat
from concert.runner.worker import AgentResult, AgentTask, QuotaPause, Worker
from tests.runner.helpers import PM, VM1, World
from tests.runner.test_assignment import dispatched
from tests.runner.test_worker import Scripted


class Limited(Scripted):
    def __init__(self, until: int | None, times: int = 1) -> None:
        super().__init__()
        self.until, self.times = until, times

    def run(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        if len(self.tasks) <= self.times:
            raise QuotaPause("usage limit", self.until)
        return AgentResult(exit="spec")


def setup() -> tuple[World, int, str]:
    world = World(auto=True)
    issue = dispatched(world)
    world.heartbeat()
    world.poll(issue)
    return world, issue, world.active(issue)[0].run_id


def test_a_paused_run_is_not_run_again_before_the_reset_and_posts_nothing() -> None:
    world, issue, run = setup()
    now = world.fake.now
    agent = Limited(until=now + 600)
    w = Worker(world.fake.client(VM1), "vm-1", VM1, PM, agent)
    assert w.run_once(issue, now) == []
    assert w.run_once(issue, now + 100) == []
    assert len(agent.tasks) == 1
    assert w.run_once(issue, now + 601) == [run]
    assert len(agent.tasks) == 2


def test_the_heartbeat_keeps_the_paused_run_and_says_until_when() -> None:
    world, issue, run = setup()
    now = world.fake.now
    w = Worker(world.fake.client(VM1), "vm-1", VM1, PM, Limited(until=now + 600))
    w.run_once(issue, now)
    w.beat(now + 5)
    beat = read_heartbeat(world.pm, "vm-1")
    assert beat is not None and run in beat.run_ids
    assert beat.paused == {run: now + 600}


def test_a_paused_run_keeps_its_lease_and_is_charged_no_retry() -> None:
    world, issue, run = setup()
    w = Worker(world.fake.client(VM1), "vm-1", VM1, PM, Limited(until=world.fake.now + 3_000))
    w.run_once(issue, world.fake.now)
    for _ in range(6):  # far longer than the lease would last without heartbeats
        world.fake.tick(ASSIGN_GRACE)
        w.beat(world.fake.now)
        world.poll(issue)
    assert [r.run_id for r in world.active(issue)] == [run]
    assert world.state(issue).infra_attempts == {}


def test_a_pause_without_a_reset_time_waits_a_default_while_and_retries() -> None:
    world, issue, run = setup()
    now = world.fake.now
    agent = Limited(until=None)
    w = Worker(world.fake.client(VM1), "vm-1", VM1, PM, agent)
    w.run_once(issue, now)
    assert w.run_once(issue, now + 60) == []
    assert w.run_once(issue, now + 901) == [run]


def test_a_reset_beyond_the_wait_budget_is_not_waited_for() -> None:
    """The run drops out of the heartbeat, its lease lapses and the PM reassigns (design:
    a quota pause with no ETA within the wait budget lets the PM assign the next attempt)."""
    world, issue, run = setup()
    now = world.fake.now
    w = Worker(world.fake.client(VM1), "vm-1", VM1, PM, Limited(until=now + 20 * 3600))
    w.run_once(issue, now)
    w.beat(now + 1)
    beat = read_heartbeat(world.pm, "vm-1")
    assert beat is not None and run not in beat.run_ids and not beat.paused


def test_a_run_that_keeps_hitting_the_limit_is_eventually_left_to_lapse() -> None:
    from concert.runner.worker import MAX_PAUSES

    world, issue, run = setup()
    now = world.fake.now
    w = Worker(world.fake.client(VM1), "vm-1", VM1, PM, Limited(until=None, times=10_000))
    for _ in range(MAX_PAUSES + 1):
        now += 1_000
        w.run_once(issue, now)
    w.beat(now)
    beat = read_heartbeat(world.pm, "vm-1")
    assert beat is not None and run not in beat.run_ids
