"""Mirror-first publication and crash recovery against the fake GitHub."""

from typing import Any

import pytest

from concert.core.events import Event, append_event, parse_event
from concert.core.mirror import event_line, parse_mirror
from concert.github.api import Identity
from concert.github.comments import render_event_comment
from concert.github.mirror import Mirror, publish_events, reconcile
from tests.fakes.github import FakeGitHub

PM = Identity(user_id=900, login="concert-pm[bot]", is_bot=True)
MALLORY = Identity(user_id=666, login="mallory", is_bot=False)


class Setup:
    def __init__(self) -> None:
        self.fake = FakeGitHub()
        self.fake.seed_branch("concert-events", {})
        self.pm = self.fake.client(PM)
        self.mirror = Mirror(self.pm, author=PM.login)
        self.issue = self.pm.create_issue("a", "").number
        self.events: list[Event] = []

    def next_event(self, kind: str = "created", data: dict[str, Any] | None = None) -> Event:
        event = append_event(self.events, epoch=1, kind=kind, data=data or {"issue_type": "code"})
        self.events.append(event)
        return event

    def reconcile(self):
        return reconcile(self.pm, self.mirror, self.issue, self.pm.list_comments(self.issue), PM)

    def pm_comments(self) -> int:
        return len([c for c in self.pm.list_comments(self.issue) if c.author.user_id == PM.user_id])


@pytest.fixture
def s() -> Setup:
    return Setup()


def test_a_missing_mirror_file_reads_as_empty(s: Setup) -> None:
    assert s.mirror.read(s.issue) == []
    assert s.reconcile().events == []


def test_publishing_mirrors_first_then_posts_one_comment_then_records_its_id(s: Setup) -> None:
    first, second = s.next_event(), s.next_event("counter_reset", {"state": "implementing"})
    comment_id = publish_events(s.pm, s.mirror, s.issue, [first, second])
    entries = parse_mirror(s.mirror.read(s.issue))
    assert [(e.seq, e.comment_id) for e in entries] == [(1, comment_id), (2, comment_id)]
    assert s.pm_comments() == 1  # coalesced
    result = s.reconcile()
    assert result.blocked_reason is None
    assert [e["seq"] for e in result.events] == [1, 2]


def test_a_healthy_log_reconciles_without_any_writes(s: Setup) -> None:
    publish_events(s.pm, s.mirror, s.issue, [s.next_event()])
    before = (s.pm_comments(), s.pm.get_ref("concert-events"))
    s.reconcile()
    assert (s.pm_comments(), s.pm.get_ref("concert-events")) == before


def test_a_crash_after_step_one_republishes_the_comment_once(s: Setup) -> None:
    event = s.next_event()
    s.mirror.append(s.issue, [event_line(event)])  # step 1 only; the runner dies
    result = s.reconcile()
    assert result.blocked_reason is None and result.republished == 1
    assert s.pm_comments() == 1
    entry = parse_mirror(s.mirror.read(s.issue))[0]
    assert entry.republished is True and entry.comment_id is not None
    again = s.reconcile()  # recovery is idempotent
    assert again.republished == 0 and s.pm_comments() == 1


def test_a_crash_after_step_two_records_the_comment_id(s: Setup) -> None:
    event = s.next_event()
    s.mirror.append(s.issue, [event_line(event)])
    comment = s.pm.add_comment(s.issue, render_event_comment([event]))  # step 2; then it dies
    result = s.reconcile()
    assert result.blocked_reason is None and result.republished == 0
    assert parse_mirror(s.mirror.read(s.issue))[0].comment_id == comment.id
    assert s.pm_comments() == 1


def test_a_deleted_event_comment_blocks(s: Setup) -> None:
    comment_id = publish_events(s.pm, s.mirror, s.issue, [s.next_event()])
    s.pm.delete_comment(comment_id)
    result = s.reconcile()
    assert result.blocked_reason is not None and "seq 1" in result.blocked_reason


def test_an_edited_event_comment_blocks(s: Setup) -> None:
    event = s.next_event()
    comment_id = publish_events(s.pm, s.mirror, s.issue, [event])
    s.fake.tick(2)
    s.pm.edit_comment(comment_id, render_event_comment([event]))
    assert s.reconcile().blocked_reason is not None


def test_a_forged_event_comment_by_someone_else_is_ignored(s: Setup) -> None:
    publish_events(s.pm, s.mirror, s.issue, [s.next_event()])
    forged = append_event(s.events, epoch=1, kind="counter_reset", data={"state": "spec"})
    s.fake.client(MALLORY).add_comment(s.issue, render_event_comment([forged]))
    result = s.reconcile()
    assert result.blocked_reason is None
    assert [e["seq"] for e in result.events] == [1]


def test_an_event_comment_with_no_mirror_line_is_forged_even_from_the_pm_account(s: Setup) -> None:
    publish_events(s.pm, s.mirror, s.issue, [s.next_event()])
    rogue = append_event(s.events, epoch=1, kind="counter_reset", data={"state": "spec"})
    s.pm.add_comment(
        s.issue, render_event_comment([rogue])
    )  # a leaked token, posted without the mirror
    result = s.reconcile()
    assert result.blocked_reason is None
    assert [e["seq"] for e in result.events] == [1]
    assert result.forged_seqs == [2]


def test_a_rewritten_mirror_line_blocks(s: Setup) -> None:
    first = s.next_event()
    publish_events(s.pm, s.mirror, s.issue, [first])
    second = s.next_event("counter_reset", {"state": "implementing"})
    publish_events(s.pm, s.mirror, s.issue, [second])
    text = s.pm.read_file("concert-events", s.mirror.path(s.issue))
    assert text is not None
    tampered = text.replace('"state":"implementing"', '"state":"spec"')
    s.pm.commit(
        "concert-events", "rewrite", {s.mirror.path(s.issue): tampered}, author="x", committer="x"
    )
    assert s.reconcile().blocked_reason is not None


def test_the_mirror_events_are_the_events_that_were_published(s: Setup) -> None:
    events = [s.next_event(), s.next_event("counter_reset", {"state": "implementing"})]
    publish_events(s.pm, s.mirror, s.issue, events)
    assert [parse_event(e) for e in s.reconcile().events] == events
