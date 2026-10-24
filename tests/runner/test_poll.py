"""The poll loop: comments in, events out, driven against the fake GitHub with no agent."""

from concert.core.derive import IssueState
from concert.core.handoff import Handoff
from concert.core.transitions import Status
from concert.github.git_facts import GitFacts
from tests.runner.helpers import ALICE, MALLORY, SPEC_FILES, World

REVIEWERS = ("code", "security")


def to_spec_approval(world: World, issue: int) -> tuple[str, str]:
    """dispatch -> triage -> spec -> spec_approval. Returns (spec_commit, spec_hash)."""
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "spec")
    world.poll(issue)
    base = world.pm.get_ref(f"pm/{issue}")  # the poll created the issue branch on entering spec
    run = world.assign(issue, head=base)
    sha = world.wip_commit(issue, run, SPEC_FILES)
    spec_hash = world.spec_hash(sha, issue)
    world.handoff(
        issue,
        run,
        "spec",
        "ready_for_approval",
        commit_sha=sha,
        artifact_rev=spec_hash,
        run_commits=[sha],
    )
    result = world.poll(issue)
    assert result.status is Status.SPEC_APPROVAL, result
    return sha, spec_hash


def to_implementing(world: World, issue: int) -> tuple[str, str]:
    sha, spec_hash = to_spec_approval(world, issue)
    world.say(ALICE, issue, f"/approve spec {spec_hash}")
    assert world.poll(issue).status is Status.IMPLEMENTING
    return sha, spec_hash


# --- the first poll and the Status card -------------------------------------------------------


def test_the_first_poll_adopts_the_issue_into_inbox() -> None:
    world = World()
    issue = world.new_issue()
    result = world.poll(issue)
    assert result.status is Status.INBOX
    assert result.posted == 1
    assert world.pm.get_status(issue) == "inbox"


def test_polling_again_with_nothing_new_posts_nothing() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    assert world.poll(issue).posted == 0


def test_a_manual_status_change_is_reverted_to_the_derived_state() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.pm.set_status(issue, "on_staging")  # someone drags the card
    result = world.poll(issue)
    assert world.pm.get_status(issue) == "inbox"
    assert result.status_reverted is True


# --- commands -----------------------------------------------------------------------------------


def test_dispatch_by_an_approver_moves_the_issue_to_triage() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    result = world.poll(issue)
    assert result.status is Status.TRIAGE
    assert world.pm.get_status(issue) == "triage"
    assert result.rejected_commands == []


def test_dispatch_by_someone_without_a_role_changes_nothing_and_says_why() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(MALLORY, issue, "/dispatch")
    result = world.poll(issue)
    assert result.status is Status.INBOX
    assert len(result.rejected_commands) == 1 and "approver" in result.rejected_commands[0]


def test_an_edited_command_is_not_accepted() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    comment_id = world.say(ALICE, issue, "hmm")
    world.fake.tick(5)
    world.fake.client(ALICE).edit_comment(comment_id, "/dispatch")
    result = world.poll(issue)
    assert result.status is Status.INBOX
    assert "edited" in result.rejected_commands[0]


# --- handoffs -----------------------------------------------------------------------------------


def test_a_handoff_from_a_run_with_no_assignment_is_ignored() -> None:
    world = World()
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    world.handoff(issue, "r-77", "triage", "spec")
    result = world.poll(issue)
    assert result.status is Status.TRIAGE
    assert len(result.ignored_handoffs) == 1


def test_a_handoff_that_fails_validation_is_reported_and_not_accepted() -> None:
    world = World()
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "spec", commit_sha="abc")  # triage reports no commit
    result = world.poll(issue)
    assert result.status is Status.TRIAGE
    assert len(result.invalid_handoffs) == 1


def test_a_wrong_spec_hash_does_not_approve_the_spec() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    world.say(ALICE, issue, "/approve spec deadbeef")
    result = world.poll(issue)
    assert result.status is Status.SPEC_APPROVAL
    assert len(result.rejected_commands) == 1


# --- trust --------------------------------------------------------------------------------------


def test_tampering_with_an_early_event_blocks_the_issue_and_posts_nothing() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    # the PM account edits its first event comment: edits are visible, so seq 1 stops counting
    first = next(c for c in world.pm.list_comments(issue) if c.author.user_id == 900)
    world.fake.tick(3)
    world.pm.edit_comment(first.id, first.body)
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert result.blocked_reason is not None and "seq 1" in result.blocked_reason
    assert result.posted == 0
    assert world.pm.get_status(issue) == "blocked"


