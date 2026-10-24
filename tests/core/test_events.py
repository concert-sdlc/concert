"""Versioned pm-events with a hash chain (design: "Event log (the trusted history)")."""

import json
from pathlib import Path

import pytest

from concert.core.events import (
    GENESIS,
    SCHEMA_LATEST,
    ChainError,
    Event,
    MalformedEvent,
    UnsupportedSchema,
    append_event,
    canonical_json,
    event_hash,
    parse_event,
    verify_chain,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "events"


def build_chain() -> list[Event]:
    events: list[Event] = []
    events.append(append_event(events, epoch=1, kind="created", data={"issue_type": "code"}))
    events.append(
        append_event(
            events,
            epoch=1,
            kind="transition",
            data={"from": "inbox", "to": "triage", "trigger": "dispatch"},
        )
    )
    return events


def test_the_writer_emits_the_newest_schema() -> None:
    assert build_chain()[0].schema == SCHEMA_LATEST


def test_an_event_round_trips_through_its_dict_form() -> None:
    event = build_chain()[1]
    assert parse_event(event.to_dict()) == event


def test_an_unknown_newer_schema_is_refused_not_misparsed() -> None:
    raw = build_chain()[0].to_dict() | {"schema": SCHEMA_LATEST + 1}
    with pytest.raises(UnsupportedSchema) as info:
        parse_event(raw)
    assert info.value.version == SCHEMA_LATEST + 1


@pytest.mark.parametrize("schema", [None, "1", 0, -1, True])
def test_a_missing_or_invalid_schema_is_malformed(schema: object) -> None:
    raw = build_chain()[0].to_dict() | {"schema": schema}
    with pytest.raises(MalformedEvent):
        parse_event(raw)


@pytest.mark.parametrize("missing", ["seq", "prev", "epoch", "kind", "data"])
def test_a_missing_field_is_malformed(missing: str) -> None:
    raw = build_chain()[0].to_dict()
    del raw[missing]
    with pytest.raises(MalformedEvent):
        parse_event(raw)


def test_the_hash_does_not_depend_on_key_order() -> None:
    event = build_chain()[0]
    reordered = dict(reversed(list(event.to_dict().items())))
    assert canonical_json(reordered) == canonical_json(event.to_dict())


def test_the_hash_changes_when_any_field_changes() -> None:
    event = build_chain()[1]
    tampered = parse_event(event.to_dict() | {"data": {**event.data, "to": "spec"}})
    assert event_hash(tampered) != event_hash(event)


def test_a_good_chain_verifies() -> None:
    verify_chain(build_chain())
    verify_chain([])


def test_the_first_event_must_have_the_genesis_prev() -> None:
    chain = build_chain()
    assert chain[0].prev == GENESIS
    forged = parse_event(chain[0].to_dict() | {"prev": "deadbeef"})
    with pytest.raises(ChainError):
        verify_chain([forged])


def test_a_missing_event_is_a_gap() -> None:
    chain = build_chain()
    third = append_event(chain, epoch=1, kind="counter_reset", data={"state": "implementing"})
    with pytest.raises(ChainError) as info:
        verify_chain([chain[0], third])  # seq 2 is gone
    assert "seq 2" in str(info.value)


def test_a_rewritten_event_breaks_the_next_hash() -> None:
    chain = build_chain()
    third = append_event(chain, epoch=1, kind="counter_reset", data={"state": "implementing"})
    rewritten = parse_event(
        chain[1].to_dict() | {"data": {"from": "inbox", "to": "spec", "trigger": "dispatch"}}
    )
    with pytest.raises(ChainError) as info:
        verify_chain([chain[0], rewritten, third])
    assert "seq 3" in str(info.value)


def test_the_golden_v1_fixture_still_parses_verifies_and_hashes_the_same() -> None:
    """Hashes in committed history must never change when the code changes."""
    raw = json.loads((FIXTURES / "v1_journey.json").read_text())
    events = [parse_event(item) for item in raw["events"]]
    verify_chain(events)
    assert [event_hash(e) for e in events] == raw["hashes"]
