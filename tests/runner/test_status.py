"""/pm status: the board by state, and one issue's situation, from what the PM has derived."""

from concert.core.dependencies import Dependency
from concert.core.derive import IssueState
from concert.core.transitions import Status
from concert.runner.status import render_board, render_issue

STATES = {
    1: IssueState(status=Status.SPEC_APPROVAL),
    2: IssueState(status=Status.BLOCKED, note="retry budget exceeded: changes_needed"),
    3: IssueState(status=Status.ON_STAGING),
    4: IssueState(status=Status.IMPLEMENTING, dependencies=(Dependency(1, "start"),)),
    5: IssueState(status=Status.SPEC_APPROVAL),
}


def test_the_board_counts_issues_by_state_in_lifecycle_order() -> None:
    text = render_board(STATES, paused=False, paused_issues=frozenset())
    lines = text.splitlines()
    assert "spec_approval: 2 (#1, #5)" in text and "implementing: 1 (#4)" in text
    order = [
        line.split(":")[0].removeprefix("- ")
        for line in lines
        if line.startswith("- ") and ": " in line
    ]
    assert order.index("spec_approval") < order.index("implementing") < order.index("on_staging")
    assert "paused" not in text.lower()


def test_blocked_issues_are_listed_with_why() -> None:
    text = render_board(STATES, paused=False, paused_issues=frozenset())
    assert "#2" in text and "retry budget exceeded: changes_needed" in text


def test_waiting_issues_and_pauses_are_shown() -> None:
    text = render_board(STATES, paused=True, paused_issues=frozenset({4}))
    assert "paused" in text.lower() and "#4" in text
    assert "waiting on #1" in text


def test_an_empty_board_says_so() -> None:
    assert "no issues" in render_board({}, paused=False, paused_issues=frozenset()).lower()


def test_one_issue_shows_its_status_and_waits() -> None:
    text = render_issue(4, STATES[4], paused=False)
    assert "#4" in text and "implementing" in text and "#1" in text


def test_an_unknown_issue_is_reported_as_such() -> None:
    assert "not been seen" in render_issue(99, None, paused=False)
