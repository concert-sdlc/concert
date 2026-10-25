"""Admin commands (`/pm ...`) and the project state they leave on the Steering issue.

Pure. Parsing turns the text after `/pm` into a command or a reason it was refused; folding the
Steering issue's events yields what is paused, which resets are waiting to be applied and which
comments were already handled, so a command is acted on once however often the loop restarts.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from concert.core.transitions import Status, is_terminal

# Where `/pm reset` may send an issue: any state it can legitimately be worked from.
_RESET_TARGETS = frozenset(
    {
        Status.INBOX,
        Status.TRIAGE,
        Status.SPEC,
        Status.SPEC_APPROVAL,
        Status.IMPLEMENTING,
        Status.IN_REVIEW,
        Status.UAT,
        Status.HUMAN_UAT,
    }
)


@dataclass(frozen=True)
class AdminCommand:
    action: str  # pause, resume, status or reset
    issue: int | None = None
    state: Status | None = None
    now: bool = False


@dataclass(frozen=True)
class AdminProblem:
    reason: str


def parse_admin(text: str) -> AdminCommand | AdminProblem:
    parts = text.split()
    if not parts:
        return AdminProblem("name a subcommand: pause, resume, status or reset")
    action, rest = parts[0].lower(), parts[1:]
    if action in ("pause", "resume", "status"):
        now = action == "pause" and rest[:1] == ["now"]
        args = rest[1:] if now else rest
        if len(args) > 1 or (args and _issue(args[0]) is None):
            return AdminProblem(f"/pm {action} takes at most one issue, written #N")
        if now and args:
            return AdminProblem("/pm pause now cannot name an issue: write the issue as #N alone")
        return AdminCommand(action, issue=_issue(args[0]) if args else None, now=now)
    if action == "reset":
        if len(rest) != 2 or _issue(rest[0]) is None:
            return AdminProblem("reset needs an issue and a state: /pm reset #N <state>")
        try:
            state = Status(rest[1].lower())
        except ValueError:
            return AdminProblem(f"{rest[1]!r} is not a state")
        if state not in _RESET_TARGETS or is_terminal(state):
            return AdminProblem(f"an issue cannot be reset to {state.value}")
        return AdminCommand("reset", issue=_issue(rest[0]), state=state)
    return AdminProblem(f"unknown subcommand {action!r}")


def _issue(token: str) -> int | None:
    if token.startswith("#") and token[1:].isdigit() and int(token[1:]) > 0:
        return int(token[1:])
    return None


@dataclass(frozen=True)
class AdminState:
    paused: bool = False
    paused_issues: frozenset[int] = frozenset()
    resets: Mapping[int, Status] = field(default_factory=dict[int, Status])
    consumed: frozenset[int] = frozenset()  # comment ids already handled
    blocked: str | None = None  # the Steering history cannot be trusted: everything waits

    def holds(self, issue: int) -> bool:
        """New work (runs, merges) for this issue waits."""
        return self.paused or self.blocked is not None or issue in self.paused_issues


def fold_admin(events: Sequence[Mapping[str, Any]]) -> AdminState:
    paused = False
    paused_issues: set[int] = set()
    resets: dict[int, Status] = {}
    consumed: set[int] = set()
    for event in events:
        data = cast("Mapping[str, Any]", event.get("data") or {})
        kind = event.get("kind")
        if kind == "admin_command":
            consumed.add(int(data["comment_id"]))
            command, issue = data.get("command"), data.get("issue")
            if command == "pause":
                if issue is None:
                    paused = True
                else:
                    paused_issues.add(int(issue))
            elif command == "resume":
                if issue is None:
                    paused = False
                else:
                    paused_issues.discard(int(issue))
            elif command == "reset" and issue is not None:
                resets[int(issue)] = Status(str(data["state"]))
        elif kind == "admin_reset_applied":
            resets.pop(int(data["issue"]), None)
    return AdminState(paused, frozenset(paused_issues), resets, frozenset(consumed))
