"""Commit binding, the between-runs check, inherited ranges and recovery.

Pure. The github module lists the commits; this module decides whether they are the ones
Concert's own accepted runs produced. Agents hold no credentials, so a foreign commit can
only come from a human with write access or a worker not assigned to the issue. (A
compromised worker assigned to the issue writes its own `run_commits`: out of scope here,
mitigated by review and the gates.)
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from concert.core.handoff import Handoff


@dataclass(frozen=True)
class Bound:
    """Every commit in the range is accounted for."""


@dataclass(frozen=True)
class ForeignCommits:
    """The issue goes to `blocked` with "foreign commit"."""

    shas: tuple[str, ...]


def check_commit_binding(
    branch_range: Sequence[str],
    accepted: Sequence[Handoff],
    inherited: Sequence[Sequence[str]] = (),
    adopted: Collection[str] = (),
) -> Bound | ForeignCommits:
    """Every commit in `staging-merge-base..commit_sha` must be in some accepted run's
    `run_commits`, or a rebase product of such a commit, or in a recorded inherited range, or
    adopted by an admin (`/pm adopt-commits`)."""
    allowed: set[str] = set(adopted)
    for handoff in accepted:
        allowed.update(handoff.run_commits)
    for commits in inherited:
        allowed.update(commits)

    # A rebase only launders commits that were already accepted: follow mappings whose old
    # SHA is allowed, repeatedly, so chains of rebases resolve.
    mappings = [pair for handoff in accepted for pair in handoff.rebases.items()]
    grew = True
    while grew:
        grew = False
        for old, new in mappings:
            if old in allowed and new not in allowed:
                allowed.add(new)
                grew = True

    foreign = tuple(sha for sha in branch_range if sha not in allowed)
    return ForeignCommits(foreign) if foreign else Bound()


# --- between runs ----------------------------------------------------------------------------


@dataclass(frozen=True)
class UnexpectedPush:
    head: str
    expected: str


def expected_head(last_accepted: str | None, branch_creation: str | None) -> str:
    """The last accepted `commit_sha`; before any accepted handoff, the branch-creation
    commit recorded in a pm-event."""
    if last_accepted is not None:
        return last_accepted
    if branch_creation is not None:
        return branch_creation
    raise ValueError("no accepted commit and no recorded branch-creation commit")


def check_between_runs(branch_head: str, expected: str) -> UnexpectedPush | None:
    return None if branch_head == expected else UnexpectedPush(head=branch_head, expected=expected)


# --- inherited ranges after a crash ----------------------------------------------------------


@dataclass(frozen=True)
class CommitMeta:
    sha: str
    parent: str
    author: str
    committer: str


@dataclass(frozen=True)
class InheritedValid:
    shas: tuple[str, ...]


@dataclass(frozen=True)
class InheritedRejected:
    reason: str


def validate_inherited_range(
    commits: Sequence[CommitMeta], last_accepted: str | None, agent_identity: str
) -> InheritedValid | InheritedRejected:
    """A crashed run's commit range is inherited only if it fast-forwards from the last
    accepted commit and every commit's author and committer is the Concert agent identity."""
    parent = last_accepted
    for commit in commits:
        if commit.parent != parent:
            return InheritedRejected(f"{commit.sha} does not fast-forward from {parent}")
        if commit.author != agent_identity or commit.committer != agent_identity:
            return InheritedRejected(
                f"{commit.sha} was not authored and committed by {agent_identity}"
            )
        parent = commit.sha
    return InheritedValid(tuple(commit.sha for commit in commits))


# --- recovery -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResetPlan:
    reset_to: str
    dropped: tuple[str, ...]  # logged as a pm-event


def plan_recovery(foreign: Sequence[str], last_accepted: str) -> ResetPlan:
    """On `/unblock` or `/pm reset`, force-reset the branch to the last accepted commit,
    dropping the foreign commits. An admin keeps a hand fix with `/pm adopt-commits`."""
    return ResetPlan(reset_to=last_accepted, dropped=tuple(foreign))
