"""The Steering issue: admins steer the project with `/pm` commands.

Each new command is validated like any other (admin role, never edited, not a bot), recorded as
an `admin_command` event on the Steering issue's own log (so it is acted on once, across
restarts) and answered with one comment. Refused commands are recorded too, so they are
answered once, not on every poll.
"""

from collections.abc import Callable

from concert.core.admin import (
    AdminCommand,
    AdminProblem,
    AdminState,
    fold_admin,
    parse_admin,
)
from concert.core.commands import IssueContext, Valid, validate_command
from concert.core.events import append_event, parse_event
from concert.core.transitions import IssueType, Status
from concert.github.api import GitHubApi
from concert.github.comments import extract_commands
from concert.github.mirror import Mirror, publish_events, reconcile
from concert.runner.poll import RunnerConfig

StatusText = Callable[[int | None], str]
_CONTEXT = IssueContext(
    status=Status.INBOX,
    issue_type=IssueType.CODE,
    spec_hash=None,
    head_sha=None,
    adoption_report_hash=None,
    workflow_diff_hash=None,
    unblock_targets=frozenset(),
    existing_issues=frozenset(),
)


def process_steering(
    github: GitHubApi, mirror: Mirror, steering: int, config: RunnerConfig, status_text: StatusText
) -> AdminState:
    comments = github.list_comments(steering)
    trusted = reconcile(github, mirror, steering, comments, config.pm)
    state = fold_admin(trusted.events)
    chain = [parse_event(e) for e in trusted.events]
    for item in extract_commands(comments, config.roles):
        if item.command.verb != "pm" or item.comment_id in state.consumed:
            continue
        verdict = validate_command(item.command, item.actor, item.facts, _CONTEXT)
        parsed = parse_admin(item.command.arg) if isinstance(verdict, Valid) else None
        data: dict[str, object] = {"comment_id": item.comment_id, "author_id": item.actor.user_id}
        if not isinstance(verdict, Valid):
            data |= {"command": "refused", "reason": verdict.reason}
            reply = f"Not done: {verdict.reason}."
        elif isinstance(parsed, AdminProblem):
            data |= {"command": "refused", "reason": parsed.reason}
            reply = f"Not done: {parsed.reason}."
        else:
            assert isinstance(parsed, AdminCommand)
            data |= _command_data(parsed)
            reply = status_text(parsed.issue) if parsed.action == "status" else _ack(parsed)
        event = append_event(chain, epoch=config.epoch, kind="admin_command", data=data)
        publish_events(github, mirror, steering, [event])
        chain.append(event)
        github.add_comment(steering, reply)
        state = fold_admin([*trusted.events, *(e.to_dict() for e in chain[len(trusted.events) :])])
    return state


def mark_reset_applied(
    github: GitHubApi, mirror: Mirror, steering: int, config: RunnerConfig, issue: int, now: int
) -> None:
    """Record that the reset requested for `issue` has been carried out."""
    trusted = reconcile(github, mirror, steering, github.list_comments(steering), config.pm)
    chain = [parse_event(e) for e in trusted.events]
    event = append_event(
        chain, epoch=config.epoch, kind="admin_reset_applied", data={"issue": issue, "at": now}
    )
    publish_events(github, mirror, steering, [event])


def _command_data(command: AdminCommand) -> dict[str, object]:
    data: dict[str, object] = {"command": command.action}
    if command.issue is not None:
        data["issue"] = command.issue
    if command.state is not None:
        data["state"] = command.state.value
    if command.now:
        data["now"] = True
    return data


def _ack(command: AdminCommand) -> str:
    scope = f"issue #{command.issue}" if command.issue else "the whole project"
    if command.action == "pause":
        return f"Paused {scope}: no new runs or merges{' (and running work stops)' if command.now else ''}."
    if command.action == "resume":
        return f"Resumed {scope}."
    return f"Issue #{command.issue} will be reset to {command.state.value if command.state else ''} counting from now."
