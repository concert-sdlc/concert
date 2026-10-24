"""CI check runs as evidence.

Pure. The github module lists the check runs for a commit; this module decides which of them
are new evidence for the definition of done. A result is recorded once per distinct outcome:
the same check on the same head, workflow and policy that is already in the ledger adds nothing.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from concert.core.decide import Proposed
from concert.core.evidence import Evidence


@dataclass(frozen=True)
class CheckRun:
    name: str
    head_sha: str
    conclusion: str | None  # None until the run has finished
    workflow_hash: str  # hash of the workflow file that produced the run


@dataclass(frozen=True)
class PrReview:
    user_id: int
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
    commit_id: str  # the head the review was made on


def plan_review_evidence(
    head_sha: str | None,
    reviews: Sequence[PrReview],
    recorded: Sequence[Evidence],
    admins: Collection[int],
) -> list[Proposed]:
    """An approving GitHub PR review by an admin on the current head is the evidence a change to
    protected paths needs (a comment is not enough: GitHub binds a review to a commit)."""
    if head_sha is None:
        return []
    known = {e.actor_id for e in recorded if e.kind == "admin_review" and e.head_sha == head_sha}
    proposals: list[Proposed] = []
    for review in reviews:
        if (
            review.state == "APPROVED"
            and review.commit_id == head_sha
            and review.user_id in admins
            and review.user_id not in known
        ):
            known.add(review.user_id)
            proposals.append(
                Proposed(
                    "evidence",
                    {
                        "kind": "admin_review",
                        "item": None,
                        "head_sha": head_sha,
                        "passed": True,
                        "actor_id": review.user_id,
                    },
                )
            )
    return proposals


def plan_ci_evidence(
    head_sha: str | None,
    runs: Sequence[CheckRun],
    recorded: Sequence[Evidence],
    dod_items: Collection[str],
    policy_rev: str,
) -> list[Proposed]:
    if head_sha is None:
        return []
    latest: dict[str, CheckRun] = {}
    for found in runs:  # a later run of the same check replaces the earlier one
        if found.head_sha == head_sha and found.conclusion is not None:
            latest[found.name] = found
    proposals: list[Proposed] = []
    for name in sorted(dod_items):
        found = latest.get(name)
        if found is None:
            continue
        evidence = Evidence(
            kind="ci",
            item=name,
            head_sha=head_sha,
            passed=found.conclusion == "success",
            policy_rev=policy_rev,
            workflow_hash=found.workflow_hash,
        )
        previous = next((e for e in reversed(recorded) if e.kind == "ci" and e.item == name), None)
        if previous == evidence:  # only the latest recorded result counts, as in the gate
            continue
        proposals.append(
            Proposed(
                "evidence",
                {
                    "kind": "ci",
                    "item": name,
                    "head_sha": head_sha,
                    "passed": evidence.passed,
                    "policy_rev": policy_rev,
                    "workflow_hash": found.workflow_hash,
                },
            )
        )
    return proposals
