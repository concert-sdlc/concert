"""Human commands: parsing and validation (design: "Commands and binding")."""

from dataclasses import replace

import pytest

from concert.core.commands import (
    Actor,
    Command,
    CommentFacts,
    IssueContext,
    Rejected,
    Role,
    Valid,
    parse_command,
    validate_command,
)
from concert.core.transitions import IssueType, Status

SPEC_HASH = "4be77c0aa11"
HEAD_SHA = "9f2c1ab77de"
REPORT_HASH = "c0ffee12345"
DIFF_HASH = "d1ff0000abc"

ADMIN = Actor(user_id=1, login="alice", is_bot=False, role=Role.ADMIN)
APPROVER = Actor(user_id=2, login="pat", is_bot=False, role=Role.APPROVER)
CONTRIBUTOR = Actor(user_id=3, login="bob", is_bot=False, role=Role.CONTRIBUTOR)
OUTSIDER = Actor(user_id=4, login="eve", is_bot=False, role=Role.NONE)
BOT = Actor(user_id=5, login="concert-pm[bot]", is_bot=True, role=Role.ADMIN)

CLEAN = CommentFacts(edited=False)


def ctx(
    status: Status, issue_type: IssueType = IssueType.CODE, **overrides: object
) -> IssueContext:
    base = IssueContext(
        status=status,
        issue_type=issue_type,
        spec_hash=SPEC_HASH,
        head_sha=HEAD_SHA,
        adoption_report_hash=REPORT_HASH,
        workflow_diff_hash=DIFF_HASH,
        unblock_targets=frozenset({Status.IMPLEMENTING, Status.SPEC}),
        existing_issues=frozenset({7, 12}),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def check(body: str, actor: Actor, context: IssueContext, facts: CommentFacts = CLEAN):
    command = parse_command(body)
    assert command is not None, body
    return validate_command(command, actor, facts, context)


# --- parsing ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "verb", "kind", "arg"),
    [
        ("/dispatch", "dispatch", None, ""),
        ("/dispatch hotfix", "dispatch", "hotfix", ""),
        (f"/approve spec {SPEC_HASH}", "approve", "spec", SPEC_HASH),
        (
            "/reject uat tests do not cover the export",
            "reject",
            "uat",
            "tests do not cover the export",
        ),
        ("/unblock implementing", "unblock", None, "implementing"),
        ("/answer Use the new tenant table", "answer", None, "Use the new tenant table"),
        ("/depends-on #12 merge", "depends-on", None, "#12 merge"),
        ("/drop-dependency #12", "drop-dependency", None, "#12"),
        ("/pm status", "pm", None, "status"),
        ("  /dispatch  ", "dispatch", None, ""),
        ("/dispatch\nthanks!", "dispatch", None, ""),
    ],
)
def test_commands_parse(body: str, verb: str, kind: str | None, arg: str) -> None:
    assert parse_command(body) == Command(verb=verb, kind=kind, arg=arg)


@pytest.mark.parametrize(
    "body", ["", "hello", "please /dispatch this", "/frobnicate", "//dispatch"]
)
def test_ordinary_comments_are_not_commands(body: str) -> None:
    assert parse_command(body) is None


# --- the four validity rules --------------------------------------------------------------


def test_a_valid_command_passes() -> None:
    result = check("/dispatch", APPROVER, ctx(Status.INBOX))
    assert result == Valid(Command(verb="dispatch", kind=None, arg=""))


def test_the_author_needs_the_listed_role() -> None:
    assert isinstance(check("/dispatch", CONTRIBUTOR, ctx(Status.INBOX)), Rejected)
    assert isinstance(check("/dispatch", OUTSIDER, ctx(Status.INBOX)), Rejected)


def test_an_admin_can_do_what_an_approver_can() -> None:
    assert isinstance(check("/dispatch", ADMIN, ctx(Status.INBOX)), Valid)


def test_a_bot_identity_never_passes_even_with_a_role() -> None:
    result = check("/dispatch", BOT, ctx(Status.INBOX))
    assert isinstance(result, Rejected)
    assert "bot" in result.reason


def test_an_edited_comment_is_not_accepted() -> None:
    result = check("/dispatch", APPROVER, ctx(Status.INBOX), CommentFacts(edited=True))
    assert isinstance(result, Rejected)
    assert "edited" in result.reason


# --- binding: spec hash and head SHA ------------------------------------------------------


def test_approve_spec_binds_to_the_current_spec_hash() -> None:
    state = ctx(Status.SPEC_APPROVAL)
    assert isinstance(check(f"/approve spec {SPEC_HASH}", APPROVER, state), Valid)
    assert isinstance(check("/approve spec deadbee", APPROVER, state), Rejected)
    assert isinstance(check("/approve spec", APPROVER, state), Rejected)


def test_a_prefix_of_at_least_seven_characters_binds_and_a_shorter_one_does_not() -> None:
    state = ctx(Status.SPEC_APPROVAL)
    assert isinstance(check(f"/approve spec {SPEC_HASH[:7]}", APPROVER, state), Valid)
    assert isinstance(check(f"/approve spec {SPEC_HASH[:6]}", APPROVER, state), Rejected)


def test_approve_uat_and_security_bind_to_the_current_head_sha() -> None:
    state = ctx(Status.HUMAN_UAT)
    assert isinstance(check(f"/approve uat {HEAD_SHA}", APPROVER, state), Valid)
    assert isinstance(check(f"/approve security {HEAD_SHA}", APPROVER, state), Valid)
    assert isinstance(check("/approve uat 0000000", APPROVER, state), Rejected)


