"""Admin commands on the Steering issue: parsing and the state they leave."""

import pytest

from concert.core.admin import (
    AdminCommand,
    AdminProblem,
    AdminState,
    fold_admin,
    parse_admin,
)
from concert.core.transitions import Status


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pause", AdminCommand("pause")),
        ("pause now", AdminCommand("pause", now=True)),
        ("pause #5", AdminCommand("pause", issue=5)),
        ("resume", AdminCommand("resume")),
        ("resume #5", AdminCommand("resume", issue=5)),
        ("status", AdminCommand("status")),
        ("status #12", AdminCommand("status", issue=12)),
        ("reset #5 spec", AdminCommand("reset", issue=5, state=Status.SPEC)),
        ("  RESET   #5   implementing ", AdminCommand("reset", issue=5, state=Status.IMPLEMENTING)),
    ],
)
def test_commands_parse(text: str, expected: AdminCommand) -> None:
    assert parse_admin(text) == expected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "subcommand"),
        ("explode", "unknown"),
        ("pause #x", "issue"),
        ("pause now #5", "issue"),
        ("reset", "reset needs"),
        ("reset #5", "reset needs"),
        ("reset 5 spec", "reset needs"),
        ("reset #5 nowhere", "state"),
        ("reset #5 on_staging", "reset to"),
        ("reset #5 closed", "reset to"),
        ("status #5 #6", "issue"),
    ],
)
def test_malformed_commands_say_why(text: str, reason: str) -> None:
    problem = parse_admin(text)
    assert isinstance(problem, AdminProblem) and reason in problem.reason


def folded(*events: dict[str, object]) -> AdminState:
    return fold_admin(list(events))


def event(comment_id: int, command: str, **data: object) -> dict[str, object]:
    return {"kind": "admin_command", "data": {"comment_id": comment_id, "command": command, **data}}


def test_pause_and_resume_toggle_the_project() -> None:
    state = folded(event(1, "pause"))
    assert state.paused and state.consumed == frozenset({1})
    assert not folded(event(1, "pause"), event(2, "resume")).paused


def test_pausing_one_issue_does_not_pause_the_project() -> None:
    state = folded(
        event(1, "pause", issue=5), event(2, "pause", issue=6), event(3, "resume", issue=5)
    )
    assert not state.paused and state.paused_issues == frozenset({6})


def test_resuming_the_project_does_not_resume_a_paused_issue() -> None:
    state = folded(event(1, "pause"), event(2, "pause", issue=5), event(3, "resume"))
    assert not state.paused and state.paused_issues == frozenset({5})


def test_a_paused_issue_is_held_when_the_project_is() -> None:
    state = folded(event(1, "pause", issue=5))
    assert state.holds(5) and not state.holds(6)
    assert folded(event(1, "pause")).holds(6)


def test_reset_requests_are_listed_until_applied() -> None:
    state = folded(
        event(1, "reset", issue=5, state="spec"), event(2, "reset", issue=6, state="triage")
    )
    assert state.resets == {5: Status.SPEC, 6: Status.TRIAGE}
    applied = folded(
        event(1, "reset", issue=5, state="spec"),
        {"kind": "admin_reset_applied", "data": {"issue": 5, "comment_id": 1}},
    )
    assert applied.resets == {}


def test_a_later_reset_replaces_an_earlier_one_for_the_same_issue() -> None:
    state = folded(
        event(1, "reset", issue=5, state="spec"), event(2, "reset", issue=5, state="triage")
    )
    assert state.resets == {5: Status.TRIAGE}


def test_other_events_are_ignored() -> None:
    assert folded({"kind": "created", "data": {}}) == AdminState()
