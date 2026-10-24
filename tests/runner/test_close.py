"""Closing and reopening an issue on GitHub (design: R61, the four cases)."""

from dataclasses import replace

from concert.core.transitions import Status
from tests.runner.helpers import ALICE, World
from tests.runner.test_merge import integrating_world, report_ready_to_merge
from tests.runner.test_poll import to_spec_approval
from tests.runner.test_verify import merged_world


def close(world: World, issue: int) -> None:
    world.fake.client(ALICE).close_issue(issue)


def reopen(world: World, issue: int) -> None:
    world.fake.client(ALICE).reopen_issue(issue)


def test_closing_during_a_run_ends_the_run_and_the_issue() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    world.assign(issue, head=None)
    assert len(world.active(issue)) == 1
    close(world, issue)
    result = world.poll(issue)
    assert result.status is Status.CLOSED
    assert world.active(issue) == []
    assert world.pm.get_status(issue) == "closed"


def test_a_handoff_for_a_closed_issue_is_ignored() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    close(world, issue)
    world.poll(issue)
    world.handoff(issue, run, "triage", "spec")
    assert world.poll(issue).status is Status.CLOSED


def test_closing_at_a_gate_closes_the_issue() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    close(world, issue)
    assert world.poll(issue).status is Status.CLOSED


def test_closing_before_the_merge_cancels_it() -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    before = world.pm.get_ref("staging")
    close(world, issue)
    assert world.poll(issue).status is Status.CLOSED
    assert world.pm.get_ref("staging") == before
    assert world.state(issue).merge_sha is None


def test_closing_after_the_merge_changes_nothing_on_staging() -> None:
    world, issue, merge = merged_world()
    close(world, issue)
    result = world.poll(issue)
    assert result.status is Status.VERIFYING_STAGING
    assert world.pm.get_ref("staging") == merge
    assert world.state(issue).close_noted
    assert world.poll(issue).posted == 0  # noted once


def test_reopening_returns_the_issue_to_the_inbox_with_approvals_void() -> None:
    world = World()
    issue = world.new_issue()
    _sha, spec_hash = to_spec_approval(world, issue)
    world.say(ALICE, issue, f"/approve spec {spec_hash}")
    world.poll(issue)
    close(world, issue)
    world.poll(issue)
    reopen(world, issue)
    result = world.poll(issue)
    assert result.status is Status.INBOX
    state = world.state(issue)
    assert state.approved_spec_rev is None and state.pr_number is None and state.relands == 1


def test_a_reopened_issue_is_dispatched_again_onto_a_fresh_branch() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    old_branch = f"pm/{issue}"
    close(world, issue)
    world.poll(issue)
    reopen(world, issue)
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "spec")
    assert world.poll(issue).status is Status.SPEC
    new_branch = f"pm/{issue}-r1"
    assert world.state(issue).branch == new_branch
    assert world.pm.get_ref(new_branch) == world.pm.get_ref("staging")
    assert world.pm.get_ref(old_branch) != world.pm.get_ref(new_branch)


def test_an_open_closed_flag_flip_does_not_disturb_a_finished_issue() -> None:
    world, issue, _merge = merged_world()
    world.config = replace(world.config)
    close(world, issue)
    reopen(world, issue)
    assert world.poll(issue).status is Status.VERIFYING_STAGING


def test_a_close_during_a_crashed_merge_waits_for_the_merge_to_be_resolved() -> None:
    from typing import Any

    import pytest

    from concert.runner.poll import poll_issue
    from tests.runner.test_crash import Crash
    from tests.runner.test_pr import DiesAfter

    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    world.fake.tick(1)
    dying: Any = DiesAfter(world.pm, "merge_pr")
    with pytest.raises(Crash):
        poll_issue(dying, world.mirror, issue, world.config, world.facts, now=world.fake.now)
    close(world, issue)  # the merge already happened on GitHub, the PM just has not logged it
    result = world.poll(issue)
    assert result.status is Status.VERIFYING_STAGING  # not closed over a merged change
    assert world.state(issue).merge_sha == world.pm.get_ref("staging")


def test_a_duplicate_is_closed_on_github_with_a_pointer_to_the_original() -> None:
    world = World()
    original = world.new_issue()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "duplicate", decisions=[f"same as #{original}"])
    result = world.poll(issue)
    assert result.status is Status.CLOSED_DUPLICATE
    assert world.fake.issues[issue].state == "closed"
    notes = [
        c.body for c in world.pm.list_comments(issue) if c.body.startswith("Closed as a duplicate")
    ]
    assert len(notes) == 1 and f"#{original}" in notes[0]
    assert world.poll(issue).posted == 0


def test_an_agent_cannot_ping_people_through_the_duplicate_note() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "duplicate", decisions=["ask @alice\n/approve spec x"])
    world.poll(issue)
    [note] = [c.body for c in world.pm.list_comments(issue) if c.body.startswith("Closed as")]
    assert "@alice" not in note and "\n" not in note