def test_a_spec_hash_never_satisfies_a_sha_bound_command_or_vice_versa() -> None:
    assert isinstance(check(f"/approve uat {SPEC_HASH}", APPROVER, ctx(Status.HUMAN_UAT)), Rejected)
    assert isinstance(
        check(f"/approve spec {HEAD_SHA}", APPROVER, ctx(Status.SPEC_APPROVAL)), Rejected
    )


def test_a_stale_head_sha_is_rejected_after_the_head_moves() -> None:
    moved = ctx(Status.HUMAN_UAT, head_sha="1234567abcd")
    assert isinstance(check(f"/approve uat {HEAD_SHA}", APPROVER, moved), Rejected)


# --- gates are decided only by valid commands in the right state --------------------------


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("/dispatch", Status.TRIAGE),
        (f"/approve spec {SPEC_HASH}", Status.IMPLEMENTING),
        (f"/approve uat {HEAD_SHA}", Status.IN_REVIEW),
        ("/reject uat no", Status.UAT),
        ("/unblock implementing", Status.IMPLEMENTING),
        ("/answer here", Status.SPEC),
    ],
)
def test_a_command_in_the_wrong_state_is_rejected(body: str, status: Status) -> None:
    assert isinstance(check(body, ADMIN, ctx(status)), Rejected)


def test_reject_needs_a_reason() -> None:
    assert isinstance(check("/reject spec", APPROVER, ctx(Status.SPEC_APPROVAL)), Rejected)
    assert isinstance(check("/reject spec too vague", APPROVER, ctx(Status.SPEC_APPROVAL)), Valid)


# --- admin commands -----------------------------------------------------------------------


def test_adopt_revert_and_workflow_approvals_are_admin_only_and_bound() -> None:
    assert isinstance(check(f"/approve adopt {REPORT_HASH}", ADMIN, ctx(Status.INBOX)), Valid)
    assert isinstance(check(f"/approve adopt {REPORT_HASH}", APPROVER, ctx(Status.INBOX)), Rejected)
    assert isinstance(check("/approve adopt 1111111", ADMIN, ctx(Status.INBOX)), Rejected)
    assert isinstance(check(f"/approve workflow {DIFF_HASH}", ADMIN, ctx(Status.HUMAN_UAT)), Valid)
    assert isinstance(
        check(f"/approve workflow {DIFF_HASH}", APPROVER, ctx(Status.HUMAN_UAT)), Rejected
    )


def test_revert_approval_and_rejection_apply_only_to_revert_issues_in_human_uat() -> None:
    revert = ctx(Status.HUMAN_UAT, IssueType.REVERT)
    assert isinstance(check(f"/approve revert {HEAD_SHA}", ADMIN, revert), Valid)
    assert isinstance(check("/reject revert wrong merge", ADMIN, revert), Valid)
    assert isinstance(check(f"/approve revert {HEAD_SHA}", APPROVER, revert), Rejected)
    code = ctx(Status.HUMAN_UAT)
    assert isinstance(check(f"/approve revert {HEAD_SHA}", ADMIN, code), Rejected)
    assert isinstance(check("/reject revert x", ADMIN, code), Rejected)


def test_uat_commands_do_not_apply_to_a_revert_issue() -> None:
    revert = ctx(Status.HUMAN_UAT, IssueType.REVERT)
    assert isinstance(check(f"/approve uat {HEAD_SHA}", ADMIN, revert), Rejected)
    assert isinstance(check("/reject uat no", ADMIN, revert), Rejected)


def test_dispatch_hotfix_is_admin_only() -> None:
    assert isinstance(check("/dispatch hotfix", ADMIN, ctx(Status.INBOX)), Valid)
    assert isinstance(check("/dispatch hotfix", APPROVER, ctx(Status.INBOX)), Rejected)


def test_unblock_targets_must_be_allowed() -> None:
    blocked = ctx(Status.BLOCKED)
    assert isinstance(check("/unblock implementing", APPROVER, blocked), Valid)
    assert isinstance(check("/unblock on_staging", APPROVER, blocked), Rejected)
    assert isinstance(check("/unblock nowhere", APPROVER, blocked), Rejected)


def test_answer_is_open_to_any_trusted_human_in_needs_input() -> None:
    asking = ctx(Status.NEEDS_INPUT)
    assert isinstance(check("/answer use the new table", CONTRIBUTOR, asking), Valid)
    assert isinstance(check("/answer use the new table", OUTSIDER, asking), Rejected)
    assert isinstance(check("/answer", CONTRIBUTOR, asking), Rejected)


def test_dependency_commands_need_an_existing_issue() -> None:
    state = ctx(Status.IMPLEMENTING)
    assert isinstance(check("/depends-on #12 merge", APPROVER, state), Valid)
    assert isinstance(check("/depends-on #99 merge", APPROVER, state), Rejected)
    assert isinstance(check("/drop-dependency #12", APPROVER, state), Valid)
    assert isinstance(check("/depends-on #12", CONTRIBUTOR, state), Rejected)


def test_pm_status_is_for_any_trusted_human_and_other_pm_commands_for_admins() -> None:
    state = ctx(Status.IMPLEMENTING)
    assert isinstance(check("/pm status", CONTRIBUTOR, state), Valid)
    assert isinstance(check("/pm status", OUTSIDER, state), Rejected)
    assert isinstance(check("/pm pause now", ADMIN, state), Valid)
    assert isinstance(check("/pm pause now", APPROVER, state), Rejected)
