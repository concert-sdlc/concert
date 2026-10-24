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
