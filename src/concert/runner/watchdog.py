"""The watchdog: notice that a runner has gone quiet, from outside the runner.

Runs on a schedule (an Action on the default branch). It reads the heartbeat refs, compares them
with the alerts already in the Steering issue, and posts one alert per silence, naming the admins,
and one note when the runner is back. Only markers written by the watchdog's own account count,
so nobody can pre-empt an alert by pasting one.
"""

from collections.abc import Collection, Sequence

from concert.core.assign import Heartbeat
from concert.core.kits import has_marker
from concert.core.watchdog import Alert, Recovered, plan_watchdog
from concert.github.api import GitHubApi
from concert.github.heartbeats import read_heartbeat


def run_watchdog(
    github: GitHubApi,
    steering: int,
    runners: Sequence[str],
    admins: Sequence[str],
    stale_after: int,
    now: int,
    authors: Collection[int],
) -> list[str]:
    heartbeats: dict[str, Heartbeat | None] = {r: read_heartbeat(github, r) for r in runners}
    alerted = _alerted(github, steering, runners, authors)
    done: list[str] = []
    for action in plan_watchdog(runners, heartbeats, now, stale_after, alerted):
        if isinstance(action, Alert):
            github.add_comment(steering, _alert_text(action, admins))
            done.append(f"alert {action.runner}")
        else:
            github.add_comment(steering, _recovered_text(action))
            done.append(f"recovered {action.runner}")
    return done


def _alerted(
    github: GitHubApi, steering: int, runners: Sequence[str], authors: Collection[int]
) -> frozenset[str]:
    """Runners whose latest watchdog note is an alert."""
    state: dict[str, bool] = {}
    for comment in sorted(github.list_comments(steering), key=lambda c: c.id):
        if comment.author.user_id not in authors:
            continue
        for runner in runners:
            if has_marker(comment.body, marker("alert", runner)):
                state[runner] = True
            elif has_marker(comment.body, marker("recovered", runner)):
                state[runner] = False
    return frozenset(r for r, silent in state.items() if silent)


def marker(kind: str, runner: str) -> str:
    return f"<!-- pm-watchdog:{kind}:{runner} -->"


def _alert_text(alert: Alert, admins: Sequence[str]) -> str:
    since = (
        "no heartbeat has ever been seen"
        if alert.age_seconds is None
        else f"the last heartbeat was {alert.age_seconds // 60} minutes ago"
    )
    mentions = " ".join(f"@{a}" for a in admins)
    return "\n".join(
        [
            f"## Runner `{alert.runner}` has gone quiet",
            "",
            f"{since.capitalize()}. Nothing is being assigned, merged or verified until it is back.",
            "",
            mentions,
            "",
            marker("alert", alert.runner),
        ]
    )


def _recovered_text(action: Recovered) -> str:
    return "\n".join(
        [f"Runner `{action.runner}` is beating again.", "", marker("recovered", action.runner)]
    )
