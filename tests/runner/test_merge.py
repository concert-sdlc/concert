"""The PM merges the PR at the expected head once the gate holds (design: integrating)."""

from dataclasses import replace
from typing import Any

import pytest

from concert.core.transitions import Status
from concert.github.api import Conflict, StaleHead
from concert.runner.poll import poll_issue
from tests.runner.helpers import ALICE, World
from tests.runner.test_crash import Crash
from tests.runner.test_evidence import POLICY, to_human_uat
from tests.runner.test_pr import DiesAfter


def integrating_world() -> tuple[World, int, str]:
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.say(ALICE, issue, f"/approve uat {head}")
    assert world.poll(issue).status is Status.INTEGRATING
    return world, issue, head


def report_ready_to_merge(world: World, issue: int, head: str) -> None:
    run = world.assign(issue, head=head)
    world.handoff(
        issue,
        run,
        "integrating",
        "ready_to_merge",
        commit_sha=head,
        artifact_rev=world.state(issue).approved_spec_rev,
    )


def test_the_pr_is_merged_at_the_expected_head_and_verification_starts() -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    result = world.poll(issue)
    assert result.status is Status.VERIFYING_STAGING
    state = world.state(issue)
    staging = world.pm.get_ref("staging")
    assert staging is not None and state.merge_sha == staging
    assert world.pm.read_tree(staging)["backend/export.py"] == "def export(): ..."
    assert world.fake.prs[state.pr_number or 0].pr.state == "merged"


def test_nothing_is_merged_before_the_integration_run_reports() -> None:
    world, issue, _head = integrating_world()
    before = world.pm.get_ref("staging")
    assert world.poll(issue).status is Status.INTEGRATING
    assert world.pm.get_ref("staging") == before


def test_a_failing_check_on_the_head_stops_the_merge() -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    world.fake.add_check_run(head, "tests", "failure", "w1")
    before = world.pm.get_ref("staging")
    assert world.poll(issue).status is Status.INTEGRATING
    assert world.pm.get_ref("staging") == before


@pytest.mark.parametrize("error", [StaleHead("head moved"), Conflict("merge conflict in a.py")])
def test_a_refused_merge_blocks_the_issue_and_closes_the_intent(error: Exception) -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)

    class Refuses:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            if name != "merge_pr":
                return getattr(self._inner, name)

            def call(*_args: Any, **_kwargs: Any) -> Any:
                raise error

            return call

    world.fake.tick(1)
    refusing: Any = Refuses(world.pm)
    result = poll_issue(
        refusing, world.mirror, issue, world.config, world.facts, now=world.fake.now
    )
    assert result.status is Status.BLOCKED
    assert str(error) in (world.state(issue).note or "")
    assert world.state(issue).pending_intents == ()
    assert world.state(issue).merge_sha is None


def test_a_crash_after_the_merge_resumes_without_merging_twice() -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    world.fake.tick(1)
    dying: Any = DiesAfter(world.pm, "merge_pr")
    with pytest.raises(Crash):
        poll_issue(dying, world.mirror, issue, world.config, world.facts, now=world.fake.now)
    merged = world.pm.get_ref("staging")
    assert world.state(issue).merge_sha is None
    assert world.poll(issue).status is Status.VERIFYING_STAGING
    assert world.pm.get_ref("staging") == merged  # no second merge commit
    assert world.state(issue).merge_sha == merged


def test_a_pr_closed_without_merging_blocks_the_issue() -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    number = world.state(issue).pr_number or 0
    world.fake.prs[number].pr = replace(world.fake.prs[number].pr, state="closed")
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert "closed" in (world.state(issue).note or "")


def test_a_merged_pr_with_no_merge_commit_blocks_the_issue() -> None:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    number = world.state(issue).pr_number or 0
    world.fake.prs[number].pr = replace(world.fake.prs[number].pr, state="merged")
    assert world.poll(issue).status is Status.BLOCKED
