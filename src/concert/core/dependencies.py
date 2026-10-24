"""Issue dependencies.

Pure rules. `start` (the default for code prerequisites): the PM takes no step for the issue
until the other is on staging. `merge`: the issue works normally but cannot take the integration
queue head until then. Waiting is never `blocked`; an unfinished, closed or blocked prerequisite
keeps the wait in place (the digest flags it).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from concert.core.transitions import Status

START = "start"
MERGE = "merge"

_BEFORE_MERGE = frozenset(
    {
        Status.INBOX,
        Status.TRIAGE,
        Status.NEEDS_INPUT,
        Status.SPEC,
        Status.SPEC_APPROVAL,
        Status.IMPLEMENTING,
        Status.IN_REVIEW,
        Status.UAT,
        Status.HUMAN_UAT,
    }
)


@dataclass(frozen=True)
class Dependency:
    issue: int
    kind: str  # "start" or "merge"


def parse_dependency_arg(arg: str) -> tuple[int, str] | None:
    parts = arg.split()
    if not 1 <= len(parts) <= 2:
        return None
    token = parts[0]
    if not token.startswith("#") or not token[1:].isdigit() or int(token[1:]) < 1:
        return None
    kind = parts[1].lower() if len(parts) == 2 else START
    if kind not in (START, MERGE):
        return None
    return int(token[1:]), kind


def unmet(
    dependencies: Sequence[Dependency], statuses: Mapping[int, Status], kind: str | None
) -> list[Dependency]:
    """The dependencies (of one kind, or all) whose issue is not on staging yet."""
    return [
        d
        for d in dependencies
        if (kind is None or d.kind == kind) and statuses.get(d.issue) is not Status.ON_STAGING
    ]


def dependency_problem(
    issue: int,
    target: int,
    status: Status,
    graph: Mapping[int, frozenset[int]],
    exists: bool,
) -> str | None:
    """Why `issue` may not wait on `target`, or None. `graph` maps issues to what they wait on."""
    if target == issue:
        return "an issue cannot depend on itself"
    if not exists:
        return f"issue #{target} does not exist"
    if status not in _BEFORE_MERGE:
        return "dependencies can only be added before the work has merged"
    if _reaches(graph, target, issue):
        return f"depending on #{target} would close a cycle"
    return None


def _reaches(graph: Mapping[int, frozenset[int]], start: int, goal: int) -> bool:
    seen: set[int] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))
    return False
