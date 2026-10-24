"""The watchdog: when a runner's silence becomes an alert, and when it ends."""

from collections.abc import Mapping

from concert.core.assign import Heartbeat
from concert.core.watchdog import Alert, Recovered, plan_watchdog

NOW = 100_000
STALE = 30 * 60


def beat(runner: str, age: int) -> Heartbeat:
    return Heartbeat(runner=runner, at=NOW - age, run_ids=frozenset())


def plan(
    beats: Mapping[str, Heartbeat | None], alerted: frozenset[str] = frozenset()
) -> list[Alert | Recovered]:
    return plan_watchdog(["vm-1", "pm"], beats, NOW, STALE, alerted)


def test_fresh_runners_need_nothing() -> None:
    assert plan({"vm-1": beat("vm-1", 60), "pm": beat("pm", 90)}) == []


def test_a_silent_runner_is_alerted_once() -> None:
    actions = plan({"vm-1": beat("vm-1", STALE + 1), "pm": beat("pm", 10)})
    assert actions == [Alert("vm-1", age_seconds=STALE + 1)]
    assert (
        plan({"vm-1": beat("vm-1", STALE + 1), "pm": beat("pm", 10)}, alerted=frozenset({"vm-1"}))
        == []
    )


def test_the_boundary_counts_as_stale_only_beyond_the_limit() -> None:
    assert plan({"vm-1": beat("vm-1", STALE), "pm": beat("pm", 0)}) == []


def test_a_runner_with_no_heartbeat_at_all_is_alerted() -> None:
    actions = plan({"vm-1": None, "pm": beat("pm", 0)})
    assert actions == [Alert("vm-1", age_seconds=None)]


def test_a_runner_that_comes_back_is_announced_once() -> None:
    fresh = {"vm-1": beat("vm-1", 20), "pm": beat("pm", 0)}
    assert plan(fresh, alerted=frozenset({"vm-1"})) == [Recovered("vm-1")]
    assert plan(fresh) == []  # nothing was alerted, so there is nothing to recover from


def test_each_runner_is_judged_on_its_own() -> None:
    beats = {"vm-1": beat("vm-1", STALE + 5), "pm": beat("pm", STALE + 9)}
    assert [a.runner for a in plan(beats)] == ["vm-1", "pm"]
    mixed = plan(beats, alerted=frozenset({"pm"}))
    assert [a.runner for a in mixed] == ["vm-1"]


def test_a_heartbeat_from_the_future_is_treated_as_fresh() -> None:
    assert plan({"vm-1": beat("vm-1", -30), "pm": beat("pm", 0)}) == []