def test_removing_the_latest_events_is_detected() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    latest = [c for c in world.pm.list_comments(issue) if c.author.user_id == 900][-1]
    world.pm.delete_comment(latest.id)
    assert world.poll(issue).status is Status.BLOCKED


# --- the first end-to-end lifecycle, no agent ----------------------------------------------------


def test_an_issue_travels_from_inbox_to_uat() -> None:
    world = World()
    issue = world.new_issue()
    _spec_commit, spec_hash = to_implementing(world, issue)

    head = world.pm.get_ref(f"pm/{issue}")
    run = world.assign(issue, head=head)
    code_commit = world.wip_commit(issue, run, {"backend/export.py": "def export(): ..."})
    world.handoff(
        issue,
        run,
        "implementing",
        "done",
        commit_sha=code_commit,
        artifact_rev=spec_hash,
        produced=["backend/export.py"],
        run_commits=[code_commit],
    )
    assert world.poll(issue).status is Status.IN_REVIEW

    for reviewer in REVIEWERS:
        run = world.assign(issue, head=code_commit, reviewer=reviewer)
        world.handoff(
            issue,
            run,
            "in_review",
            "clean",
            reviewer=reviewer,
            commit_sha=code_commit,
            artifact_rev=spec_hash,
        )
    result = world.poll(issue)
    assert result.status is Status.UAT
    assert world.pm.get_status(issue) == "uat"


def test_review_waits_for_both_reviewers() -> None:
    world = World()
    issue = world.new_issue()
    _sha, spec_hash = to_implementing(world, issue)
    head = world.pm.get_ref(f"pm/{issue}")
    run = world.assign(issue, head=head)
    code_commit = world.wip_commit(issue, run, {"backend/export.py": "x"})
    world.handoff(
        issue,
        run,
        "implementing",
        "done",
        commit_sha=code_commit,
        artifact_rev=spec_hash,
        run_commits=[code_commit],
    )
    world.poll(issue)
    run = world.assign(issue, head=code_commit)
    world.handoff(
        issue,
        run,
        "in_review",
        "clean",
        reviewer="code",
        commit_sha=code_commit,
        artifact_rev=spec_hash,
    )
    assert world.poll(issue).status is Status.IN_REVIEW


def test_changing_the_spec_during_implementation_sends_the_issue_back_for_approval() -> None:
    world = World()
    issue = world.new_issue()
    _sha, spec_hash = to_implementing(world, issue)
    head = world.pm.get_ref(f"pm/{issue}")
    run = world.assign(issue, head=head)
    drifted = world.wip_commit(issue, run, {"docs/specs/1/spec.md": "# spec, but different"})
    world.handoff(
        issue,
        run,
        "implementing",
        "done",
        commit_sha=drifted,
        artifact_rev=spec_hash,
        run_commits=[drifted],
    )
    assert world.poll(issue).status is Status.SPEC_APPROVAL


# --- unreadable logs and wasted work -------------------------------------------------------------


class SpyFacts:
    """Counts how often the loop reads repository facts."""

    def __init__(self, inner: GitFacts) -> None:
        self.inner = inner
        self.calls = 0

    def facts(self, issue: int, handoff: Handoff, state: IssueState, run_start_head: str | None):
        self.calls += 1
        return self.inner.facts(issue, handoff, state, run_start_head)


def test_an_unreadable_pm_event_block_blocks_the_issue() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.pm.add_comment(issue, "```pm-event\n: : not yaml [\n```")
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert result.blocked_reason is not None and "unreadable" in result.blocked_reason
    assert result.posted == 0


def test_a_blocked_issue_does_not_read_repository_facts_for_pending_handoffs() -> None:
    world = World()
    spy = SpyFacts(world.facts)
    world.facts = spy  # type: ignore[assignment]
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "spec")
    first = next(c for c in world.pm.list_comments(issue) if c.author.user_id == 900)
    world.fake.tick(3)
    world.pm.edit_comment(first.id, first.body)  # the log is now untrustworthy
    assert world.poll(issue).status is Status.BLOCKED
    assert spy.calls == 0


def test_a_handoff_for_another_state_is_left_alone_without_reading_facts() -> None:
    world = World()
    spy = SpyFacts(world.facts)
    world.facts = spy  # type: ignore[assignment]
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)  # the issue is in triage
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "implementing", "done", commit_sha="abc", artifact_rev="x")
    result = world.poll(issue)
    assert result.status is Status.TRIAGE
    assert spy.calls == 0 and result.ignored_handoffs == [] and result.invalid_handoffs == []
