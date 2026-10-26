"""Which finished check run speaks for its check: the newest one that tested something."""

from collections.abc import Iterable
from typing import Protocol


class Finished(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def conclusion(self) -> str | None: ...
    @property
    def completed_at(self) -> int | None: ...


def is_verdict(conclusion: str | None) -> bool:
    """Not finished is no result; a skipped run tested nothing (a draft pull request skips CI)."""
    return conclusion is not None and conclusion != "skipped"


def newest_verdicts[R: Finished](runs: Iterable[R]) -> dict[str, R]:
    """The latest verdict per check name. GitHub lists runs newest first, so order is not
    trusted: completed_at decides, and a tie goes to the later entry."""
    latest: dict[str, R] = {}
    for found in runs:
        if not is_verdict(found.conclusion):
            continue
        previous = latest.get(found.name)
        if previous is None or (found.completed_at or 0) >= (previous.completed_at or 0):
            latest[found.name] = found
    return latest
