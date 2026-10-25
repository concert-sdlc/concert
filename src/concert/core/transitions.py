"""Issue Status values and the transition table (main path and process returns).

Pure and IO-free. Rows for the `docs`, `revert`, `hotfix` and `workflow` issue types,
dependency waits and the freeze/queue rules are added with their own tests.
"""

from enum import Enum


class Status(Enum):
    INBOX = "inbox"
    TRIAGE = "triage"
    NEEDS_INPUT = "needs_input"
    SPEC = "spec"
    SPEC_APPROVAL = "spec_approval"
    IMPLEMENTING = "implementing"
    IN_REVIEW = "in_review"
    UAT = "uat"
    HUMAN_UAT = "human_uat"
    INTEGRATING = "integrating"
    VERIFYING_STAGING = "verifying_staging"
    ON_STAGING = "on_staging"
    BLOCKED = "blocked"
    CLOSED_DUPLICATE = "closed_duplicate"
    CLOSED = "closed"


class Trigger(Enum):
    DISPATCH = "dispatch"
    SPEC = "spec"
    DUPLICATE = "duplicate"
    NEEDS_INPUT = "needs_input"
    READY_FOR_APPROVAL = "ready_for_approval"
    APPROVE_SPEC = "approve_spec"
    REJECT_SPEC = "reject_spec"
    DONE = "done"
    SPEC_DRIFT = "spec_drift"
    CLEAN = "clean"
    CHANGES_NEEDED = "changes_needed"
    PASSED = "passed"
    FAILED = "failed"
    APPROVE_UAT = "approve_uat"
    REJECT_UAT = "reject_uat"
    READY_TO_MERGE = "ready_to_merge"
    REBASED = "rebased"
    FAILED_REVERTED = "failed_reverted"  # verification failed twice and the auto-revert succeeded
    VERIFY_FAILED = "verify_failed"  # verification failed and nothing was auto-reverted: freeze
    BUDGET_EXCEEDED = "budget_exceeded"
    CHECK_FAILED = "check_failed"  # a deterministic check failed (foreign commit, unexpected push)
    # revert issues (no agent: the PM opens the revert PR and dispatches CI)
    CI_GREEN = "ci_green"
    CI_FAILED_TWICE = "ci_failed_twice"
    CLOSE = "close"  # the issue was closed on GitHub before its work merged
    REOPEN = "reopen"
    APPROVE_REVERT = "approve_revert"
    REJECT_REVERT = "reject_revert"
    # the PM rebased the branch onto a moved staging head (pre-gate refresh, or at integrating)
    REFRESHED = "refreshed"
    REFRESH_CONFLICT = "refresh_conflict"  # the rebase conflicts: back to implementing, uncharged
    REFRESH_ESCALATED = "refresh_escalated"  # a second conflict: admins are asked


class IssueType(Enum):
    CODE = "code"
    HOTFIX = "hotfix"  # full lifecycle; /dispatch hotfix (admin); never auto-reverted
    REVERT = "revert"  # created by /pm revert; no agent runs
    DOCS = "docs"  # documentation only: no automated UAT; reclassified to code if the diff is not


_S = Status
_T = Trigger

_TERMINAL = frozenset({_S.ON_STAGING, _S.CLOSED_DUPLICATE, _S.CLOSED})

# States where an agent runs; any of them goes to `blocked` when a budget is exceeded.
_AGENT_STATES = frozenset(
    {
        _S.TRIAGE,
        _S.SPEC,
        _S.IMPLEMENTING,
        _S.IN_REVIEW,
        _S.UAT,
        _S.INTEGRATING,
        _S.VERIFYING_STAGING,
    }
)

# Re-entries are charged at the returning transition, so a rejection at a gate can also
# exceed the budget.
_BUDGETED_STATES = _AGENT_STATES | {_S.SPEC_APPROVAL, _S.HUMAN_UAT}

# Closing is allowed until the work has merged (verifying_staging and later are unaffected).
_CLOSABLE = (
    _S.INBOX,
    _S.TRIAGE,
    _S.NEEDS_INPUT,
    _S.SPEC,
    _S.SPEC_APPROVAL,
    _S.IMPLEMENTING,
    _S.IN_REVIEW,
    _S.UAT,
    _S.HUMAN_UAT,
    _S.INTEGRATING,
    _S.BLOCKED,
)
_CLOSE_ROWS: dict[tuple[Status, Trigger], Status] = {
    **{(state, _T.CLOSE): _S.CLOSED for state in _CLOSABLE},
    (_S.CLOSED, _T.REOPEN): _S.INBOX,
}

