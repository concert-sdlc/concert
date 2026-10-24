"""Transition table, main path and returns (design: "Transition table")."""

import pytest

from concert.core.transitions import InvalidTransition, Status, Trigger, is_terminal, next_status

S = Status
T = Trigger

MAIN_PATH = [
    (S.INBOX, T.DISPATCH, S.TRIAGE),
    (S.TRIAGE, T.SPEC, S.SPEC),
    (S.TRIAGE, T.DUPLICATE, S.CLOSED_DUPLICATE),
    (S.TRIAGE, T.NEEDS_INPUT, S.NEEDS_INPUT),
    (S.SPEC, T.READY_FOR_APPROVAL, S.SPEC_APPROVAL),
    (S.SPEC, T.NEEDS_INPUT, S.NEEDS_INPUT),
    (S.SPEC_APPROVAL, T.APPROVE_SPEC, S.IMPLEMENTING),
    (S.IMPLEMENTING, T.DONE, S.IN_REVIEW),
    (S.IN_REVIEW, T.CLEAN, S.UAT),
    (S.UAT, T.PASSED, S.HUMAN_UAT),
    (S.HUMAN_UAT, T.APPROVE_UAT, S.INTEGRATING),
    (S.INTEGRATING, T.READY_TO_MERGE, S.VERIFYING_STAGING),
    (S.VERIFYING_STAGING, T.PASSED, S.ON_STAGING),
]

RETURNS = [
    (S.SPEC_APPROVAL, T.REJECT_SPEC, S.SPEC),
    (S.IMPLEMENTING, T.SPEC_DRIFT, S.SPEC_APPROVAL),
    (S.IN_REVIEW, T.CHANGES_NEEDED, S.IMPLEMENTING),
    (S.UAT, T.FAILED, S.IMPLEMENTING),
    (S.HUMAN_UAT, T.REJECT_UAT, S.IMPLEMENTING),
    (S.INTEGRATING, T.REBASED, S.IN_REVIEW),
    (S.VERIFYING_STAGING, T.FAILED_REVERTED, S.IMPLEMENTING),
]


@pytest.mark.parametrize(("status", "trigger", "expected"), MAIN_PATH + RETURNS)
def test_row(status: Status, trigger: Trigger, expected: Status) -> None:
    assert next_status(status, trigger) is expected


def test_unknown_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        next_status(Status.INBOX, Trigger.APPROVE_UAT)


@pytest.mark.parametrize(
    "status",
    [S.TRIAGE, S.SPEC, S.IMPLEMENTING, S.IN_REVIEW, S.UAT, S.VERIFYING_STAGING],
)
def test_any_agent_state_goes_to_blocked_when_a_budget_is_exceeded(status: Status) -> None:
    assert next_status(status, Trigger.BUDGET_EXCEEDED) is S.BLOCKED


@pytest.mark.parametrize("status", [S.ON_STAGING, S.CLOSED_DUPLICATE, S.CLOSED])
def test_terminal_states_have_no_outgoing_transition(status: Status) -> None:
    assert is_terminal(status)
    with pytest.raises(InvalidTransition):
        next_status(status, Trigger.DISPATCH)


def test_gates_and_agent_states_are_not_terminal() -> None:
    assert not is_terminal(Status.HUMAN_UAT)
    assert not is_terminal(Status.BLOCKED)


@pytest.mark.parametrize("status", [S.SPEC_APPROVAL, S.HUMAN_UAT])
def test_a_rejection_at_a_gate_that_exceeds_the_budget_goes_to_blocked(status: Status) -> None:
    assert next_status(status, Trigger.BUDGET_EXCEEDED) is S.BLOCKED


@pytest.mark.parametrize(
    "status",
    [
        S.TRIAGE,
        S.SPEC,
        S.SPEC_APPROVAL,
        S.IMPLEMENTING,
        S.IN_REVIEW,
        S.UAT,
        S.HUMAN_UAT,
        S.INTEGRATING,
    ],
)
def test_a_failed_check_blocks_the_issue_from_any_working_state(status: Status) -> None:
    assert next_status(status, Trigger.CHECK_FAILED) is S.BLOCKED


def test_a_failed_check_has_no_effect_on_an_issue_that_is_not_started_or_finished() -> None:
    for status in (S.INBOX, S.ON_STAGING, S.CLOSED):
        with pytest.raises(InvalidTransition):
            next_status(status, Trigger.CHECK_FAILED)
