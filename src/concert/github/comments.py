"""Rendering and extracting Concert's structured comments.

Trust is decided here, from facts GitHub enforces: authorship is the immutable user id, and
an edit is visible (`edited_at`). A pm-event counts only if the PM App wrote it and it was
never edited. A handoff's author is resolved by user id against the registered runners, never
by the name the block claims. YAML is loaded with `safe_load` only.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import yaml

from concert.core.commands import Actor, Command, CommentFacts, Role, parse_command
from concert.core.events import Event, MalformedEvent, UnsupportedSchema
from concert.core.handoff import Assignment, BlockFacts, Handoff, parse_handoff
from concert.github.api import Comment, Identity

_FENCE = re.compile(r"```(pm-event|pm-handoff)[ \t]*\n(.*?)\n```", re.DOTALL)


def _blocks(body: str, tag: str) -> list[str]:
    return [match.group(2) for match in _FENCE.finditer(body) if match.group(1) == tag]


def _load(block: str) -> dict[str, Any] | None:
    try:
        value: Any = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _dump(raw: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(raw), sort_keys=True, default_flow_style=False).rstrip("\n")


# --- pm-events ---------------------------------------------------------------------------------


def render_event_comment(events: Sequence[Event]) -> str:
    """One comment holds every event produced in a poll tick, in order, collapsed."""
    blocks = "\n\n".join(f"```pm-event\n{_dump(event.to_dict())}\n```" for event in events)
    return f"<details><summary>pm-event</summary>\n\n{blocks}\n\n</details>"


@dataclass(frozen=True)
class EventRecord:
    event: dict[str, Any]
    comment_id: int


@dataclass(frozen=True)
class IgnoredComment:
    comment_id: int
    reason: str


@dataclass(frozen=True)
class EventExtraction:
    records: list[EventRecord]
    ignored: list[IgnoredComment]
    problems: list[str]  # trusted comments Concert could not read: the issue goes to blocked


def extract_events(comments: Sequence[Comment], pm: Identity) -> EventExtraction:
    records: list[EventRecord] = []
    ignored: list[IgnoredComment] = []
    problems: list[str] = []
    for comment in sorted(comments, key=lambda c: c.id):
        blocks = _blocks(comment.body, "pm-event")
        if not blocks:
            continue
        if comment.author.user_id != pm.user_id:
            ignored.append(IgnoredComment(comment.id, "not authored by the PM App"))
            continue
        if comment.edited_at is not None:
            ignored.append(IgnoredComment(comment.id, "edited"))
            continue
        for block in blocks:
            event = _load(block)
            if event is None:
                problems.append(f"comment {comment.id}: unreadable pm-event block")
            else:
                records.append(EventRecord(event, comment.id))
    return EventExtraction(records, ignored, problems)


def assignments_from(records: Sequence[EventRecord]) -> dict[str, Assignment]:
    found: dict[str, Assignment] = {}
    for record in records:
        if record.event.get("kind") == "assignment":
            data = cast("dict[str, Any]", record.event.get("data", {}))
            run_id, runner = data.get("run_id"), data.get("runner")
            if isinstance(run_id, str) and isinstance(runner, str):
                found[run_id] = Assignment(run_id=run_id, runner=runner)
    return found


def lease_expiry_comment_ids(records: Sequence[EventRecord]) -> dict[str, int]:
    """run_id -> id of the PM comment that recorded `lease_expired` (comment ids are ordered)."""
    found: dict[str, int] = {}
    for record in records:
        if record.event.get("kind") == "lease_expired":
            data = cast("dict[str, Any]", record.event.get("data", {}))
            run_id = data.get("run_id")
            if isinstance(run_id, str) and run_id not in found:
                found[run_id] = record.comment_id
    return found


# --- pm-handoff --------------------------------------------------------------------------------


def render_handoff_comment(raw: Mapping[str, Any]) -> str:
    return f"```pm-handoff\n{_dump(raw)}\n```\n<!-- pm-run:{raw['run_id']} -->"


@dataclass(frozen=True)
class HandoffComment:
    handoff: Handoff
    block: BlockFacts
    comment_id: int


@dataclass(frozen=True)
class HandoffExtraction:
    items: list[HandoffComment]
    problems: list[str]


def extract_handoffs(
    comments: Sequence[Comment],
    runner_ids: Mapping[str, int],
    lease_expiry: Mapping[str, int],
) -> HandoffExtraction:
    """`runner_ids` maps a runner name to its Worker App bot user id (from process.yaml)."""
    names = {user_id: name for name, user_id in runner_ids.items()}
    items: list[HandoffComment] = []
    problems: list[str] = []
    for comment in sorted(comments, key=lambda c: c.id):
        for block in _blocks(comment.body, "pm-handoff"):
            raw = _load(block)
            if raw is None:
                problems.append(f"comment {comment.id}: unreadable pm-handoff block")
                continue
            try:
                handoff = parse_handoff(raw)
            except (UnsupportedSchema, MalformedEvent) as error:
                problems.append(f"comment {comment.id}: {error}")
                continue
            if f"<!-- pm-run:{handoff.run_id} -->" not in comment.body:
                problems.append(f"comment {comment.id}: marker does not match run {handoff.run_id}")
                continue
            author = names.get(comment.author.user_id, f"user:{comment.author.user_id}")
            expiry = lease_expiry.get(handoff.run_id)
            facts = BlockFacts(
                author=author,
                edited=comment.edited_at is not None,
                posted_before_lease_expiry=expiry is None or comment.id < expiry,
            )
            items.append(HandoffComment(handoff, facts, comment.id))
    return HandoffExtraction(items, problems)


# --- commands ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandComment:
    comment_id: int
    command: Command
    actor: Actor
    facts: CommentFacts


def extract_commands(
    comments: Sequence[Comment], roles: Mapping[int, Role]
) -> list[CommandComment]:
    """`roles` maps GitHub user ids to roles (from `humans.*`); unknown ids have no role."""
    found: list[CommandComment] = []
    for comment in sorted(comments, key=lambda c: c.id):
        command = parse_command(comment.body)
        if command is None:
            continue
        actor = Actor(
            user_id=comment.author.user_id,
            login=comment.author.login,
            is_bot=comment.author.is_bot,
            role=roles.get(comment.author.user_id, Role.NONE),
        )
        found.append(
            CommandComment(
                comment.id, command, actor, CommentFacts(edited=comment.edited_at is not None)
            )
        )
    return found
