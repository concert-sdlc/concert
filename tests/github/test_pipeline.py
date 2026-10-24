"""Comments -> trusted events -> derive(): the tamper cases the design is built around."""

from typing import Any

from concert.core.derive import derive
from concert.core.events import Event, append_event
from concert.core.transitions import Status
from concert.github.api import Identity
from concert.github.comments import extract_events, render_event_comment
from tests.fakes.github import FakeGitHub

PM = Identity(user_id=900, login="concert-pm[bot]", is_bot=True)
MALLORY = Identity(user_id=666, login="mallory", is_bot=False)


def events_for_triage() -> list[Event]:
    events: list[Event] = []
    for kind, data in (
        ("created", {"issue_type": "code"}),
        ("transition", {"from": "inbox", "to": "triage", "trigger": "dispatch"}),
        ("transition", {"from": "triage", "to": "spec", "trigger": "spec"}),
    ):
        events.append(append_event(events, epoch=1, kind=kind, data=data))
    return events


def derive_from(fake: FakeGitHub, issue: int) -> Any:
    extraction = extract_events(fake.client(PM).list_comments(issue), pm=PM)
    assert extraction.problems == []
    return derive([r.event for r in extraction.records])


def post_each(fake: FakeGitHub, issue: int, events: list[Event]) -> list[int]:
    pm = fake.client(PM)
    return [pm.add_comment(issue, render_event_comment([event])).id for event in events]


def test_trusted_comments_derive_to_the_issue_state() -> None:
    fake = FakeGitHub()
    issue = fake.client(PM).create_issue("a", "").number
    post_each(fake, issue, events_for_triage())
    state = derive_from(fake, issue)
    assert state.status is Status.SPEC
    assert state.blocked_reason is None


def test_a_forged_event_comment_by_someone_else_changes_nothing() -> None:
    fake = FakeGitHub()
    issue = fake.client(PM).create_issue("a", "").number
    events = events_for_triage()
    post_each(fake, issue, events[:2])
    # mallory posts a perfectly formed next event (the chain hash is public)
    fake.client(MALLORY).add_comment(issue, render_event_comment([events[2]]))
    state = derive_from(fake, issue)
    assert state.status is Status.TRIAGE
    assert state.last_seq == 2


def test_an_edited_pm_comment_leaves_a_gap_and_blocks_the_issue() -> None:
    fake = FakeGitHub()
    issue = fake.client(PM).create_issue("a", "").number
    ids = post_each(fake, issue, events_for_triage())
    fake.tick(1)
    fake.client(PM).edit_comment(ids[1], render_event_comment([events_for_triage()[1]]))
    state = derive_from(fake, issue)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None and "seq 2" in state.blocked_reason


def test_a_deleted_pm_comment_leaves_a_gap_and_blocks_the_issue() -> None:
    fake = FakeGitHub()
    issue = fake.client(PM).create_issue("a", "").number
    ids = post_each(fake, issue, events_for_triage())
    fake.client(PM).delete_comment(ids[1])
    state = derive_from(fake, issue)
    assert state.status is Status.BLOCKED
    assert state.blocked_reason is not None and "seq 2" in state.blocked_reason
