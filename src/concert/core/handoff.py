"""pm-handoff: the result block a worker posts at the end of a run.

Pure. The github module supplies the facts (who authored the block, whether it was edited,
what exists at the commit); this module decides: ignore (untrusted or late), accept, or
reject as invalid (one infra retry, then `blocked`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

from concert.core.events import MalformedEvent, UnsupportedSchema
from concert.core.transitions import Trigger

HANDOFF_SCHEMA_LATEST = 1


class RunState(Enum):
    TRIAGE = "triage"
    SPEC = "spec"
    IMPLEMENTING = "implementing"
    IN_REVIEW = "in_review"
    UAT = "uat"
    INTEGRATING = "integrating"
    VERIFYING_STAGING = "verifying_staging"


@dataclass(frozen=True)
class Handoff:
    schema: int
    state: RunState
    run_id: str
    runner: str
    exit: str
    commit_sha: str | None
    artifact_rev: str | None
    produced: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    doubts: tuple[str, ...] = ()
    lessons_applied: tuple[str, ...] = ()
    lessons_proposed: tuple[str, ...] = ()
    plan_deviations: tuple[str, ...] = ()
    skills_used: tuple[Mapping[str, Any], ...] = ()
    depends_on: tuple[Mapping[str, Any], ...] = ()
    run_commits: tuple[str, ...] = ()
    reviewer: str | None = None  # in_review only: "code" or "security"
    rebases: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )  # old -> new SHA


def _string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise MalformedEvent(f"handoff field {key!r} must be a string")
    return value


def _nullable_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise MalformedEvent(f"handoff field {key!r} must be a string or null")
    return value


def _list(raw: Mapping[str, Any], key: str) -> list[Any]:
    value: Any = raw.get(key, [])
    if not isinstance(value, list):
        raise MalformedEvent(f"handoff field {key!r} must be a list")
    return cast("list[Any]", value)


def _strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    items = _list(raw, key)
    if not all(isinstance(item, str) for item in items):
        raise MalformedEvent(f"handoff field {key!r} must be a list of strings")
    return tuple(cast("list[str]", items))


def _string_map(raw: Mapping[str, Any], key: str) -> Mapping[str, str]:
    value: Any = raw.get(key, {})
    if not isinstance(value, dict):
        raise MalformedEvent(f"handoff field {key!r} must be a mapping of strings")
    pairs = cast("dict[Any, Any]", value)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in pairs.items()):
        raise MalformedEvent(f"handoff field {key!r} must map strings to strings")
    return MappingProxyType(cast("dict[str, str]", dict(pairs)))


def _mappings(raw: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    items = _list(raw, key)
    if not all(isinstance(item, dict) for item in items):
        raise MalformedEvent(f"handoff field {key!r} must be a list of mappings")
    return tuple(cast("list[Mapping[str, Any]]", items))


def _parse_v1(raw: Mapping[str, Any]) -> Handoff:
    try:
        state = RunState(_string(raw, "state"))
    except ValueError as error:
        raise MalformedEvent(f"unknown handoff state: {error}") from None
    return Handoff(
        schema=1,
        state=state,
        run_id=_string(raw, "run_id"),
        runner=_string(raw, "runner"),
        exit=_string(raw, "exit"),
        commit_sha=_nullable_string(raw, "commit_sha"),
        artifact_rev=_nullable_string(raw, "artifact_rev"),
        produced=_strings(raw, "produced"),
        decisions=_strings(raw, "decisions"),
        rejected=_strings(raw, "rejected"),
        doubts=_strings(raw, "doubts"),
        lessons_applied=_strings(raw, "lessons_applied"),
        lessons_proposed=_strings(raw, "lessons_proposed"),
        plan_deviations=_strings(raw, "plan_deviations"),
        skills_used=_mappings(raw, "skills_used"),
        depends_on=_mappings(raw, "depends_on"),
        run_commits=_strings(raw, "run_commits"),
        reviewer=_nullable_string(raw, "reviewer"),
        rebases=_string_map(raw, "rebases"),
    )


def parse_handoff(raw: Mapping[str, Any]) -> Handoff:
    version = raw.get("schema")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise MalformedEvent("handoff field 'schema' must be an integer >= 1")
    if version > HANDOFF_SCHEMA_LATEST:
        raise UnsupportedSchema(version)
    return _parse_v1(raw)


# --- acceptance ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    """The trusted assignment pm-event for a run."""

    run_id: str
    runner: str


@dataclass(frozen=True)
class BlockFacts:
    """Facts about the comment that carried the block."""

    author: str
    edited: bool
    posted_before_lease_expiry: bool


@dataclass(frozen=True)
class RunFacts:
    """Facts about the repository at the end of the run."""

    run_start_head: str | None
    approved_spec_rev: str | None
    commit_exists_on_remote: bool
    tree_hash_at_commit: str | None  # tree hash of docs/specs/<issue>/ at commit_sha
    paths_at_commit: frozenset[str]


@dataclass(frozen=True)
class Accepted:
    """`trigger` is None for exits decided by other rules (waits on a dependency, a
    staging verification failure): the caller reads `handoff.exit`."""

    trigger: Trigger | None
    spec_drift: bool = False


@dataclass(frozen=True)
class Ignored:
    """Untrusted or late: ignored and logged, never an error."""

    reason: str


@dataclass(frozen=True)
class Invalid:
    reason: str


_S = RunState
_T = Trigger

# state -> exit -> trigger (None: decided by other rules, e.g. a dependency wait)
_EXITS: dict[RunState, dict[str, Trigger | None]] = {
    _S.TRIAGE: {
        "needs_input": _T.NEEDS_INPUT,
        "spec": _T.SPEC,
        "duplicate": _T.DUPLICATE,
        "blocked_by": None,
    },
    _S.SPEC: {"needs_input": _T.NEEDS_INPUT, "ready_for_approval": _T.READY_FOR_APPROVAL},
    _S.IMPLEMENTING: {"done": _T.DONE, "needs_input": _T.NEEDS_INPUT, "needs_prerequisite": None},
    _S.IN_REVIEW: {
        "changes_needed": _T.CHANGES_NEEDED,
        "clean": _T.CLEAN,
        "needs_prerequisite": None,
    },
    _S.UAT: {"failed": _T.FAILED, "passed": _T.PASSED, "needs_prerequisite": None},
    _S.INTEGRATING: {"ready_to_merge": _T.READY_TO_MERGE, "rebased": _T.REBASED},
    _S.VERIFYING_STAGING: {"passed": _T.PASSED, "failed": None},
}

_COMMITTING = frozenset({_S.SPEC, _S.IMPLEMENTING, _S.INTEGRATING})


def accept_handoff(
    handoff: Handoff,
    assignments: Mapping[str, Assignment],
    block: BlockFacts,
    facts: RunFacts,
) -> Accepted | Ignored | Invalid:
    assignment = assignments.get(handoff.run_id)
    if assignment is None:
        return Ignored(f"no trusted assignment for run {handoff.run_id}")
    if block.author != assignment.runner or handoff.runner != assignment.runner:
        return Ignored(
            f"run {handoff.run_id} was assigned to {assignment.runner}, not {block.author}"
        )
    if block.edited:
        return Ignored("the block was edited")
    if not block.posted_before_lease_expiry:
        return Ignored(f"the lease of run {handoff.run_id} had expired")

    state = handoff.state
    exits = _EXITS[state]
    if handoff.exit not in exits:
        return Invalid(f"exit {handoff.exit!r} is not listed for {state.value}")

    if state is _S.TRIAGE:
        if handoff.commit_sha is not None or handoff.artifact_rev is not None:
            return Invalid("triage reports no commit and no artifact")
    elif state in _COMMITTING:
        if handoff.commit_sha is None or not facts.commit_exists_on_remote:
            return Invalid("the reported commit does not exist on the remote branch")
    elif handoff.commit_sha != facts.run_start_head:
        return Invalid("a non-committing state must report the head at run start")

    if state is _S.SPEC:
        if handoff.artifact_rev is None or handoff.artifact_rev != facts.tree_hash_at_commit:
            return Invalid("artifact_rev must equal the spec tree hash at the commit")
    elif state is not _S.TRIAGE and handoff.artifact_rev != facts.approved_spec_rev:
        return Invalid("the run must work from the approved spec")

    missing = [path for path in handoff.produced if path not in facts.paths_at_commit]
    if missing:
        return Invalid(f"declared paths missing at the commit: {', '.join(missing)}")

    if (
        state is _S.IMPLEMENTING
        and facts.tree_hash_at_commit is not None
        and facts.tree_hash_at_commit != facts.approved_spec_rev
    ):
        return Accepted(trigger=_T.SPEC_DRIFT, spec_drift=True)

    return Accepted(trigger=exits[handoff.exit])


class InvalidResultAction(Enum):
    RETRY = "retry"
    BLOCK = "block"


def invalid_result_action(previous_invalid: int) -> InvalidResultAction:
    """An invalid or missing result gets one infra retry with the error appended, then blocks."""
    return InvalidResultAction.RETRY if previous_invalid < 1 else InvalidResultAction.BLOCK


def consolidate_reviews(exits: Sequence[str]) -> str:
    """All reviewers report at the same head. needs_prerequisite wins over changes_needed
    (no charge), which wins over clean."""
    if not exits:
        raise ValueError("no reviewer results to consolidate")
    for winner in ("needs_prerequisite", "changes_needed"):
        if winner in exits:
            return winner
    return "clean"
