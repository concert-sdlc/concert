"""Issues wait on other issues without being blocked (design: Issue dependencies)."""

from dataclasses import replace

from concert.core.derive import IssueState
from concert.core.transitions import Status
from concert.runner.cycle import run_cycle
from concert.runner.poll import PollResult, poll_issue
from tests.runner.helpers import ALICE, CONFIG_AUTO, World
from tests.runner.test_evidence import POLICY, to_human_uat
from tests.runner.test_poll import to_implementing, to_spec_approval


def poll_with(
    world: World, issue: int, others: dict[int, IssueState], head: bool = True
) -> PollResult:
    world.fake.tick(1)
    return poll_issue(
        world.pm,
        world.mirror,
        issue,
        world.config,
        world.facts,
        now=world.fake.now,
        others=others,
        at_queue_head=head,
    )


def settle(
    world: World, issue: int, others: dict[int, IssueState], head: bool = True
) -> PollResult:
    """Poll until each queued command has been taken (the PM takes one command per poll)."""
    result = poll_with(world, issue, others, head)
    for _ in range(3):
        result = poll_with(world, issue, others, head)
    return result


def on_staging() -> IssueState:
    return IssueState(status=Status.ON_STAGING)


def test_depends_on_records_a_dependency_of_the_chosen_kind() -> None:
    world = World()
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.poll(second)
    world.say(ALICE, second, "/dispatch")
    world.say(ALICE, second, f"/depends-on #{first} merge")
    settle(world, second, {first: IssueState()})
    assert [(d.issue, d.kind) for d in world.state(second).dependencies] == [(first, "merge")]


def test_refused_dependencies_say_why_and_change_nothing() -> None:
    world = World()
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.poll(second)
    world.say(ALICE, second, f"/depends-on #{second}")
    world.say(ALICE, second, "/depends-on #999")
    world.say(ALICE, second, "/depends-on nonsense")
    result = settle(world, second, {first: IssueState()})
    assert world.state(second).dependencies == ()
    reasons = " | ".join(result.rejected_commands)
    assert "itself" in reasons and "does not exist" in reasons and "as #N" in reasons


def test_a_cycle_is_refused() -> None:
    world = World()
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.poll(second)
    world.say(ALICE, first, f"/depends-on #{second}")
    poll_with(world, first, {second: IssueState()})
    world.say(ALICE, second, f"/depends-on #{first}")
    others = {first: world.state(first)}
    result = poll_with(world, second, others)
    assert world.state(second).dependencies == ()
    assert any("cycle" in r for r in result.rejected_commands)


def test_a_start_dependency_holds_back_runs_until_the_prerequisite_is_on_staging() -> None:
    world = World(auto=True)
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.poll(second)
    world.say(ALICE, second, f"/depends-on #{first}")
    world.say(ALICE, second, "/dispatch")
    world.heartbeat()
    settle(world, second, {first: IssueState(status=Status.IMPLEMENTING)})
    assert world.state(second).status is Status.TRIAGE and world.active(second) == []
    world.heartbeat()
    result = poll_with(world, second, {first: on_staging()})
    assert len(world.active(second)) == 1 and result.waiting_on == ()


def test_the_waiting_issue_reports_what_it_waits_on_and_is_not_blocked() -> None:
    world = World(auto=True)
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.poll(second)
    world.say(ALICE, second, f"/depends-on #{first}")
    world.say(ALICE, second, "/dispatch")
    world.heartbeat()
    result = settle(world, second, {first: IssueState(status=Status.SPEC)})
    assert result.waiting_on == (first,) and result.status is not Status.BLOCKED


def test_a_dependency_never_blocks_the_issues_own_spec_kit() -> None:
    world = World()
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.say(ALICE, second, f"/depends-on #{first}")
    settle(world, second, {first: IssueState()})
    to_spec_approval(world, second)
    kits = [c for c in world.pm.list_comments(second) if "pm-kit:spec" in c.body]
    assert len(kits) == 1