_CODE_TABLE: dict[tuple[Status, Trigger], Status] = {
    (_S.INBOX, _T.DISPATCH): _S.TRIAGE,
    (_S.TRIAGE, _T.SPEC): _S.SPEC,
    (_S.TRIAGE, _T.DUPLICATE): _S.CLOSED_DUPLICATE,
    (_S.TRIAGE, _T.NEEDS_INPUT): _S.NEEDS_INPUT,
    (_S.SPEC, _T.READY_FOR_APPROVAL): _S.SPEC_APPROVAL,
    (_S.SPEC, _T.NEEDS_INPUT): _S.NEEDS_INPUT,
    (_S.SPEC_APPROVAL, _T.APPROVE_SPEC): _S.IMPLEMENTING,
    (_S.SPEC_APPROVAL, _T.REJECT_SPEC): _S.SPEC,
    (_S.IMPLEMENTING, _T.DONE): _S.IN_REVIEW,
    (_S.IMPLEMENTING, _T.SPEC_DRIFT): _S.SPEC_APPROVAL,
    (_S.IN_REVIEW, _T.CLEAN): _S.UAT,
    (_S.IN_REVIEW, _T.CHANGES_NEEDED): _S.IMPLEMENTING,
    (_S.UAT, _T.PASSED): _S.HUMAN_UAT,
    (_S.UAT, _T.FAILED): _S.IMPLEMENTING,
    (_S.HUMAN_UAT, _T.APPROVE_UAT): _S.INTEGRATING,
    (_S.HUMAN_UAT, _T.REJECT_UAT): _S.IMPLEMENTING,
    (_S.INTEGRATING, _T.READY_TO_MERGE): _S.VERIFYING_STAGING,
    (_S.INTEGRATING, _T.REBASED): _S.IN_REVIEW,
    (_S.UAT, _T.REFRESHED): _S.IN_REVIEW,
    (_S.UAT, _T.REFRESH_CONFLICT): _S.IMPLEMENTING,
    (_S.UAT, _T.REFRESH_ESCALATED): _S.NEEDS_INPUT,
    (_S.INTEGRATING, _T.REFRESHED): _S.IN_REVIEW,
    (_S.INTEGRATING, _T.REFRESH_CONFLICT): _S.IMPLEMENTING,
    (_S.INTEGRATING, _T.REFRESH_ESCALATED): _S.NEEDS_INPUT,
    (_S.VERIFYING_STAGING, _T.PASSED): _S.ON_STAGING,
    (_S.VERIFYING_STAGING, _T.FAILED_REVERTED): _S.IMPLEMENTING,
    (_S.VERIFYING_STAGING, _T.VERIFY_FAILED): _S.BLOCKED,  # freeze fallback
    **{(state, _T.BUDGET_EXCEEDED): _S.BLOCKED for state in _BUDGETED_STATES},
    **{(state, _T.CHECK_FAILED): _S.BLOCKED for state in _BUDGETED_STATES},
    **_CLOSE_ROWS,
}

# A hotfix runs the full lifecycle, but a failed verification of its own merge is never
# auto-reverted: it goes to `blocked` and the queue stays frozen.
_HOTFIX_TABLE: dict[tuple[Status, Trigger], Status] = {
    key: value for key, value in _CODE_TABLE.items() if key[1] is not _T.FAILED_REVERTED
}

# A revert has no agent: `implementing` is a PM-only interim state while CI runs on the
# revert PR. It is exempt from agent re-entries, so there is no BUDGET_EXCEEDED row, and
# it never returns to `implementing` (no REJECT_UAT, no FAILED_REVERTED).
_REVERT_TABLE: dict[tuple[Status, Trigger], Status] = {
    (_S.IMPLEMENTING, _T.CI_GREEN): _S.HUMAN_UAT,
    (_S.IMPLEMENTING, _T.CI_FAILED_TWICE): _S.BLOCKED,
    (_S.HUMAN_UAT, _T.APPROVE_REVERT): _S.INTEGRATING,
    (_S.HUMAN_UAT, _T.REJECT_REVERT): _S.BLOCKED,
    (_S.INTEGRATING, _T.READY_TO_MERGE): _S.VERIFYING_STAGING,
    (_S.INTEGRATING, _T.REBASED): _S.HUMAN_UAT,  # CI re-runs, then the revert kit is re-posted
    (_S.VERIFYING_STAGING, _T.PASSED): _S.ON_STAGING,
    (_S.VERIFYING_STAGING, _T.VERIFY_FAILED): _S.BLOCKED,
    **_CLOSE_ROWS,
}

# A docs issue skips automated UAT: a clean review goes straight to the human gate.
_DOCS_TABLE: dict[tuple[Status, Trigger], Status] = {
    **{
        key: value
        for key, value in _CODE_TABLE.items()
        if key[0] is not _S.UAT and key != (_S.IN_REVIEW, _T.CLEAN)
    },
    (_S.IN_REVIEW, _T.CLEAN): _S.HUMAN_UAT,
}

_TABLES: dict[IssueType, dict[tuple[Status, Trigger], Status]] = {
    IssueType.CODE: _CODE_TABLE,
    IssueType.HOTFIX: _HOTFIX_TABLE,
    IssueType.REVERT: _REVERT_TABLE,
    IssueType.DOCS: _DOCS_TABLE,
}


class RevertCiAction(Enum):
    REDISPATCH = "redispatch"
    BLOCK = "block"


def revert_ci_action(failures: int) -> RevertCiAction:
    """CI on a revert PR is re-dispatched once; a second failure blocks the issue."""
    return RevertCiAction.REDISPATCH if failures < 2 else RevertCiAction.BLOCK


def initial_status(issue_type: IssueType) -> Status:
    """Where a new issue of this type starts: reverts start in the PM-only `implementing`."""
    return Status.IMPLEMENTING if issue_type is IssueType.REVERT else Status.INBOX


class InvalidTransition(Exception):
    def __init__(self, status: Status, trigger: Trigger) -> None:
        super().__init__(f"no transition from {status.value} on {trigger.value}")
        self.status = status
        self.trigger = trigger


def is_terminal(status: Status) -> bool:
    return status in _TERMINAL


def next_status(status: Status, trigger: Trigger, issue_type: IssueType = IssueType.CODE) -> Status:
    try:
        return _TABLES[issue_type][(status, trigger)]
    except KeyError:
        raise InvalidTransition(status, trigger) from None
