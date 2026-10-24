"""Crash injection: the runner dies between publication steps; the next poll recovers."""

from typing import Any

import pytest

from concert.core.transitions import Status
from concert.github.mirror import Mirror
from concert.runner.poll import poll_issue
from tests.runner.helpers import ALICE, CONFIG, PM, World


class Crash(Exception):
    pass


class Flaky:
    """Wraps a GitHub client and raises once, on the n-th call of one method."""

    def __init__(self, inner: Any, method: str, nth: int) -> None:
        self._inner = inner
        self._method = method
        self._left = nth

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name != self._method:
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            self._left -= 1
            if self._left == 0:
                raise Crash(f"{name} #{self._method} died")
            return target(*args, **kwargs)

        return call


def event_comments(world: World, issue: int) -> int:
    return len([c for c in world.pm.list_comments(issue) if c.author.user_id == PM.user_id])


def dispatched(world: World) -> int:
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    return issue


@pytest.mark.parametrize(
    ("method", "nth", "label"),
    [
        ("commit", 1, "before the mirror line is pushed"),
        ("add_comment", 1, "after the mirror line, before the comment"),
        ("commit", 2, "after the comment, before its id is recorded"),
    ],
)
def test_a_crash_while_publishing_a_transition_recovers_without_loss_or_duplicates(
    method: str, nth: int, label: str
) -> None:
    world = World()
    issue = dispatched(world)
    flaky = Flaky(world.pm, method, nth)
    with pytest.raises(Crash):
        poll_issue(flaky, Mirror(flaky, author=PM.login), issue, CONFIG, world.facts)  # type: ignore[arg-type]
    result = world.poll(issue)  # the runner restarts
    assert result.status is Status.TRIAGE, label
    assert world.pm.get_status(issue) == "triage", label
    assert world.poll(issue).posted == 0, label  # nothing left to do
    assert event_comments(world, issue) == 2, label  # created + exactly one for the dispatch


def test_a_crash_while_adopting_a_new_issue_recovers() -> None:
    world = World()
    issue = world.new_issue()
    flaky = Flaky(world.pm, "add_comment", 1)
    with pytest.raises(Crash):
        poll_issue(flaky, Mirror(flaky, author=PM.login), issue, CONFIG, world.facts)  # type: ignore[arg-type]
    assert world.poll(issue).status is Status.INBOX
    assert event_comments(world, issue) == 1
