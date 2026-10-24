"""Integration queue rules for reverts and hotfixes (design: queue rules, K bound, freeze)."""

from concert.core.queue import (
    QueueEntry,
    bypasses_freeze,
    counts_toward_k,
    exempt_from_head_hold,
    pick_queue_head,
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


# --- choosing the head -----------------------------------------------------------------------


def waiting(
    number: int, kind: IssueType = IssueType.CODE, at: int = 0, merged: int = 0
) -> QueueEntry:
    return QueueEntry(number, kind, dispatched_at=at, merged_at=merged)


def test_nobody_is_chosen_when_nobody_waits() -> None:
    assert pick_queue_head([], busy=False, frozen=False) is None


def test_the_earliest_dispatch_takes_the_head() -> None:
    chosen = pick_queue_head([waiting(2, at=20), waiting(1, at=10)], busy=False, frozen=False)
    assert chosen is not None and chosen.number == 1


def test_a_code_issue_waits_while_another_merge_is_being_integrated_or_verified() -> None:
    assert pick_queue_head([waiting(1)], busy=True, frozen=False) is None


def test_a_code_issue_waits_while_the_queue_is_frozen() -> None:
    assert pick_queue_head([waiting(1)], busy=False, frozen=True) is None


def test_reverts_and_hotfixes_pass_a_busy_or_frozen_queue() -> None:
    for kind in (IssueType.REVERT, IssueType.HOTFIX):
        chosen = pick_queue_head([waiting(1), waiting(2, kind)], busy=True, frozen=True)
        assert chosen is not None and chosen.number == 2


def test_an_eligible_later_entry_is_chosen_when_the_first_is_not() -> None:
    chosen = pick_queue_head(
        [waiting(1, at=1), waiting(2, IssueType.HOTFIX, at=9)], busy=True, frozen=False
    )
    assert chosen is not None and chosen.number == 2
