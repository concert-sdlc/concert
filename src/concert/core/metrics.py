"""Delivery metrics, computed from the event logs alone (no new stored data).

Pure. A log is a list of `Timed` events: the event and when it was published (the comment time).
An *attempt* starts at a `/dispatch` and ends at `on_staging`, `closed` or the present; lead time,
re-entries and first-pass are computed within an attempt, so a reopened and redispatched issue
has a closed attempt and a new one. Revert-type issues are never counted as merged issues or in
lead time, `closed` attempts are left out of the lead-time and first-pass populations and counted
apart, and a revert belongs to the week of the merge it undid. Staging rebases, pre-gate refreshes
and infra retries are not re-entries (they never produce a `counter_charged` event).
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from concert.core.events import Event

DAY = 86_400
WEEK = 7 * DAY
_MONDAY = 4 * DAY  # 1970-01-05 00:00 UTC was a Monday
_REVERT_ATTRIBUTION_DAYS = 14  # a week's revert rate is restated daily until it is this old

# Re-entry causes as the digest groups them.
_GROUP = {
    "changes_needed": "review",
    "uat_failed": "uat",
    "reject_uat": "reject",
    "reject_spec": "reject",
    "spec_drift": "spec drift",
}
_HUMAN_GATES = frozenset({"spec_approval", "human_uat"})


def week_start(timestamp: int) -> int:
    """Monday 00:00 UTC of the calendar week containing the timestamp."""
    return timestamp - ((timestamp - _MONDAY) % WEEK)


@dataclass(frozen=True)
class Timed:
    event: Event
    at: int


@dataclass(frozen=True)
class Attempt:
    started_at: int
    ended_at: int | None = None
    outcome: str | None = None  # "on_staging", "closed", or None while it is open
    reentries: Mapping[str, int] = field(default_factory=dict[str, int])

    @property
    def lead_time(self) -> int | None:
        if self.outcome != "on_staging" or self.ended_at is None:
            return None
        return self.ended_at - self.started_at


@dataclass
class _Open:
    started_at: int
    reentries: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(frozen=True)
class Observation:
    """What one issue's log says."""

    issue: int
    issue_type: str
    status: str
    attempts: tuple[Attempt, ...]
    merged_at: int | None  # the first time the PM merged it to staging
    reverted: bool  # a merge of this issue was auto-reverted
    revert_events: int
    plan_deviations: int  # deviations from the spec that reviewers reported
    approvals: int  # human approvals of the UAT gate
    ci_runs: int
    interventions: int  # commands that moved the issue (dispatch, approve, reject, unblock...)
    gate_waits: tuple[int, ...]  # seconds spent in a human gate, per visit


def observe(issue: int, timed: Sequence[Timed], status: str = "") -> Observation:
    issue_type = "code"
    attempts: list[Attempt] = []
    current: _Open | None = None
    actions: dict[str, str] = {}
    merged_at: int | None = None
    reverted = False
    revert_events = 0
    approvals = ci_runs = interventions = plan_deviations = 0
    gate: tuple[str, int] | None = None
    waits: list[int] = []
    last_status = status

    def close(outcome: str | None, at: int | None) -> None:
        nonlocal current
        if current is None:
            return
        attempts.append(
            Attempt(
                started_at=current.started_at,
                ended_at=at,
                outcome=outcome,
                reentries=dict(current.reentries),
            )
        )
        current = None

    for item in timed:
        event, at = item.event, item.at
        data = event.data
        kind = event.kind
        if kind in ("created", "issue_typed"):
            issue_type = str(data.get("issue_type", issue_type))
        elif kind == "gate_passage":
            interventions += 1
            if data.get("verb") == "dispatch":
                started = int(data.get("created_at", at))
                if current is not None:
                    close(None, None)  # a re-dispatch without an end: the earlier one stays open
                current = _Open(started)
        elif kind == "counter_charged" and current is not None:
            group = _GROUP.get(str(data["cause"]), str(data["cause"]))
            current.reentries[group] = current.reentries.get(group, 0) + 1
        elif kind == "transition":
            target = str(data["to"])
            source = str(data["from"])
            last_status = target
            if gate is not None and gate[0] == source:
                waits.append(at - gate[1])
                gate = None
            if target in _HUMAN_GATES:
                gate = (target, at)
            if target == "on_staging":
                close("on_staging", at)
            elif target == "closed":
                close("closed", at)
        elif kind == "intent":
            actions[str(data["key"])] = str(data["action"])
        elif kind == "outcome":
            action = actions.get(str(data["key"]), "")
            result: Mapping[str, Any] = data.get("result") or {}
            if action == "merge" and result.get("sha") and merged_at is None:
                merged_at = at
            if action == "merge_revert" and result.get("sha"):
                revert_events += 1
                reverted = True
        elif kind == "handoff_accepted":
            plan_deviations += len(data.get("plan_deviations", ()))
        elif kind == "evidence":
            if data.get("kind") == "approval" and data.get("item") == "uat":
                approvals += 1
            if data.get("kind") == "ci":
                ci_runs += 1
    close(None, None)
    return Observation(
        issue=issue,
        issue_type=issue_type,
        status=last_status,
        attempts=tuple(attempts),
        merged_at=merged_at,
        reverted=reverted,
        revert_events=revert_events,
        plan_deviations=plan_deviations,
        approvals=approvals,
        ci_runs=ci_runs,
        interventions=interventions,
        gate_waits=tuple(waits),
    )


