"""Delivery metrics from a fixture event log with known answers."""

from collections.abc import Mapping
from typing import Any

from concert.core.events import Event, append_event
from concert.core.metrics import DAY, WEEK, Timed, compute, observe, week_start

MON = 4 * DAY + 10 * WEEK  # a Monday 00:00 UTC
NOW = MON + 3 * DAY  # Thursday of that week


class Log:
    """A hand-written event log for one issue."""

    def __init__(self, issue_type: str = "code") -> None:
        self.events: list[Event] = []
        self.timed: list[Timed] = []
        self.add(MON, "created", {"issue_type": issue_type})

    def add(self, at: int, kind: str, data: Mapping[str, Any]) -> "Log":
        event = append_event(self.events, epoch=1, kind=kind, data=data)
        self.events.append(event)
        self.timed.append(Timed(event, at))
        return self

    def dispatch(self, at: int) -> "Log":
        return self.add(
            at, "gate_passage", {"verb": "dispatch", "created_at": at, "comment_id": at}
        )

    def move(self, at: int, source: str, target: str, trigger: str = "x") -> "Log":
        return self.add(at, "transition", {"from": source, "to": target, "trigger": trigger})

    def revert(self, at: int) -> "Log":
        """The PM merged an auto-revert of this issue's merge, and verification of it passed."""
        self.add(at, "intent", {"key": f"revert{at}", "action": "merge_revert"})
        self.add(at, "outcome", {"key": f"revert{at}", "result": {"sha": "rev"}})
        return self.move(at + 1, "verifying_staging", "implementing", "failed_reverted")

    def charge(self, cause: str) -> "Log":
        return self.add(MON, "counter_charged", {"cause": cause})

    def merge(self, at: int) -> "Log":
        self.add(at, "intent", {"key": "merge", "action": "merge"})
        return self.add(at, "outcome", {"key": "merge", "result": {"sha": "abc"}})

    def landed(self, start: int, end: int, *causes: str) -> "Log":
        self.dispatch(start)
        for cause in causes:
            self.charge(cause)
        return self.move(end, "verifying_staging", "on_staging", "passed")


def digest(logs: list[Log], status: str = "on_staging"):  # type: ignore[no-untyped-def]
    return compute([observe(i, log.timed, status) for i, log in enumerate(logs, start=1)], NOW)


def test_weeks_start_on_monday_utc() -> None:
    assert week_start(MON) == MON and week_start(MON + WEEK - 1) == MON
    assert week_start(MON + WEEK) == MON + WEEK


def test_lead_time_median_and_p95_run_from_dispatch_to_on_staging() -> None:
    logs = [Log().landed(MON, MON + hours * 3600) for hours in (1, 2, 3, 4, 100)]
    week = digest(logs).weeks[0]
    assert week.landed == 5
    assert week.lead_median == 3 * 3600
    assert week.lead_p95 == 100 * 3600  # nearest rank over five values


def test_first_pass_counts_a_rebase_only_issue_but_not_a_rejected_one() -> None:
    clean = Log().landed(MON, MON + 3600)  # a staging rebase writes no charge
    rejected = Log().landed(MON, MON + 3600, "reject_uat")
    week = digest([clean, rejected]).weeks[0]
    assert (week.first_pass, week.first_pass_rate) == (1, 0.5)


def test_re_entries_are_grouped_by_cause() -> None:
    log = Log().landed(
        MON,
        MON + 3600,
        "changes_needed",
        "changes_needed",
        "uat_failed",
        "spec_drift",
        "reject_spec",
    )
    assert digest([log]).weeks[0].reentries == {
        "review": 2,
        "uat": 1,
        "spec drift": 1,
        "reject": 1,
    }


def test_revert_issues_and_closed_attempts_stay_out_of_the_populations() -> None:
    revert = Log("revert").landed(MON, MON + 60)
    closed = Log().dispatch(MON).move(MON + 3600, "inbox", "closed", "close")
    normal = Log().landed(MON, MON + 7200)
    week = digest([revert, closed, normal]).weeks[0]
    assert week.landed == 1 and week.lead_median == 7200
    assert week.closed == 1


def test_open_and_blocked_attempts_report_their_median_age_in_days() -> None:
    open_one = Log().dispatch(MON)  # three days old
    open_two = Log().dispatch(MON + DAY)  # two days old
    blocked = Log().dispatch(MON)
    metrics = compute(
        [
            observe(1, open_one.timed, "implementing"),
            observe(2, open_two.timed, "in_review"),
            observe(3, blocked.timed, "blocked"),
        ],
        NOW,
    ).weeks[0]
    assert (metrics.open, metrics.open_median_age_days) == (2, 2.5)
    assert (metrics.blocked, metrics.blocked_median_age_days) == (1, 3.0)


def test_a_reopened_and_redispatched_issue_has_a_closed_attempt_and_a_new_one() -> None:
    log = Log().dispatch(MON).move(MON + 100, "triage", "closed", "close")
    log.move(MON + 200, "closed", "inbox", "reopen").dispatch(MON + DAY).move(
        MON + 2 * DAY, "verifying_staging", "on_staging", "passed"
    )
    obs = observe(1, log.timed, "on_staging")
    assert [a.outcome for a in obs.attempts] == ["closed", "on_staging"]
    assert digest([log]).weeks[0].lead_median == DAY


def test_a_revert_is_attributed_to_the_week_of_the_original_merge() -> None:
    last_week = MON - WEEK + DAY
    old = Log().landed(last_week - DAY, last_week)
    old.merge(last_week)
    old.revert(NOW - 10)  # reverted this week
    fine = Log().landed(last_week - DAY, last_week)
    fine.merge(last_week)
    twice = Log().landed(MON, MON + 3600)
    twice.merge(MON + 10)
    twice.revert(MON + 20)
    twice.revert(MON + 30)
    result = digest([old, fine, twice])
    previous, current = result.reverts[1], result.reverts[0]
    assert (previous.merged, previous.reverted_issues, previous.rate) == (2, 1, 0.5)
    assert (current.merged, current.reverted_issues, current.revert_events) == (
        1,
        1,
        2,
    )  # once as an issue, twice as events
    assert previous.restated  # the week is not yet 14 days old


def test_human_gate_waits_and_repeat_approvals_come_from_the_log() -> None:
    log = Log().dispatch(MON)
    log.move(MON + 10, "spec", "spec_approval").move(MON + 70, "spec_approval", "implementing")
    log.move(MON + 100, "uat", "human_uat").move(MON + 700, "human_uat", "integrating")
    for _ in range(2):
        log.add(
            MON + 600,
            "evidence",
            {"kind": "approval", "item": "uat", "head_sha": "h", "passed": True},
        )
    metrics = digest([log])
    assert metrics.gate_wait_median == 330 and metrics.gate_wait_p95 == 600
    assert metrics.repeat_approvals_per_issue == 1.0


def test_an_empty_log_gives_no_figures() -> None:
    week = digest([]).weeks[0]
    assert week.lead_median is None and week.first_pass_rate is None
