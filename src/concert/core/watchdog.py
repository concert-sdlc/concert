"""When a runner's silence becomes an alert, and when the alert ends.

Pure. The watchdog runs outside the runner (a scheduled Action), so it can notice that the
runner itself has stopped. A runner is silent when its newest heartbeat is older than the limit
or missing; each silence is alerted once, and the return is announced once.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from concert.core.assign import Heartbeat


@dataclass(frozen=True)
class Alert:
    runner: str
    age_seconds: int | None  # None: no heartbeat has ever been seen


@dataclass(frozen=True)
class Recovered:
    runner: str


def plan_watchdog(
    runners: Sequence[str],
    heartbeats: Mapping[str, Heartbeat | None],
    now: int,
    stale_after: int,
    alerted: frozenset[str],
) -> list[Alert | Recovered]:
    actions: list[Alert | Recovered] = []
    for runner in runners:
        beat = heartbeats.get(runner)
        age = None if beat is None else max(0, now - beat.at)
        silent = age is None or age > stale_after
        if silent and runner not in alerted:
            actions.append(Alert(runner, age))
        elif not silent and runner in alerted:
            actions.append(Recovered(runner))
    return actions
