"""verifying_staging: the run starts on the merge commit; a pass puts the issue on staging."""

from dataclasses import replace

from concert.core.transitions import Status
from tests.runner.helpers import CONFIG_AUTO, World
from tests.runner.test_merge import integrating_world, report_ready_to_merge


def merged_world() -> tuple[World, int, str]:
    world, issue, head = integrating_world()
    report_ready_to_merge(world, issue, head)
    assert world.poll(issue).status is Status.VERIFYING_STAGING
    merge = world.state(issue).merge_sha
    assert merge is not None
    return world, issue, merge


def test_the_verification_run_starts_on_the_merge_commit() -> None:
    world, issue, merge = merged_world()
    world.config = replace(world.config, runners=CONFIG_AUTO.runners)
    world.heartbeat()
    world.poll(issue)
    [run] = world.active(issue)
    assert run.state == "verifying_staging" and run.head == merge


def test_a_passing_verification_on_the_merge_commit_puts_the_issue_on_staging() -> None:
    world, issue, merge = merged_world()
    run = world.assign(issue, head=merge)
    world.handoff(
        issue,
        run,
        "verifying_staging",
        "passed",
        commit_sha=merge,
        artifact_rev=world.state(issue).approved_spec_rev,
    )
    result = world.poll(issue)
    assert result.status is Status.ON_STAGING
    assert world.pm.get_status(issue) == "on_staging"


def test_a_pass_reported_for_another_commit_is_not_accepted() -> None:
    world, issue, merge = merged_world()
    run = world.assign(issue, head=merge)
    world.handoff(
        issue,
        run,
        "verifying_staging",
        "passed",
        commit_sha="0" * 40,
        artifact_rev=world.state(issue).approved_spec_rev,
    )
    result = world.poll(issue)
    assert result.status is Status.VERIFYING_STAGING
    assert len(result.invalid_handoffs) == 1


def fail_verification(world: World, issue: int, merge: str) -> None:
    run = world.assign(issue, head=merge)
    world.handoff(
        issue,
        run,
        "verifying_staging",
        "failed",
        commit_sha=merge,
        artifact_rev=world.state(issue).approved_spec_rev,
    )


def test_a_failed_verification_is_re_run_once_before_the_issue_is_frozen() -> None:
    world, issue, merge = merged_world()
    fail_verification(world, issue, merge)
    assert world.poll(issue).status is Status.VERIFYING_STAGING
    assert world.state(issue).verify_failures == 1
    # the re-run is assigned and fails too
    fail_verification(world, issue, merge)
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert world.pm.get_status(issue) == "blocked"


def test_under_auto_revert_a_final_failure_does_not_block_a_code_issue() -> None:
    world, issue, merge = merged_world()
    world.config = replace(world.config, on_failure="auto_revert")
    fail_verification(world, issue, merge)
    world.poll(issue)
    fail_verification(world, issue, merge)
    result = world.poll(issue)
    assert result.status is Status.VERIFYING_STAGING
    assert world.state(issue).verify_failed


def test_the_number_of_re_runs_comes_from_the_config() -> None:
    world, issue, merge = merged_world()
    world.config = replace(world.config, verify_reruns=0)
    fail_verification(world, issue, merge)
    assert world.poll(issue).status is Status.BLOCKED
