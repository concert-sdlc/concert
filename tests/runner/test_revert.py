"""Auto-revert after a final verification failure (design: Auto-revert)."""

from dataclasses import replace
from typing import Any

import pytest

from concert.core.transitions import Status
from concert.runner.poll import poll_issue
from tests.runner.helpers import World
from tests.runner.test_crash import Crash
from tests.runner.test_pr import DiesAfter
from tests.runner.test_verify import fail_verification, merged_world


def failed_world(**config: Any) -> tuple[World, int, str]:
    world, issue, merge = merged_world()
    world.config = replace(world.config, on_failure="auto_revert", **config)
    fail_verification(world, issue, merge)
    world.poll(issue)
    fail_verification(world, issue, merge)
    return world, issue, merge


def revert_prs(world: World) -> list[Any]:
    return [r.pr for r in world.fake.prs.values() if r.pr.head.startswith("revert-pr-")]


def test_a_final_failure_opens_a_revert_pr_and_waits_for_its_checks() -> None:
    world, issue, _merge = failed_world()
    result = world.poll(issue)
    assert result.status is Status.VERIFYING_STAGING
    [pr] = revert_prs(world)
    state = world.state(issue)
    assert state.revert_pr == pr.number and state.revert_head == world.pm.get_ref(pr.head)
    assert pr.state == "open"
    assert world.poll(issue).posted == 0


def test_green_checks_on_the_revert_merge_it_and_start_verifying_the_revert() -> None:
    world, issue, _merge = failed_world()
    world.poll(issue)
    state = world.state(issue)
    assert state.revert_head is not None
    world.fake.add_check_run(state.revert_head, "tests", "success", "w1")
    world.poll(issue)
    state = world.state(issue)
    staging = world.pm.get_ref("staging")
    assert staging is not None and state.revert_merge_sha == staging
    assert "backend/export.py" not in world.pm.read_tree(staging)
    assert state.status is Status.VERIFYING_STAGING and not state.verify_failed


def test_a_verified_revert_sends_the_issue_back_to_implementing() -> None:
    world, issue, _merge = failed_world()
    world.poll(issue)
    revert_head = world.state(issue).revert_head or ""
    world.fake.add_check_run(revert_head, "tests", "success", "w1")
    world.poll(issue)
    revert_merge = world.state(issue).revert_merge_sha or ""
    run = world.assign(issue, head=revert_merge)
    world.handoff(
        issue,
        run,
        "verifying_staging",
        "passed",
        commit_sha=revert_merge,
        artifact_rev=world.state(issue).approved_spec_rev,
    )
    assert world.poll(issue).status is Status.IMPLEMENTING


def test_failing_checks_on_the_revert_freeze_the_issue_without_merging_it() -> None:
    world, issue, _merge = failed_world()
    world.poll(issue)
    world.fake.add_check_run(world.state(issue).revert_head or "", "tests", "failure", "w1")
    before = world.pm.get_ref("staging")
    assert world.poll(issue).status is Status.BLOCKED
    assert world.pm.get_ref("staging") == before


def test_a_revert_that_touches_protected_paths_is_not_merged() -> None:
    world, issue, _merge = failed_world(protected_paths=("backend/**",))
    world.poll(issue)
    world.fake.add_check_run(world.state(issue).revert_head or "", "tests", "success", "w1")
    before = world.pm.get_ref("staging")
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert "protected" in (world.state(issue).note or "")
    assert world.pm.get_ref("staging") == before


def test_a_revert_that_fails_verification_freezes_the_issue() -> None:
    world, issue, _merge = failed_world()
    world.poll(issue)
    world.fake.add_check_run(world.state(issue).revert_head or "", "tests", "success", "w1")
    world.poll(issue)
    revert_merge = world.state(issue).revert_merge_sha or ""
    for _ in range(2):
        fail_verification(world, issue, revert_merge)
        world.poll(issue)
    assert world.state(issue).status is Status.BLOCKED


def test_a_crash_after_the_revert_pr_is_opened_does_not_open_a_second() -> None:
    world, issue, _merge = failed_world()
    world.fake.tick(1)
    dying: Any = DiesAfter(world.pm, "revert_pr")
    with pytest.raises(Crash):
        poll_issue(dying, world.mirror, issue, world.config, world.facts, now=world.fake.now)
    assert len(revert_prs(world)) == 1
    world.poll(issue)
    assert len(revert_prs(world)) == 1
    assert world.state(issue).revert_pr == revert_prs(world)[0].number


def test_the_revert_verification_run_starts_on_the_revert_commit() -> None:
    from tests.runner.helpers import CONFIG_AUTO

    world, issue, _merge = failed_world()
    world.poll(issue)
    world.fake.add_check_run(world.state(issue).revert_head or "", "tests", "success", "w1")
    world.poll(issue)
    revert_merge = world.state(issue).revert_merge_sha
    world.config = replace(world.config, runners=CONFIG_AUTO.runners)
    world.heartbeat()
    world.poll(issue)
    [run] = world.active(issue)
    assert run.head == revert_merge
