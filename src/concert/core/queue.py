"""Integration queue rules for reverts and hotfixes.

Pure ordering and exemption rules; the merge itself stays in the runner.
"""

from dataclasses import dataclass

from concert.core.transitions import IssueType


@dataclass(frozen=True)
class QueueEntry:
    number: int
    issue_type: IssueType
    dispatched_at: int  # epoch seconds of the issue's (latest) /dispatch
    merged_at: int = 0  # for a revert: when the work it reverts was merged


def queue_order(waiting: list[QueueEntry]) -> list[QueueEntry]:
    """Reverts first (newest merge first), then hotfixes, then normal issues; the last two
    in dispatch order. A revert restores a known-good staging before a fix lands on it."""

    def key(entry: QueueEntry) -> tuple[int, int]:
        if entry.issue_type is IssueType.REVERT:
            return (0, -entry.merged_at)
        if entry.issue_type is IssueType.HOTFIX:
            return (1, entry.dispatched_at)
        return (2, entry.dispatched_at)

    return sorted(waiting, key=key)


def bypasses_freeze(issue_type: IssueType) -> bool:
    return issue_type in (IssueType.REVERT, IssueType.HOTFIX)


def exempt_from_head_hold(issue_type: IssueType) -> bool:
    """The displaced head re-proves against the new staging head."""
    return issue_type in (IssueType.REVERT, IssueType.HOTFIX)


def counts_toward_k(issue_type: IssueType) -> bool:
    """Revert and hotfix merges do not count toward the K = 2 staging-rebase bound."""
    return issue_type is IssueType.CODE