def test_a_merge_dependency_keeps_a_ready_issue_out_of_the_queue_until_the_prerequisite_lands() -> (
    None
):
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    head = to_human_uat(world, second)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.say(ALICE, second, f"/depends-on #{first} merge")
    world.say(ALICE, second, f"/approve uat {head}")
    waiting = settle(world, second, {first: IssueState(status=Status.IN_REVIEW)}, head=False)
    assert waiting.status is Status.HUMAN_UAT and not waiting.ready_for_integration
    done = settle(world, second, {first: on_staging()}, head=False)
    assert done.status is Status.HUMAN_UAT  # ready, but integration is the cycle's decision
    assert done.ready_for_integration


def test_a_waiting_issue_does_not_stall_the_queue_for_the_others() -> None:
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    never, waiting, free = world.new_issue(), world.new_issue(), world.new_issue()
    world.poll(never)
    heads: dict[int, str] = {}
    for issue in (waiting, free):
        heads[issue] = to_human_uat(world, issue)
        world.fake.add_check_run(heads[issue], "tests", "success", "w1")
    world.say(ALICE, waiting, f"/depends-on #{never} merge")
    for issue, head in heads.items():
        world.say(ALICE, issue, f"/approve uat {head}")
    world.fake.tick(1)
    result = run_cycle(
        world.pm,
        world.mirror,
        [never, waiting, free],
        world.config,
        world.facts,
        now=world.fake.now,
    )
    assert result.head == free
    assert world.state(free).status is Status.INTEGRATING
    assert world.state(waiting).status is Status.HUMAN_UAT


def test_blocked_by_in_triage_waits_without_leaving_triage() -> None:
    world = World(auto=True)
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.poll(second)
    world.say(ALICE, second, "/dispatch")
    poll_with(world, second, {first: IssueState(status=Status.SPEC)})
    run = world.assign(second, head=None)
    world.handoff(
        second, run, "triage", "blocked_by", depends_on=[{"issue": first, "kind": "start"}]
    )
    result = poll_with(world, second, {first: IssueState(status=Status.SPEC)})
    assert result.status is Status.TRIAGE and result.waiting_on == (first,)
    assert world.state(second).counters is not None
    later = poll_with(world, second, {first: on_staging()})
    assert later.waiting_on == ()


def test_an_agent_cannot_make_an_issue_wait_on_one_that_does_not_exist() -> None:
    world = World()
    only = world.new_issue()
    world.poll(only)
    world.say(ALICE, only, "/dispatch")
    poll_with(world, only, {})
    run = world.assign(only, head=None)
    world.handoff(only, run, "triage", "spec", depends_on=[{"issue": 4242, "kind": "start"}])
    poll_with(world, only, {})
    assert world.state(only).dependencies == ()
    assert world.state(only).status is Status.SPEC


def test_implementation_resumes_after_the_prerequisite_lands() -> None:
    world = World(auto=True)
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    to_implementing(world, second)
    world.say(ALICE, second, f"/depends-on #{first}")
    world.config = replace(world.config, runners=CONFIG_AUTO.runners)
    world.heartbeat()
    poll_with(world, second, {first: IssueState(status=Status.IMPLEMENTING)})
    assert world.active(second) == []
    world.heartbeat()
    poll_with(world, second, {first: on_staging()})
    assert len(world.active(second)) == 1


def test_a_waiting_issue_still_gets_its_spec_written() -> None:
    world = World(auto=True)
    first, second = world.new_issue(), world.new_issue()
    world.poll(first)
    world.say(ALICE, second, f"/depends-on #{first}")
    world.say(ALICE, second, "/dispatch")
    world.heartbeat()
    others = {first: IssueState(status=Status.IMPLEMENTING)}
    settle(world, second, others)
    run = world.active(second)
    assert run == []  # in triage the wait holds (start-after: no steps)
    on_first = {first: on_staging()}
    world.heartbeat()
    poll_with(world, second, on_first)
    [triage] = world.active(second)
    world.handoff(second, triage.run_id, "triage", "spec")
    world.heartbeat(triage.run_id)
    poll_with(world, second, others)  # the prerequisite is (again) unfinished: spec work goes on
    world.heartbeat()
    poll_with(world, second, others)
    assert world.state(second).status is Status.SPEC
    assert [r.state for r in world.active(second)] == ["spec"]
