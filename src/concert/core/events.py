"""Versioned pm-events with a hash chain.

Every event carries `schema: <n>`. There is one parser per schema version; writers always
emit the newest. An unknown newer version raises `UnsupportedSchema` so the caller can send
the issue to `blocked` ("runner too old") instead of misparsing it.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

GENESIS = ""  # `prev` of the first event of an issue
SCHEMA_LATEST = 1


class MalformedEvent(Exception):
    """The event is missing a field or a field has the wrong type."""


class UnsupportedSchema(Exception):
    """The event was written by a newer (or unknown) schema version."""

    def __init__(self, version: int) -> None:
        super().__init__(f"unsupported event schema {version}: runner too old")
        self.version = version


class ChainError(Exception):
    """A missing seq, a hash break or a mismatch in the event chain."""


@dataclass(frozen=True)
class Event:
    schema: int
    seq: int
    prev: str
    epoch: int
    kind: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seq": self.seq,
            "prev": self.prev,
            "epoch": self.epoch,
            "kind": self.kind,
            "data": dict(self.data),
        }


def _require(raw: Mapping[str, Any], key: str, kind: type) -> Any:
    if key not in raw:
        raise MalformedEvent(f"missing field {key!r}")
    value = raw[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise MalformedEvent(f"field {key!r} must be {kind.__name__}")
    return value


def _parse_v1(raw: Mapping[str, Any]) -> Event:
    return Event(
        schema=1,
        seq=_require(raw, "seq", int),
        prev=_require(raw, "prev", str),
        epoch=_require(raw, "epoch", int),
        kind=_require(raw, "kind", str),
        data=_require(raw, "data", dict),
    )


# One parser per supported schema version.
_PARSERS: dict[int, Callable[[Mapping[str, Any]], Event]] = {1: _parse_v1}


def parse_event(raw: Mapping[str, Any]) -> Event:
    version = _require(raw, "schema", int)
    if version < 1:
        raise MalformedEvent(f"field 'schema' must be >= 1, got {version}")
    parser = _PARSERS.get(version)
    if parser is None:
        raise UnsupportedSchema(version)
    return parser(raw)


def canonical_json(raw: Mapping[str, Any]) -> str:
    """Stable encoding for hashing: sorted keys, no whitespace, UTF-8 characters kept."""
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(event: Event) -> str:
    return hashlib.sha256(canonical_json(event.to_dict()).encode("utf-8")).hexdigest()


def append_event(
    events: Sequence[Event], *, epoch: int, kind: str, data: Mapping[str, Any]
) -> Event:
    """The next event of a chain: seq = len + 1, prev = hash of the last event."""
    prev = event_hash(events[-1]) if events else GENESIS
    return Event(
        schema=SCHEMA_LATEST, seq=len(events) + 1, prev=prev, epoch=epoch, kind=kind, data=data
    )


def verify_chain(events: Sequence[Event]) -> None:
    """seq must run 1..n with no gap, the first `prev` is GENESIS, and each `prev` is the
    hash of the event before it."""
    previous: Event | None = None
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            raise ChainError(f"gap in the event chain: expected seq {index}, found seq {event.seq}")
        expected = GENESIS if previous is None else event_hash(previous)
        if event.prev != expected:
            raise ChainError(
                f"hash break at seq {event.seq}: prev does not match the event before it"
            )
        previous = event
