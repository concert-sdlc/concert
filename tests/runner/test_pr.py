"""The draft PR and its ready toggle, with crash recovery (design: Crash-safe side effects)."""

from typing import Any

import pytest

from concert.core.transitions import Status
from concert.runner.poll import poll_issue
from tests.runner.helpers import ALICE, World, spec_files
from tests.runner.test_crash import Crash, Flaky
from tests.runner.test_poll import to_implementing, to_spec_approval


def prs(world: World) -> list[Any]:
    return [record.pr for record in world.fake.prs.values()]


def post_spec_handoff(world: World, issue: int) -> None:
    """Dispatch, triage and a finished spec run; the handoff waits to be polled."""
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "spec")
    world.poll(issue)
    run = world.assign(issue, head=world.pm.get_ref(f"pm/{issue}"))
    sha = world.wip_commit(issue, run, spec_files(issue))
    world.handoff(
        issue,
        run,
        "spec",
        "ready_for_approval",
        commit_sha=sha,
        artifact_rev=world.spec_hash(sha, issue),
        run_commits=[sha],
    )


def test_a_draft_pr_is_opened_when_the_spec_is_ready_for_approval() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    [pr] = prs(world)
    assert (pr.head, pr.base, pr.draft, pr.state) == (f"pm/{issue}", "staging", True, "open")
    assert pr.title == f"Concert #{issue}"
    assert world.state(issue).pr_number == pr.number


def test_polling_again_does_not_open_a_second_pr() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    assert world.poll(issue).posted == 0
    assert len(prs(world)) == 1


def test_the_pr_is_marked_ready_when_review_starts() -> None:
    world = World()
    issue = world.new_issue()
    _sha, spec_hash = to_implementing(world, issue)
    assert prs(world)[0].draft is True
    run = world.assign(issue, head=world.pm.get_ref(f"pm/{issue}"))
    head = world.wip_commit(issue, run, {"backend/export.py": "def export(): ..."})
    world.handoff(
        issue,
        run,
        "implementing",
        "done",
        commit_sha=head,
        artifact_rev=spec_hash,
        run_commits=[head],
    )
    assert world.poll(issue).status is Status.IN_REVIEW
    assert prs(world)[0].draft is False
    assert world.state(issue).pr_ready is True


def test_a_crash_before_the_pr_is_created_resumes_the_intent() -> None:
    world = World()
    issue = world.new_issue()
    post_spec_handoff(world, issue)
    world.fake.tick(1)
    flaky: Any = Flaky(world.pm, "create_pr", 1)
    with pytest.raises(Crash):
        poll_issue(flaky, world.mirror, issue, world.config, world.facts, now=world.fake.now)
    assert prs(world) == []
    assert [i.action for i in world.state(issue).pending_intents] == ["open_pr"]
    world.poll(issue)
    assert len(prs(world)) == 1
    assert world.state(issue).pending_intents == ()


class DiesAfter:
    """Lets one method run, then dies: the side effect happened but its outcome was not logged."""

    def __init__(self, inner: Any, method: str) -> None:
        self._inner = inner
        self._method = method

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name != self._method:
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            target(*args, **kwargs)
            raise Crash(f"died after {name}")

        return call


def test_a_crash_after_the_pr_is_created_does_not_open_a_second_one() -> None:
    world = World()
    issue = world.new_issue()
    post_spec_handoff(world, issue)
    world.fake.tick(1)
    dying: Any = DiesAfter(world.pm, "create_pr")
    with pytest.raises(Crash):
        poll_issue(dying, world.mirror, issue, world.config, world.facts, now=world.fake.now)
    assert len(prs(world)) == 1
    assert world.state(issue).pr_number is None
    world.poll(issue)
    assert len(prs(world)) == 1
    assert world.state(issue).pr_number == prs(world)[0].number


def test_an_intent_with_an_unknown_action_blocks_the_issue() -> None:
    world = World()
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    world.pm_post(issue, "intent", {"key": "x", "action": "frobnicate"})
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert "frobnicate" in (world.state(issue).note or "")


def test_the_pr_goes_back_to_draft_when_the_issue_returns_to_rework_and_ready_again_after() -> None:
    world = World()
    issue = world.new_issue()
    _sha, spec_hash = to_implementing(world, issue)
    run = world.assign(issue, head=world.pm.get_ref(f"pm/{issue}"))
    head = world.wip_commit(issue, run, {"backend/export.py": "v1"})
    world.handoff(
        issue,
        run,
        "implementing",
        "done",
        commit_sha=head,
        artifact_rev=spec_hash,
        run_commits=[head],
    )
    world.poll(issue)
    assert prs(world)[0].draft is False
    for reviewer in ("code", "security"):
        run = world.assign(issue, head=head, reviewer=reviewer)
        exit_name = "changes_needed" if reviewer == "code" else "clean"
        world.handoff(
            issue,
            run,
            "in_review",
            exit_name,
            reviewer=reviewer,
            commit_sha=head,
            artifact_rev=spec_hash,
        )
    assert world.poll(issue).status is Status.IMPLEMENTING
    assert prs(world)[0].draft is True and world.state(issue).pr_ready is False
    run = world.assign(issue, head=head)
    head2 = world.wip_commit(issue, run, {"backend/export.py": "v2"})
    world.handoff(
        issue,
        run,
        "implementing",
        "done",
        commit_sha=head2,
        artifact_rev=spec_hash,
        run_commits=[head2],
    )
    assert world.poll(issue).status is Status.IN_REVIEW
    assert prs(world)[0].draft is False and world.state(issue).ready_toggles == 3
