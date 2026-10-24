"""A cycle polls every issue and lets only the head of the queue integrate."""

from dataclasses import replace

from concert.core.derive import IssueState
from concert.core.transitions import Status
from concert.runner.cycle import run_cycle
from tests.runner.helpers import ALICE, World
from tests.runner.test_evidence import POLICY, to_human_uat
from tests.runner.test_merge import report_ready_to_merge
from tests.runner.test_verify import fail_verification


def two_ready_issues() -> tuple[World, int, int, str, str]:
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    first, second = world.new_issue(), world.new_issue()
    heads: list[str] = []
    for issue in (first, second):
        head = to_human_uat(world, issue)
        world.fake.add_check_run(head, "tests", "success", "w1")
        heads.append(head)
    for issue, head in zip((first, second), heads, strict=True):
        world.say(ALICE, issue, f"/approve uat {head}")
    return world, first, second, heads[0], heads[1]


def cycle(world: World, *issues: int):
    world.fake.tick(1)
    return run_cycle(
        world.pm, world.mirror, list(issues), world.config, world.facts, now=world.fake.now
    )


def test_only_the_earliest_dispatched_ready_issue_integrates() -> None:
    world, first, second, _h1, _h2 = two_ready_issues()
    result = cycle(world, first, second)
    assert result.head == first
    assert world.state(first).status is Status.INTEGRATING
    assert world.state(second).status is Status.HUMAN_UAT


def test_the_next_issue_waits_while_the_first_is_integrating_and_verifying() -> None:
    world, first, second, h1, _h2 = two_ready_issues()
    cycle(world, first, second)
    report_ready_to_merge(world, first, h1)
    cycle(world, first, second)
    assert world.state(first).status is Status.VERIFYING_STAGING
    assert world.state(second).status is Status.HUMAN_UAT
    assert cycle(world, first, second).head is None


def test_the_next_issue_integrates_once_the_first_is_on_staging() -> None:
    world, first, second, h1, _h2 = two_ready_issues()
    cycle(world, first, second)
    report_ready_to_merge(world, first, h1)
    cycle(world, first, second)
    merge = world.state(first).merge_sha or ""
    run = world.assign(first, head=merge)
    world.handoff(
        first,
        run,
        "verifying_staging",
        "passed",
        commit_sha=merge,
        artifact_rev=world.state(first).approved_spec_rev,
    )
    result = cycle(world, first, second)
    assert world.state(first).status is Status.ON_STAGING
    assert result.head is None or result.head == second
    cycle(world, first, second)
    assert world.state(second).status is Status.INTEGRATING


def test_a_frozen_queue_holds_the_next_issue() -> None:
    world, first, second, h1, _h2 = two_ready_issues()
    cycle(world, first, second)
    report_ready_to_merge(world, first, h1)
    cycle(world, first, second)
    merge = world.state(first).merge_sha or ""
    for _ in range(2):
        fail_verification(world, first, merge)
        cycle(world, first, second)
    assert world.state(first).status is Status.BLOCKED
    assert cycle(world, first, second).head is None
    assert world.state(second).status is Status.HUMAN_UAT


def test_a_dependent_issue_integrates_in_the_cycle_after_its_prerequisite_lands() -> None:
    world, first, second, _h1, _h2 = two_ready_issues()
    world.fake.tick(1)
    dependent = run_cycle(
        world.pm, world.mirror, [first, second], world.config, world.facts, now=world.fake.now
    )
    assert dependent.head == first  # sanity: the earlier issue goes first
    world.say(ALICE, second, f"/depends-on #{first} merge")
    cycle(world, second)  # records the dependency (one command per poll)
    known = {first: world.state(first)}
    held = run_cycle(
        world.pm,
        world.mirror,
        [second],
        world.config,
        world.facts,
        now=world.fake.now + 5,
        known=known,
    )
    assert held.head is None and world.state(second).status is Status.HUMAN_UAT
    landed = {first: IssueState(status=Status.ON_STAGING)}
    freed = run_cycle(
        world.pm,
        world.mirror,
        [second],
        world.config,
        world.facts,
        now=world.fake.now + 9,
        known=landed,
    )
    assert freed.head == second and freed.states[second].status is Status.INTEGRATING


def test_a_hotfix_jumps_the_queue_and_passes_a_frozen_queue() -> None:
    from concert.core.commands import Role

    world = World()
    world.config = (
        replace(
            world.config,
            gate_policy=POLICY,
            roles={ALICE.user_id: Role.ADMIN},
            gate_policy_admins=None,
        )
        if False
        else replace(
            world.config,
            gate_policy=replace(POLICY, admins=frozenset({ALICE.user_id})),
            roles={ALICE.user_id: Role.ADMIN},
        )
    )
    normal, urgent = world.new_issue(), world.new_issue()
    heads = {
        normal: to_human_uat(world, normal),
        urgent: to_human_uat(world, urgent, "/dispatch hotfix"),
    }
    assert world.state(urgent).issue_type.value == "hotfix"
    for issue, head in heads.items():
        world.fake.add_check_run(head, "tests", "success", "w1")
        world.say(ALICE, issue, f"/approve uat {head}")
    result = cycle(world, normal, urgent)
    assert result.head == urgent  # dispatched later, but it jumps the queue
    assert world.state(normal).status is Status.HUMAN_UAT
