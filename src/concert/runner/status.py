"""Text for `/pm status`: derived from what the PM already knows, nothing fetched."""

from collections.abc import Mapping

from concert.core.derive import IssueState
from concert.core.transitions import Status

_ORDER = list(Status)


def render_board(
    states: Mapping[int, IssueState], paused: bool, paused_issues: frozenset[int]
) -> str:
    if not states:
        return "No issues seen yet."
    lines = ["## Board", ""]
    if paused:
        lines += ["**The project is paused:** no new runs or merges.", ""]
    by_status: dict[Status, list[int]] = {}
    for number, state in sorted(states.items()):
        by_status.setdefault(state.status, []).append(number)
    for status in sorted(by_status, key=_ORDER.index):
        numbers = ", ".join(f"#{n}" for n in by_status[status])
        lines.append(f"- {status.value}: {len(by_status[status])} ({numbers})")
    blocked = [(n, s) for n, s in sorted(states.items()) if s.status is Status.BLOCKED]
    if blocked:
        lines += ["", "### Blocked"]
        lines += [
            f"- #{n}: {s.blocked_reason or s.note or 'no reason recorded'}" for n, s in blocked
        ]
    waiting = [(n, s) for n, s in sorted(states.items()) if s.dependencies]
    if waiting:
        lines += ["", "### Waiting"]
        for n, state in waiting:
            on = ", ".join(f"#{d.issue}" for d in state.dependencies)
            lines.append(f"- #{n} is waiting on {on}")
    if paused_issues:
        lines += ["", "### Paused issues", ", ".join(f"#{n}" for n in sorted(paused_issues))]
    return "\n".join(lines)


def render_issue(number: int, state: IssueState | None, paused: bool) -> str:
    if state is None:
        return f"Issue #{number} has not been seen by the PM."
    lines = [f"## #{number}: {state.status.value}"]
    if state.blocked_reason or state.note:
        lines.append(f"> {state.blocked_reason or state.note}")
    if state.dependencies:
        lines.append(
            "Waiting on " + ", ".join(f"#{d.issue} ({d.kind})" for d in state.dependencies)
        )
    if paused:
        lines.append("Paused.")
    return "\n".join(lines)
