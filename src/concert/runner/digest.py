"""The daily digest in Steering: delivery metrics and what needs attention.

The numbers come from `core.metrics` (event logs only); this module turns them into text and
posts at most one digest per UTC day, found again by a marker so a restart does not repeat it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from concert.core.kits import has_marker
from concert.core.metrics import DAY, Digest, RevertWeek, WeekMetrics
from concert.github.api import GitHubApi, Identity


def digest_marker(day: str) -> str:
    return f"<!-- pm-digest:{day} -->"


def utc_day(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def post_digest_once_a_day(
    github: GitHubApi, steering: int, pm: Identity, now: int, body: str
) -> bool:
    """Post `body` under today's marker unless the PM already did today. True when posted."""
    marker = digest_marker(utc_day(now))
    for comment in github.list_comments(steering):
        if comment.author.user_id == pm.user_id and has_marker(comment.body, marker):
            return False
    github.add_comment(steering, f"{body}\n\n{marker}")  # a marker counts only as the last line
    return True


def render_digest(digest: Digest, attention: Sequence[str] = ()) -> str:
    lines = [f"## Daily digest, {utc_day(digest.now)}", ""]
    for label, week in zip(("This week", "Last week"), digest.weeks, strict=True):
        lines += _week_lines(label, week)
    lines += ["", "### Reverts, by the week of the merge they undid"]
    for label, revert in zip(("This week", "Last week"), digest.reverts, strict=True):
        lines.append(_revert_line(label, revert))
    lines += [
        "",
        "### Human involvement",
        f"- routing interventions per issue: {_number(digest.interventions_per_issue)}",
        f"- repeat human approvals per issue: {_number(digest.repeat_approvals_per_issue)}",
        (
            f"- waiting on a human gate: median {_duration(digest.gate_wait_median)}, "
            f"p95 {_duration(digest.gate_wait_p95)}"
        ),
        f"- CI runs per completed issue: {_number(digest.ci_runs_per_issue)}",
    ]
    if attention:
        lines += ["", "### Needs attention", *(f"- {item}" for item in attention)]
    return "\n".join(lines)


def _week_lines(label: str, week: WeekMetrics) -> list[str]:
    lines = [
        f"### {label} (from {datetime.fromtimestamp(week.start, UTC).strftime('%Y-%m-%d')})",
        (
            f"- landed on staging: {week.landed}; lead time median {_duration(week.lead_median)}, "
            f"p95 {_duration(week.lead_p95)}"
        ),
        f"- first pass: {week.first_pass}/{week.landed} ({_percent(week.first_pass_rate)})",
    ]
    if week.reentries:
        causes = ", ".join(f"{cause} {count}" for cause, count in sorted(week.reentries.items()))
        lines.append(f"- re-entries: {causes}")
    lines.append(
        f"- open: {week.open} (median age {_days(week.open_median_age_days)}); "
        f"blocked: {week.blocked} (median age {_days(week.blocked_median_age_days)}); "
        f"closed: {week.closed}"
    )
    return lines


def _revert_line(label: str, week: RevertWeek) -> str:
    note = " (can still change)" if week.restated else ""
    return (
        f"- {label}: {week.reverted_issues} of {week.merged} merged issues reverted "
        f"({_percent(week.rate)}), {week.revert_events} revert events{note}"
    )


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _days(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} d"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds >= DAY:
        return f"{seconds / DAY:.1f} d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds // 60} min"
