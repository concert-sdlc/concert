"""The external side effect an issue needs next.

Pure. Each effect is announced with an `intent` event before it is performed and closed with an
`outcome` event after, so a crash between the two is resolved on the next poll: a pending intent
is resumed first, and every effect is idempotent by key (the caller looks for the PR before it
opens one, and marking a PR ready twice changes nothing).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from concert.core.derive import IssueState
from concert.core.transitions import IssueType, Status, is_terminal

# States that come before the spec is ready, or have no work: no PR exists or is wanted yet.
_NO_PR_YET = frozenset(
    {Status.INBOX, Status.TRIAGE, Status.SPEC, Status.NEEDS_INPUT, Status.BLOCKED}
)
# From review on, the PR is out of draft.
_READY_STATES = frozenset(
    {
        Status.IN_REVIEW,
        Status.UAT,
        Status.HUMAN_UAT,
        Status.INTEGRATING,
        Status.VERIFYING_STAGING,
    }
)


@dataclass(frozen=True)
class Effect:
    key: str
    action: str
    data: Mapping[str, Any]
    resumed: bool  # true when the intent is already in the log


def plan_effect(
    state: IssueState,
    issue: int,
    base_branch: str,
    gate_ready: bool = False,
    auto_revert: bool = False,
    awaiting_human: bool = False,
) -> Effect | None:
    """`gate_ready`: every merge requirement holds on the current head; `awaiting_human`: only human
    approvals are still missing (the caller evaluates both)."""
    if state.pending_intents:
        pending = state.pending_intents[0]
        return Effect(pending.key, pending.action, pending.data, resumed=True)
    if state.status is Status.CLOSED_DUPLICATE:
        if "close_duplicate" in state.closed_posted:
            return None
        data = {"decisions": list(state.decisions)}
        return Effect("close_duplicate", "close_issue", data, resumed=False)
    if is_terminal(state.status):
        return None
    if (
        state.pr_number is None
        and state.branch_base is not None
        and state.status not in _NO_PR_YET
        and state.issue_type is not IssueType.REVERT
    ):
        data = {
            "head": state.branch or f"pm/{issue}",
            "base": base_branch,
            "title": f"Concert #{issue}",
        }
        return Effect("open_pr", "open_pr", data, resumed=False)
    if state.pr_number is not None and not state.pr_ready and state.status in _READY_STATES:
        return Effect("ready", "mark_ready", {"number": state.pr_number}, resumed=False)
    if state.status is Status.NEEDS_INPUT:
        key = f"question:{len(state.answers)}"
        if key in state.questions_posted:
            return None
        return Effect(key, "post_question", {"doubts": list(state.doubts)}, resumed=False)
    kit = _plan_kit(state, awaiting_human)
    if kit is not None:
        return kit
    if (
        state.status is Status.INTEGRATING
        and gate_ready
        and state.pr_number is not None
        and state.head_sha is not None
        and state.merge_sha is None
        and state.settled_runs  # the integration run reported ready_to_merge
    ):
        data = {"number": state.pr_number, "expected_head": state.head_sha}
        return Effect("merge", "merge", data, resumed=False)
    return _plan_revert(state) if auto_revert else None


def _plan_revert(state: IssueState) -> Effect | None:
    """After a final verification failure: open a revert PR, then merge it once its checks pass."""
    if (
        state.status is not Status.VERIFYING_STAGING
        or not state.verify_failed
        or state.issue_type is not IssueType.CODE
        or state.revert_merge_sha is not None
    ):
        return None
    if state.revert_pr is None:
        if state.pr_number is None:
            return None
        return Effect("revert_pr", "revert_pr", {"number": state.pr_number}, resumed=False)
    if state.revert_ci_passed and state.revert_head is not None:
        data = {
            "number": state.revert_pr,
            "expected_head": state.revert_head,
            "reverts": state.merge_sha,
        }
        return Effect("merge_revert", "merge_revert", data, resumed=False)
    return None


def _plan_kit(state: IssueState, awaiting_human: bool) -> Effect | None:
    """The human gates each get one kit per revision they ask the human to approve."""
    revision: str | None = None
    kind = ""
    if state.status is Status.SPEC_APPROVAL:
        kind, revision = "spec", state.spec_rev
    elif (
        state.status is Status.HUMAN_UAT
        and state.issue_type is not IssueType.REVERT
        and awaiting_human
    ):
        kind, revision = "uat", state.head_sha
    if revision is None:
        return None
    key = f"kit:{kind}:{revision}"
    if key in state.kits_posted:
        return None
    return Effect(key, "post_kit", {"kind": kind, "rev": revision}, resumed=False)
