"""The watchdog command: read heartbeats, alert admins in Steering once, announce recovery."""

from concert.github.heartbeats import write_heartbeat
from concert.runner.watchdog import run_watchdog
from tests.runner.helpers import ALICE, MALLORY, PM, World

STALE = 30 * 60


def setup() -> tuple[World, int]:
    world = World()
    world.fake.seed_branch("concert-heartbeats/pm", {})
    return world, world.new_issue()


def beat(world: World, runner: str, at: int) -> None:
    write_heartbeat(world.fake.client(PM), runner, [], at)


def watch(world: World, steering: int, now: int) -> list[str]:
    return run_watchdog(
        world.pm,
        steering,
        runners=["vm-1", "pm"],
        admins=["alice", "bob"],
        stale_after=STALE,
        now=now,
        authors={PM.user_id},
    )


def posted(world: World, steering: int) -> list[str]:
    return [c.body for c in world.pm.list_comments(steering) if c.author.user_id == PM.user_id]


def test_a_silent_runner_is_alerted_in_steering_mentioning_the_admins() -> None:
    world, steering = setup()
    beat(world, "vm-1", 1000)
    beat(world, "pm", 100_000)
    actions = watch(world, steering, now=1000 + STALE + 60)
    assert actions == ["alert vm-1"]
    [alert] = posted(world, steering)
    assert "vm-1" in alert and "@alice" in alert and "@bob" in alert
    assert alert.rstrip().endswith("<!-- pm-watchdog:alert:vm-1 -->")


def test_the_same_silence_is_alerted_only_once() -> None:
    world, steering = setup()
    beat(world, "vm-1", 1000)
    beat(world, "pm", 1000 + STALE + 60)
    watch(world, steering, now=1000 + STALE + 60)
    beat(world, "pm", 1000 + STALE + 7200)
    assert watch(world, steering, now=1000 + STALE + 7200) == []
    assert len([p for p in posted(world, steering) if "pm-watchdog:alert:vm-1" in p]) == 1


def test_a_runner_that_returns_is_announced_and_can_be_alerted_again_later() -> None:
    world, steering = setup()
    beat(world, "vm-1", 1000)
    beat(world, "pm", 1000)
    watch(world, steering, now=1000 + STALE + 60)
    now = 1000 + STALE + 600
    beat(world, "vm-1", now)
    beat(world, "pm", now)
    assert watch(world, steering, now=now + 30) == ["recovered vm-1", "recovered pm"]
    assert watch(world, steering, now=now + 60) == []
    later = now + STALE + 3600
    beat(world, "pm", later)
    assert watch(world, steering, now=later) == ["alert vm-1"]


def test_a_runner_that_never_beat_is_alerted() -> None:
    world, steering = setup()
    beat(world, "pm", 1000)
    assert watch(world, steering, now=1010) == ["alert vm-1"]
    assert "No heartbeat" in posted(world, steering)[0]


def test_someone_elses_marker_cannot_silence_the_watchdog() -> None:
    world, steering = setup()
    beat(world, "pm", 1000)
    world.say(MALLORY, steering, "<!-- pm-watchdog:alert:vm-1 -->")
    world.say(ALICE, steering, "text\n<!-- pm-watchdog:alert:vm-1 -->")
    assert watch(world, steering, now=1010) == ["alert vm-1"]


def test_mentions_cannot_be_forged_through_the_runner_names() -> None:
    world, steering = setup()
    beat(world, "pm", 1000)
    run_watchdog(
        world.pm,
        steering,
        runners=["vm-1"],
        admins=["alice"],
        stale_after=STALE,
        now=10_000,
        authors={PM.user_id},
    )
    assert "@alice" in posted(world, steering)[0]
