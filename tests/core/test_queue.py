"""Integration queue rules for reverts and hotfixes (design: queue rules, K bound, freeze)."""

from concert.core.queue import (
    QueueEntry,
    bypasses_freeze,
    counts_toward_k,
    exempt_from_head_hold,
    queue_order,
)
from concert.core.transitions import IssueType

CODE, REVERT, HOTFIX = IssueType.CODE, IssueType.REVERT, IssueType.HOTFIX


def entry(number: int, issue_type: IssueType, dispatched: int, merged: int = 0) -> QueueEntry:
    return QueueEntry(
        number=number, issue_type=issue_type, dispatched_at=dispatched, merged_at=merged
    )


def numbers(entries: list[QueueEntry]) -> list[int]:
    return [e.number for e in entries]


def test_a_revert_goes_before_a_hotfix_before_normal_issues() -> None:
    waiting = [entry(1, CODE, 1), entry(2, HOTFIX, 2), entry(3, REVERT, 3)]
    assert numbers(queue_order(waiting)) == [3, 2, 1]


def test_hotfixes_go_in_dispatch_order() -> None:
    waiting = [entry(1, HOTFIX, 5), entry(2, HOTFIX, 2)]
    assert numbers(queue_order(waiting)) == [2, 1]


def test_reverts_go_newest_merge_first() -> None:
    waiting = [entry(1, REVERT, 1, merged=10), entry(2, REVERT, 2, merged=30)]
    assert numbers(queue_order(waiting)) == [2, 1]


def test_normal_issues_go_in_dispatch_order() -> None:
    waiting = [entry(1, CODE, 9), entry(2, CODE, 4)]
    assert numbers(queue_order(waiting)) == [2, 1]


def test_reverts_and_hotfixes_bypass_a_freeze_and_the_head_hold() -> None:
    for issue_type in (REVERT, HOTFIX):
        assert bypasses_freeze(issue_type)
        assert exempt_from_head_hold(issue_type)
    assert not bypasses_freeze(CODE)
    assert not exempt_from_head_hold(CODE)


def test_revert_and_hotfix_merges_do_not_count_toward_k() -> None:
    assert not counts_toward_k(REVERT)
    assert not counts_toward_k(HOTFIX)
    assert counts_toward_k(CODE)
