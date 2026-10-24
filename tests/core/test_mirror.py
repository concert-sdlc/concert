"""The append-only mirror of the event log (design: "Deletion detection", "Crash-safe publication")."""

from collections.abc import Mapping
from typing import Any

import pytest

from concert.core.events import Event, append_event, event_hash, parse_event
from concert.core.mirror import (
    MirrorError,
    check_mirror,
    event_line,
    parse_mirror,
    published_line,
)


def chain(n: int) -> list[Event]:
    events: list[Event] = []
    for index in range(n):
        kind = "created" if index == 0 else "counter_reset"
        data: Mapping[str, Any] = (
            {"issue_type": "code"} if index == 0 else {"state": "implementing"}
        )
        events.append(append_event(events, epoch=1, kind=kind, data=data))
    return events


def lines_for(events: list[Event], published: dict[int, int] | None = None) -> list[dict[str, Any]]:
    lines = [event_line(e) for e in events]
    for seq, comment_id in (published or {}).items():
        lines.append(published_line(seq, comment_id))
    return lines


def records(events: list[Event], ids: dict[int, int]) -> list[tuple[dict[str, Any], int]]:
    return [(e.to_dict(), ids[e.seq]) for e in events if e.seq in ids]


# --- parsing -------------------------------------------------------------------------------------


def test_event_lines_and_published_updates_fold_into_entries() -> None:
    events = chain(2)
    entries = parse_mirror(lines_for(events, {1: 10}))
    assert [(e.seq, e.comment_id, e.republished) for e in entries] == [
        (1, 10, False),
        (2, None, False),
    ]
    assert entries[0].event == events[0].to_dict()


def test_a_republished_update_is_remembered() -> None:
    entries = parse_mirror([*lines_for(chain(1)), published_line(1, 7, republished=True)])
    assert entries[0].republished is True


@pytest.mark.parametrize(
    "bad",
    [
        [{"line": "mystery"}],
        [{"line": "published", "seq": 5, "comment_id": 1, "republished": False}],  # unknown seq
    ],
)
def test_malformed_mirror_lines_are_errors(bad: list[dict[str, Any]]) -> None:
    with pytest.raises(MirrorError):
        parse_mirror(bad)


def test_a_duplicate_event_line_is_an_error() -> None:
    events = chain(1)
    with pytest.raises(MirrorError):
        parse_mirror([event_line(events[0]), event_line(events[0])])


# --- comparing the mirror with the comments ---------------------------------------------------------


def test_matching_mirror_and_comments_are_trusted() -> None:
    events = chain(3)
    entries = parse_mirror(lines_for(events, {1: 10, 2: 10, 3: 11}))
    verdict = check_mirror(entries, records(events, {1: 10, 2: 10, 3: 11}))
    assert verdict.blocked_reason is None
    assert verdict.events == [e.to_dict() for e in events]
    assert verdict.republish == [] and verdict.record_ids == []


def test_an_empty_mirror_is_fine() -> None:
    verdict = check_mirror([], [])
    assert verdict.blocked_reason is None and verdict.events == []


def test_a_crash_after_the_comment_but_before_recording_its_id_records_the_id() -> None:
    events = chain(2)
    entries = parse_mirror(lines_for(events, {1: 10}))  # seq 2 posted at 11, update never written
    verdict = check_mirror(entries, records(events, {1: 10, 2: 11}))
    assert verdict.blocked_reason is None
    assert verdict.record_ids == [(2, 11)]
    assert verdict.republish == []


def test_a_crash_after_the_mirror_line_but_before_the_comment_republishes_it() -> None:
    events = chain(2)
    entries = parse_mirror(lines_for(events, {1: 10}))  # seq 2 is pending
    verdict = check_mirror(entries, records(events, {1: 10}))
    assert verdict.blocked_reason is None
    assert verdict.republish == [events[1].to_dict()]
    # the pending event is part of the history even before its comment exists
    assert [e["seq"] for e in verdict.events] == [1, 2]


def test_a_published_comment_that_is_gone_is_a_deletion() -> None:
    events = chain(3)
    entries = parse_mirror(lines_for(events, {1: 10, 2: 10, 3: 11}))
    verdict = check_mirror(entries, records(events, {1: 10, 2: 10}))  # the tail comment vanished
    assert verdict.blocked_reason is not None
    assert "seq 3" in verdict.blocked_reason and "missing" in verdict.blocked_reason


def test_a_comment_that_differs_from_the_mirror_blocks() -> None:
    events = chain(2)
    entries = parse_mirror(lines_for(events, {1: 10, 2: 10}))
    forged = events[1].to_dict() | {"data": {"state": "spec"}}
    verdict = check_mirror(entries, [(events[0].to_dict(), 10), (forged, 10)])
    assert verdict.blocked_reason is not None and "mismatch at seq 2" in verdict.blocked_reason


def test_a_comment_with_no_mirror_line_is_forged_and_ignored() -> None:
    events = chain(2)
    entries = parse_mirror(lines_for(events[:1], {1: 10}))
    verdict = check_mirror(entries, records(events, {1: 10, 2: 11}))
    assert verdict.blocked_reason is None
    assert [e["seq"] for e in verdict.events] == [1]
    assert verdict.forged_seqs == [2]


def test_a_broken_hash_chain_in_the_mirror_blocks() -> None:
    events = chain(3)
    tampered = parse_event(events[1].to_dict() | {"data": {"state": "spec"}})
    entries = parse_mirror([event_line(events[0]), event_line(tampered), event_line(events[2])])
    verdict = check_mirror(entries, [])
    assert verdict.blocked_reason is not None and "hash break" in verdict.blocked_reason
    assert event_hash(tampered) != event_hash(events[1])
