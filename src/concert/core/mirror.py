"""The append-only mirror of an issue's event log (`events/<issue>.jsonl` on `concert-events`).

Pure. The mirror is pushed first, then the comment is posted, then a `published` update
records the comment id, so a crash at any step is recoverable and nothing is lost. Only the
PM App can push the mirror branch, so the mirror is what makes a deleted or edited event
comment (including the tail of the log) detectable. A comment with no mirror line cannot
occur by construction and is treated as forged and ignored.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from concert.core.events import (
    SCHEMA_LATEST,
    ChainError,
    Event,
    MalformedEvent,
    UnsupportedSchema,
    parse_event,
    verify_chain,
)


class MirrorError(Exception):
    """The mirror file itself is malformed."""


@dataclass(frozen=True)
class MirrorEntry:
    seq: int
    event: dict[str, Any]
    comment_id: int | None
    republished: bool


def event_line(event: Event) -> dict[str, Any]:
    """Step 1 of publication: the event, recorded before its comment exists."""
    return {"line": "event", "schema": SCHEMA_LATEST, "seq": event.seq, "event": event.to_dict()}


def published_line(seq: int, comment_id: int, republished: bool = False) -> dict[str, Any]:
    """Step 3 of publication: which comment carries the event."""
    return {
        "line": "published",
        "schema": SCHEMA_LATEST,
        "seq": seq,
        "comment_id": comment_id,
        "republished": republished,
    }


def parse_mirror(lines: Sequence[Mapping[str, Any]]) -> list[MirrorEntry]:
    events: dict[int, dict[str, Any]] = {}
    ids: dict[int, int] = {}
    republished: dict[int, bool] = {}
    for line in lines:
        kind = line.get("line")
        seq = line.get("seq")
        if not isinstance(seq, int):
            raise MirrorError(f"mirror line without a seq: {dict(line)}")
        if kind == "event":
            raw_event: Any = line.get("event")
            if not isinstance(raw_event, dict):
                raise MirrorError(f"mirror event line for seq {seq} is malformed")
            event = cast("dict[str, Any]", raw_event)
            if event.get("seq") != seq:
                raise MirrorError(f"mirror event line for seq {seq} is malformed")
            if seq in events:
                raise MirrorError(f"mirror has two event lines for seq {seq}")
            events[seq] = event
        elif kind == "published":
            comment_id = line.get("comment_id")
            if seq not in events or not isinstance(comment_id, int):
                raise MirrorError(f"mirror update for unknown seq {seq}")
            ids[seq] = comment_id
            republished[seq] = bool(line.get("republished", False))
        else:
            raise MirrorError(f"unknown mirror line {kind!r}")
    return [
        MirrorEntry(seq, events[seq], ids.get(seq), republished.get(seq, False))
        for seq in sorted(events)
    ]


@dataclass(frozen=True)
class MirrorVerdict:
    events: list[dict[str, Any]]  # the trusted history, in order (empty when blocked)
    blocked_reason: str | None = None
    record_ids: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    republish: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    forged_seqs: list[int] = field(default_factory=list[int])


def check_mirror(
    entries: Sequence[MirrorEntry], comment_events: Sequence[tuple[dict[str, Any], int]]
) -> MirrorVerdict:
    """`comment_events` are the events found in trusted PM comments, with their comment ids."""
    try:
        chain = [parse_event(entry.event) for entry in entries]
        verify_chain(chain)
    except (UnsupportedSchema, MalformedEvent, ChainError) as error:
        return MirrorVerdict(events=[], blocked_reason=str(error))

    by_seq: dict[int, list[tuple[dict[str, Any], int]]] = {}
    for event, comment_id in comment_events:
        seq = event.get("seq")
        if isinstance(seq, int):
            by_seq.setdefault(seq, []).append((event, comment_id))

    record_ids: list[tuple[int, int]] = []
    republish: list[dict[str, Any]] = []
    for entry in entries:
        found = by_seq.get(entry.seq, [])
        if any(event != entry.event for event, _ in found):
            return MirrorVerdict(events=[], blocked_reason=f"mirror mismatch at seq {entry.seq}")
        if found:
            if entry.comment_id is None:
                record_ids.append((entry.seq, found[0][1]))
        elif entry.comment_id is not None:
            return MirrorVerdict(
                events=[],
                blocked_reason=f"event comment for seq {entry.seq} is missing (deleted or edited)",
            )
        else:
            republish.append(entry.event)

    known = {entry.seq for entry in entries}
    return MirrorVerdict(
        events=[entry.event for entry in entries],
        record_ids=record_ids,
        republish=republish,
        forged_seqs=sorted(set(by_seq) - known),
    )
