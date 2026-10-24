"""What /pm pause and /pm reset do to an issue's poll."""

from dataclasses import replace

from concert.core.admin import AdminState
from concert.core.transitions import Status
from concert.runner.cycle import run_cycle
from concert.runner.poll import PollResult, poll_issue
from tests.runner.helpers import ALICE, World
from tests.runner.test_evidence import POLICY, to_human_uat
from tests.runner.test_poll import to_spec_approval


def poll(
    world: World, issue: int, held: bool = False, reset_to: Status | None = None
) -> PollResult:
    world.fake.tick(1)
    return poll_issue(
        world.pm,
        world.mirror,
        issue,
        world.config,
        world.facts,
        now=world.fake.now,
        held=held,
        reset_to=reset_to,
    )


def test_a_held_issue_gets_no_new_run_until_released() -> None:
    world = World(auto=True)
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.heartbeat()
    poll(world, issue, held=True)
    poll(world, issue, held=True)
    assert world.state(issue).status is Status.TRIAGE and world.active(issue) == []
    world.heartbeat()
    poll(world, issue)
    assert len(world.active(issue)) == 1


def test_a_held_issue_does_not_take_the_integration_queue() -> None:
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.say(ALICE, issue, f"/approve uat {head}")
    held = poll(world, issue, held=True)
    assert held.status is Status.HUMAN_UAT and not held.ready_for_integration


def test_a_reset_moves_the_issue_and_reports_it_was_applied() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    result = poll(world, issue, reset_to=Status.SPEC)
    assert result.status is Status.SPEC and result.reset_handled
    assert world.pm.get_status(issue) == "spec"
    assert world.state(issue).approved_spec_rev is None


def test_a_reset_to_the_current_state_is_handled_without_events() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    again = poll(world, issue, reset_to=Status.SPEC_APPROVAL)
    assert again.reset_handled and again.status is Status.SPEC_APPROVAL


def test_a_reset_of_a_finished_issue_is_dropped() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "duplicate")
    world.poll(issue)
    result = poll(world, issue, reset_to=Status.TRIAGE)
    assert result.status is Status.CLOSED_DUPLICATE and result.reset_handled


def test_a_blocked_issue_can_be_reset_out_of_its_block() -> None:
    world = World()
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    world.pm_post(
        issue,
        "transition",
        {"from": "triage", "to": "blocked", "trigger": "check_failed", "note": "x"},
    )
    assert world.poll(issue).status is Status.BLOCKED
    assert poll(world, issue, reset_to=Status.TRIAGE).status is Status.TRIAGE


def test_the_cycle_applies_pauses_and_resets_from_the_admin_state() -> None:
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    first, second = world.new_issue(), world.new_issue()
    heads: dict[int, str] = {}
    for issue in (first, second):
        heads[issue] = to_human_uat(world, issue)
        world.fake.add_check_run(heads[issue], "tests", "success", "w1")
        world.say(ALICE, issue, f"/approve uat {heads[issue]}")
    world.fake.tick(1)
    admin = AdminState(paused_issues=frozenset({first}))
    result = run_cycle(
        world.pm,
        world.mirror,
        [first, second],
        world.config,
        world.facts,
        now=world.fake.now,
        admin=admin,
    )
    assert result.head == second  # the paused issue is passed over
    assert world.state(first).status is Status.HUMAN_UAT
    admin = AdminState(paused=True)
    world.fake.tick(1)
    frozen = run_cycle(
        world.pm,
        world.mirror,
        [first, second],
        world.config,
        world.facts,
        now=world.fake.now,
        admin=admin,
    )
    assert frozen.head is None
    reset = AdminState(resets={first: Status.SPEC})
    world.fake.tick(1)
    applied = run_cycle(
        world.pm,
        world.mirror,
        [first, second],
        world.config,
        world.facts,
        now=world.fake.now,
        admin=reset,
    )
    assert applied.results[first].reset_handled and world.state(first).status is Status.SPEC
