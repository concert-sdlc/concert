"""The evidence ledger and the merge gate.

Pure. Every piece of evidence binds to the PR head SHA: any head change makes the old
evidence stale. CI evidence also records the workflow that produced it and the policy it ran
under, so a workflow that is no longer allowed, or a stricter policy, stales it too. An
approval counts only while its author still holds the role that gives it weight.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from concert.core.transitions import IssueType


@dataclass(frozen=True)
class Evidence:
    """One result. `kind` is ci, review, uat, approval or admin_review."""

    kind: str
    item: str | None  # the DoD item, the reviewer, or the approval kind
    head_sha: str
    passed: bool
    policy_rev: str | None = None
    workflow_hash: str | None = None
    actor_id: int | None = None


@dataclass(frozen=True)
class GatePolicy:
    policy_rev: str
    dod_items: frozenset[str]
    allowed_workflows: frozenset[str]
    approvers: frozenset[int]
    admins: frozenset[int]
    security_sensitive: bool
    protected_changed: bool
    issue_type: IssueType
    require_reviews: bool = True  # reverts have no review or automated UAT


@dataclass(frozen=True)
class Ready:
    """Every requirement holds on the head SHA."""


@dataclass(frozen=True)
class Waiting:
    missing: tuple[str, ...]  # nothing valid on the head (includes stale and void items)
    stale: tuple[str, ...]  # evidence exists but is outdated (old head, workflow or policy)
    failing: tuple[str, ...]  # the latest valid result is a failure


def evaluate_gate(
    head_sha: str, evidence: Sequence[Evidence], policy: GatePolicy
) -> Ready | Waiting:
    latest: dict[tuple[str, str | None], Evidence] = {}
    for item in evidence:  # later evidence replaces earlier evidence for the same slot
        latest[(item.kind, item.item)] = item

    missing: list[str] = []
    stale: list[str] = []
    failing: list[str] = []

    def record(label: str, status: str) -> None:
        if status in ("missing", "stale"):
            missing.append(label)
        if status == "stale":
            stale.append(label)
        if status == "failing":
            failing.append(label)

    for name in sorted(policy.dod_items):
        record(f"ci:{name}", _result(latest.get(("ci", name)), head_sha, policy, ci=True))
    if policy.require_reviews:
        for reviewer in ("code", "security"):
            record(
                f"review:{reviewer}", _result(latest.get(("review", reviewer)), head_sha, policy)
            )
        if policy.issue_type is not IssueType.DOCS:  # docs skip automated UAT
            record("uat", _result(latest.get(("uat", None)), head_sha, policy))

    is_revert = policy.issue_type is IssueType.REVERT
    required: list[tuple[str, str, frozenset[int]]] = [
        (
            "approval:revert" if is_revert else "approval:uat",
            "revert" if is_revert else "uat",
            policy.admins if is_revert else policy.approvers | policy.admins,
        )
    ]
    if policy.security_sensitive:
        required.append(("approval:security", "security", policy.approvers | policy.admins))
    for label, kind, allowed in required:
        record(label, _approval(evidence, "approval", kind, head_sha, allowed))
    if policy.protected_changed:
        record("admin_review", _approval(evidence, "admin_review", None, head_sha, policy.admins))

    if not (missing or failing):
        return Ready()
    return Waiting(tuple(missing), tuple(stale), tuple(failing))


def _result(item: Evidence | None, head_sha: str, policy: GatePolicy, ci: bool = False) -> str:
    if item is None:
        return "missing"
    if item.head_sha != head_sha:
        return "stale"
    if ci and (
        item.workflow_hash not in policy.allowed_workflows or item.policy_rev != policy.policy_rev
    ):
        return "stale"
    return "ok" if item.passed else "failing"


def _approval(
    evidence: Sequence[Evidence],
    kind: str,
    item: str | None,
    head_sha: str,
    allowed: frozenset[int],
) -> str:
    """An approval counts if its author still holds the role and it is bound to the head."""
    eligible = [e for e in evidence if e.kind == kind and e.item == item and e.actor_id in allowed]
    if any(e.head_sha == head_sha and e.passed for e in eligible):
        return "ok"
    return "stale" if eligible else "missing"
