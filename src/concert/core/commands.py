"""Human commands: parsing and validation.

Pure. A command is valid only if the author has the listed role (by user id), is not a bot,
never edited the comment, and its argument binds exactly to what the command names. A spec
hash never satisfies a SHA-bound command or vice versa. Gate passage is decided only by
valid commands; invalid ones are ignored and logged by the caller.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from concert.core.transitions import IssueType, Status


class Role(IntEnum):
    """Admins can do what approvers can; approvers what contributors can."""

    NONE = 0
    CONTRIBUTOR = 1  # trusted input, cannot approve
    APPROVER = 2
    ADMIN = 3


@dataclass(frozen=True)
class Actor:
    user_id: int
    login: str
    is_bot: bool
    role: Role  # resolved from `humans.*` by user id, never by login


@dataclass(frozen=True)
class CommentFacts:
    edited: bool


@dataclass(frozen=True)
class IssueContext:
    status: Status
    issue_type: IssueType
    spec_hash: str | None
    head_sha: str | None
    adoption_report_hash: str | None
    workflow_diff_hash: str | None
    unblock_targets: frozenset[Status]
    existing_issues: frozenset[int]


@dataclass(frozen=True)
class Command:
    verb: str
    kind: str | None
    arg: str


@dataclass(frozen=True)
class Valid:
    command: Command


@dataclass(frozen=True)
class Rejected:
    reason: str


_VERBS = frozenset(
    {
        "dispatch",
        "approve",
        "reject",
        "unblock",
        "answer",
        "depends-on",
        "drop-dependency",
        "pm",
        "lesson",
    }
)


def parse_command(body: str) -> Command | None:
    """A command is the first line of a comment, starting with `/`."""
    stripped = body.strip()
    if not stripped:
        return None
    line = stripped.splitlines()[0].strip()
    if not line.startswith("/"):
        return None
    head, _, rest = line[1:].partition(" ")
    verb, rest = head, rest.strip()
    if verb not in _VERBS:
        return None
    if verb == "dispatch":
        if rest in ("hotfix", "docs"):
            return Command("dispatch", rest, "")
        return Command("dispatch", None, rest)
    if verb in ("approve", "reject"):
        kind, _, arg = rest.partition(" ")
        return Command(verb, kind or None, arg.strip())
    return Command(verb, None, rest)


# --- rules ---------------------------------------------------------------------------------

_Binding = Callable[[Command, IssueContext], str | None]  # None = binds; else the reason


def _prefix_binding(attribute: str, label: str) -> _Binding:
    def bind(command: Command, context: IssueContext) -> str | None:
        expected: str | None = getattr(context, attribute)
        argument = command.arg.strip().lower()
        if expected is None or len(argument) < 7 or not expected.lower().startswith(argument):
            return f"the argument must be the current {label} (at least 7 characters)"
        return None

    return bind


def _reason_required(command: Command, _context: IssueContext) -> str | None:
    return None if command.arg.strip() else "a reason is required"


def _no_argument(command: Command, _context: IssueContext) -> str | None:
    return None if not command.arg else "this command takes no argument"


def _text_required(command: Command, _context: IssueContext) -> str | None:
    return None if command.arg.strip() else "the answer text is required"


LESSON_LIMIT = 2_000


def _lesson_text(command: Command, _context: IssueContext) -> str | None:
    if not command.arg.strip():
        return "write the lesson after /lesson"
    if len(command.arg) > LESSON_LIMIT:
        return f"a lesson is at most {LESSON_LIMIT} characters"
    return None


def _unblock_target(command: Command, context: IssueContext) -> str | None:
    try:
        target = Status(command.arg.strip())
    except ValueError:
        return f"unknown state {command.arg!r}"
    return None if target in context.unblock_targets else f"{target.value} is not an allowed target"


def _issue_reference(command: Command, context: IssueContext) -> str | None:
    token = command.arg.split(maxsplit=1)[0] if command.arg.split() else ""
    if not token.startswith("#") or not token[1:].isdigit():
        return "name an issue as #N"
    return None if int(token[1:]) in context.existing_issues else f"issue {token} does not exist"


def _anything(_command: Command, _context: IssueContext) -> str | None:
    return None


@dataclass(frozen=True)
class _Rule:
    role: Role
    binding: _Binding
    statuses: frozenset[Status] | None = None
    issue_types: frozenset[IssueType] | None = None


_ALL_TYPES = frozenset(IssueType)
_NOT_REVERT = _ALL_TYPES - {IssueType.REVERT, IssueType.WORKFLOW}
_S = Status


def _rule(command: Command) -> _Rule | None:
    verb, kind = command.verb, command.kind
    if verb == "dispatch":
        if kind == "hotfix":
            return _Rule(Role.ADMIN, _no_argument, frozenset({_S.INBOX}))
        if kind in (None, "docs"):
            return _Rule(Role.APPROVER, _no_argument, frozenset({_S.INBOX}))
    if verb == "approve":
        if kind == "spec":
            return _Rule(
                Role.APPROVER,
                _prefix_binding("spec_hash", "spec hash"),
                frozenset({_S.SPEC_APPROVAL}),
            )
        if kind in ("uat", "security"):
            return _Rule(
                Role.APPROVER,
                _prefix_binding("head_sha", "head SHA"),
                frozenset({_S.HUMAN_UAT}),
                _NOT_REVERT,
            )
        if kind == "adopt":
            return _Rule(Role.ADMIN, _prefix_binding("adoption_report_hash", "report hash"))
        if kind == "revert":
            return _Rule(
                Role.ADMIN,
                _prefix_binding("head_sha", "revert PR head SHA"),
                frozenset({_S.HUMAN_UAT}),
                frozenset({IssueType.REVERT}),
            )
        if kind == "workflow":
            return _Rule(
                Role.ADMIN,
                _prefix_binding("workflow_diff_hash", "workflow diff hash"),
                frozenset({_S.HUMAN_UAT}),
                frozenset({IssueType.WORKFLOW}),
            )
    if verb == "reject":
        if kind == "spec":
            return _Rule(Role.APPROVER, _reason_required, frozenset({_S.SPEC_APPROVAL}))
        if kind == "uat":
            return _Rule(Role.APPROVER, _reason_required, frozenset({_S.HUMAN_UAT}), _NOT_REVERT)
        if kind == "revert":
            return _Rule(
                Role.ADMIN,
                _reason_required,
                frozenset({_S.HUMAN_UAT}),
                frozenset({IssueType.REVERT}),
            )
    if verb == "unblock":
        return _Rule(Role.APPROVER, _unblock_target, frozenset({_S.BLOCKED}))
    if verb == "answer":
        return _Rule(Role.CONTRIBUTOR, _text_required, frozenset({_S.NEEDS_INPUT}))
    if verb == "lesson":
        return _Rule(Role.CONTRIBUTOR, _lesson_text)
    if verb in ("depends-on", "drop-dependency"):
        return _Rule(Role.APPROVER, _issue_reference)
    if verb == "pm":
        subcommand = command.arg.split(maxsplit=1)[0] if command.arg.split() else ""
        return _Rule(Role.CONTRIBUTOR if subcommand == "status" else Role.ADMIN, _anything)
    return None


def validate_command(
    command: Command, actor: Actor, facts: CommentFacts, context: IssueContext
) -> Valid | Rejected:
    if actor.is_bot:
        return Rejected("bot identities cannot issue commands")
    if facts.edited:
        return Rejected(
            "the comment was edited: post a new comment; edited commands are not accepted"
        )
    rule = _rule(command)
    if rule is None:
        return Rejected(f"unknown command /{command.verb} {command.kind or ''}".rstrip())
    needed = rule.role
    if context.issue_type is IssueType.HOTFIX and command.verb in ("approve", "reject"):
        needed = max(needed, Role.ADMIN)  # a hotfix skips the queue, so an admin decides on it
    if actor.role < needed:
        return Rejected(f"/{command.verb} needs the {needed.name.lower()} role")
    if rule.statuses is not None and context.status not in rule.statuses:
        return Rejected(
            f"/{command.verb} does not apply while the issue is in {context.status.value}"
        )
    if rule.issue_types is not None and context.issue_type not in rule.issue_types:
        return Rejected(
            f"/{command.verb} {command.kind or ''} does not apply to a {context.issue_type.value} issue".replace(
                "  ", " "
            )
        )
    problem = rule.binding(command, context)
    if problem is not None:
        return Rejected(problem)
    return Valid(command)
