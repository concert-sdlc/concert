"""The event mirror on the `concert-events` branch, and crash-safe publication.

Each event is published in three steps: (1) push the mirror line, (2) post the comment,
(3) push a mirror update recording the comment id. `reconcile` repairs a crash between any
two steps and blocks on anything it cannot explain (a deleted or edited comment, a
rewritten mirror, a hash break).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from concert.core.events import Event, parse_event
from concert.core.mirror import (
    MirrorError,
    check_mirror,
    event_line,
    parse_mirror,
    published_line,
)
from concert.github.api import Comment, GitHubApi, Identity
from concert.github.comments import extract_events, render_event_comment


class Mirror:
    def __init__(self, github: GitHubApi, author: str, branch: str = "concert-events") -> None:
        self._github = github
        self._author = author
        self._branch = branch

    def path(self, issue: int) -> str:
        return f"events/{issue}.jsonl"

    def read(self, issue: int) -> list[dict[str, Any]]:
        text = self._github.read_file(self._branch, self.path(issue))
        if not text:
            return []
        lines: list[dict[str, Any]] = []
        for number, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                value: Any = json.loads(raw)
            except json.JSONDecodeError as error:
                raise MirrorError(f"mirror line {number} is not JSON: {error}") from None
            if not isinstance(value, dict):
                raise MirrorError(f"mirror line {number} is not an object")
            lines.append(value)  # type: ignore[arg-type]
        return lines

    def append(self, issue: int, lines: Sequence[dict[str, Any]]) -> None:
        existing = self._github.read_file(self._branch, self.path(issue)) or ""
        encoded = "".join(
            json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            for line in lines
        )
        self._github.commit(
            self._branch,
            f"mirror issue {issue}",
            {self.path(issue): existing + encoded},
            author=self._author,
            committer=self._author,
        )


def publish_events(github: GitHubApi, mirror: Mirror, issue: int, events: Sequence[Event]) -> int:
    """Mirror first, then the comment, then the comment id. Returns the comment id."""
    mirror.append(issue, [event_line(event) for event in events])
    comment = github.add_comment(issue, render_event_comment(events))
    mirror.append(issue, [published_line(event.seq, comment.id) for event in events])
    return comment.id


@dataclass(frozen=True)
class Reconciled:
    events: list[dict[str, Any]]  # the trusted history; empty when blocked
    blocked_reason: str | None = None
    republished: int = 0
    forged_seqs: list[int] = field(default_factory=list[int])
    problems: list[str] = field(default_factory=list[str])  # unreadable trusted comments


def reconcile(
    github: GitHubApi,
    mirror: Mirror,
    issue: int,
    comments: Sequence[Comment],
    pm: Identity,
) -> Reconciled:
    extraction = extract_events(comments, pm)
    try:
        entries = parse_mirror(mirror.read(issue))
    except MirrorError as error:
        return Reconciled(events=[], blocked_reason=str(error))
    verdict = check_mirror(entries, [(r.event, r.comment_id) for r in extraction.records])
    if verdict.blocked_reason is not None:
        return Reconciled(
            events=[], blocked_reason=verdict.blocked_reason, problems=extraction.problems
        )

    republished = 0
    if verdict.republish:
        events = [parse_event(raw) for raw in verdict.republish]
        comment = github.add_comment(issue, render_event_comment(events))
        mirror.append(issue, [published_line(e.seq, comment.id, republished=True) for e in events])
        republished = len(events)
    if verdict.record_ids:
        mirror.append(issue, [published_line(seq, cid) for seq, cid in verdict.record_ids])
    return Reconciled(
        events=verdict.events,
        republished=republished,
        forged_seqs=verdict.forged_seqs,
        problems=extraction.problems,
    )