@dataclass(frozen=True)
class WeekMetrics:
    """One calendar week of dispatches."""

    start: int
    landed: int  # attempts that reached on_staging
    lead_median: int | None
    lead_p95: int | None
    first_pass: int
    first_pass_rate: float | None
    reentries: Mapping[str, int]
    open: int
    open_median_age_days: float | None
    blocked: int
    blocked_median_age_days: float | None
    closed: int  # closed attempts, left out of the populations above


@dataclass(frozen=True)
class RevertWeek:
    """Merges attributed to the week they landed in, and how many of those issues were reverted."""

    start: int
    merged: int
    reverted_issues: int
    revert_events: int
    restated: bool  # the week is younger than the restatement window: the figure can still move

    @property
    def rate(self) -> float | None:
        return self.reverted_issues / self.merged if self.merged else None


@dataclass(frozen=True)
class Digest:
    now: int
    weeks: tuple[WeekMetrics, ...]  # the current week, then the previous one
    reverts: tuple[RevertWeek, ...]
    interventions_per_issue: float | None
    ci_runs_per_issue: float | None
    repeat_approvals_per_issue: float | None
    gate_wait_median: int | None
    gate_wait_p95: int | None
    plan_deviations: int = 0


def compute(observations: Sequence[Observation], now: int) -> Digest:
    this_week = week_start(now)
    weeks = (this_week, this_week - WEEK)
    real = [o for o in observations if o.issue_type not in ("revert", "workflow")]
    return Digest(
        now=now,
        weeks=tuple(_week(real, start, now) for start in weeks),
        reverts=tuple(_reverts(real, start, now) for start in weeks),
        interventions_per_issue=_mean([o.interventions for o in real]),
        ci_runs_per_issue=_mean([o.ci_runs for o in real if _landed(o)]),
        repeat_approvals_per_issue=_mean([max(0, o.approvals - 1) for o in real if o.approvals]),
        gate_wait_median=_median([w for o in real for w in o.gate_waits]),
        gate_wait_p95=_percentile([w for o in real for w in o.gate_waits], 95),
        plan_deviations=sum(o.plan_deviations for o in real),
    )


def _landed(observation: Observation) -> bool:
    return any(a.outcome == "on_staging" for a in observation.attempts)


def _week(observations: Sequence[Observation], start: int, now: int) -> WeekMetrics:
    attempts = [a for o in observations for a in o.attempts if week_start(a.started_at) == start]
    landed = [a for a in attempts if a.outcome == "on_staging"]
    leads = [lead for a in landed if (lead := a.lead_time) is not None]
    clean = [a for a in landed if not any(a.reentries.values())]
    reentries: dict[str, int] = {}
    for attempt in landed:
        for cause, count in attempt.reentries.items():
            reentries[cause] = reentries.get(cause, 0) + count
    blocked_issues = {o.issue for o in observations if o.status == "blocked"}
    still_open = [
        (a, o)
        for o in observations
        for a in o.attempts
        if a.outcome is None and week_start(a.started_at) == start
    ]
    blocked_ages = [(now - a.started_at) / DAY for a, o in still_open if o.issue in blocked_issues]
    open_ages = [(now - a.started_at) / DAY for a, o in still_open if o.issue not in blocked_issues]
    return WeekMetrics(
        start=start,
        landed=len(landed),
        lead_median=_median(leads),
        lead_p95=_percentile(leads, 95),
        first_pass=len(clean),
        first_pass_rate=len(clean) / len(landed) if landed else None,
        reentries=reentries,
        open=len(open_ages),
        open_median_age_days=_median_float(open_ages),
        blocked=len(blocked_ages),
        blocked_median_age_days=_median_float(blocked_ages),
        closed=sum(1 for a in attempts if a.outcome == "closed"),
    )


def _reverts(observations: Sequence[Observation], start: int, now: int) -> RevertWeek:
    merged = [
        o for o in observations if o.merged_at is not None and week_start(o.merged_at) == start
    ]
    return RevertWeek(
        start=start,
        merged=len(merged),
        reverted_issues=sum(1 for o in merged if o.reverted),
        revert_events=sum(o.revert_events for o in merged),
        restated=now < start + WEEK + _REVERT_ATTRIBUTION_DAYS * DAY,
    )


def _mean(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[int]) -> int | None:
    return int(statistics.median(values)) if values else None


def _median_float(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _percentile(values: Sequence[int], percent: int) -> int | None:
    """Nearest rank: the smallest value with at least `percent` of the sample at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]
