"""Revert and hotfix issue types (design: transition table rows for revert/hotfix)."""

import pytest

from concert.core.transitions import (
    InvalidTransition,
    IssueType,
    RevertCiAction,
    Status,
    Trigger,
    initial_status,
    next_status,
    revert_ci_action,
)

S = Status
T = Trigger
REVERT = IssueType.REVERT
HOTFIX = IssueType.HOTFIX


def test_a_revert_starts_in_the_pm_only_implementing_state() -> None:
    assert initial_status(REVERT) is S.IMPLEMENTING
    assert initial_status(IssueType.CODE) is S.INBOX
    assert initial_status(HOTFIX) is S.INBOX


@pytest.mark.parametrize(
    ("status", "trigger", "expected"),
    [
        (S.IMPLEMENTING, T.CI_GREEN, S.HUMAN_UAT),
        (S.IMPLEMENTING, T.CI_FAILED_TWICE, S.BLOCKED),
        (S.HUMAN_UAT, T.APPROVE_REVERT, S.INTEGRATING),
        (S.HUMAN_UAT, T.REJECT_REVERT, S.BLOCKED),
        (S.INTEGRATING, T.READY_TO_MERGE, S.VERIFYING_STAGING),
        (S.INTEGRATING, T.REBASED, S.HUMAN_UAT),
        (S.VERIFYING_STAGING, T.PASSED, S.ON_STAGING),
        (S.VERIFYING_STAGING, T.VERIFY_FAILED, S.BLOCKED),
    ],
)
def test_revert_rows(status: Status, trigger: Trigger, expected: Status) -> None:
    assert next_status(status, trigger, REVERT) is expected


@pytest.mark.parametrize(
    ("status", "trigger"),
    [
        (S.HUMAN_UAT, T.REJECT_UAT),  # a revert has no agent to send back to
        (S.HUMAN_UAT, T.APPROVE_UAT),  # reverts use /approve revert
        (S.VERIFYING_STAGING, T.FAILED_REVERTED),  # auto_revert never applies to a revert
        (S.IMPLEMENTING, T.DONE),  # no agent run in implementing
        (S.INBOX, T.DISPATCH),
    ],
)
def test_revert_rejects_code_issue_rows(status: Status, trigger: Trigger) -> None:
    with pytest.raises(InvalidTransition):
        next_status(status, trigger, REVERT)


def test_revert_ci_is_redispatched_once_then_blocks() -> None:
    assert revert_ci_action(failures=1) is RevertCiAction.REDISPATCH
    assert revert_ci_action(failures=2) is RevertCiAction.BLOCK


def test_a_hotfix_follows_the_full_code_lifecycle() -> None:
    status = initial_status(HOTFIX)
    for trigger in (
        T.DISPATCH,
        T.SPEC,
        T.READY_FOR_APPROVAL,
        T.APPROVE_SPEC,
        T.DONE,
        T.CLEAN,
        T.PASSED,
        T.APPROVE_UAT,
        T.READY_TO_MERGE,
        T.PASSED,
    ):
        status = next_status(status, trigger, HOTFIX)
    assert status is S.ON_STAGING


def test_a_hotfix_whose_verification_fails_is_blocked_and_never_auto_reverted() -> None:
    assert next_status(S.VERIFYING_STAGING, T.VERIFY_FAILED, HOTFIX) is S.BLOCKED
    with pytest.raises(InvalidTransition):
        next_status(S.VERIFYING_STAGING, T.FAILED_REVERTED, HOTFIX)


def test_a_code_issue_freezes_to_blocked_when_the_auto_revert_cannot_complete() -> None:
    assert next_status(S.VERIFYING_STAGING, T.VERIFY_FAILED) is S.BLOCKED
    assert next_status(S.VERIFYING_STAGING, T.FAILED_REVERTED) is S.IMPLEMENTING
