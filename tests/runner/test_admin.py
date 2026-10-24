"""The Steering issue: admins steer the project with /pm commands."""

from dataclasses import replace

from concert.core.admin import AdminState
from concert.core.commands import Role
from concert.core.derive import IssueState
from concert.core.transitions import Status
from concert.runner.admin import process_steering
from tests.runner.helpers import ALICE, MALLORY, PM, World


def steering(world: World) -> int:
    return world.new_issue()


def run(world: World, issue: int, status_text: str = "board") -> AdminState:
    world.fake.tick(1)
    return process_steering(
        world.pm,
        world.mirror,
        issue,
        replace(world.config, roles={**world.config.roles, ALICE.user_id: Role.ADMIN}),
        lambda target: f"{status_text} {target}",
    )


def replies(world: World, issue: int) -> list[str]:
    return [
        c.body
        for c in world.pm.list_comments(issue)
        if c.author.user_id == PM.user_id and not c.body.startswith("<details>")
    ]


def test_an_admins_pause_is_recorded_acknowledged_and_remembered() -> None:
    world = World()
    issue = steering(world)
    world.say(ALICE, issue, "/pm pause")
    state = run(world, issue)
    assert state.paused
    assert any("paused" in r.lower() for r in replies(world, issue))
    assert run(world, issue).paused  # a restart re-derives it
    assert len(replies(world, issue)) == 1  # and does not answer twice


def test_resume_lifts_the_pause() -> None:
    world = World()
    issue = steering(world)
    world.say(ALICE, issue, "/pm pause")
    run(world, issue)
    world.say(ALICE, issue, "/pm resume")
    assert not run(world, issue).paused


def test_a_command_from_someone_without_the_role_is_refused_once_with_the_reason() -> None:
    world = World()
    issue = steering(world)
    world.say(MALLORY, issue, "/pm pause")
    state = run(world, issue)
    assert not state.paused
    run(world, issue)
    [reply] = replies(world, issue)
    assert "role" in reply


def test_a_malformed_command_is_refused_once() -> None:
    world = World()
    issue = steering(world)
    world.say(ALICE, issue, "/pm reset #5")
    run(world, issue)
    run(world, issue)
    [reply] = replies(world, issue)
    assert "reset needs" in reply


def test_an_edited_command_is_not_obeyed() -> None:
    world = World()
    issue = steering(world)
    comment = world.say(ALICE, issue, "/pm status")
    world.fake.tick(3)
    world.fake.client(ALICE).edit_comment(comment, "/pm pause")
    assert not run(world, issue).paused


def test_reset_is_queued_for_the_issue_it_names() -> None:
    world = World()
    issue = steering(world)
    world.say(ALICE, issue, "/pm reset #7 spec")
    assert run(world, issue).resets == {7: Status.SPEC}


def test_status_replies_with_the_board() -> None:
    world = World()
    issue = steering(world)
    world.say(ALICE, issue, "/pm status #7")
    run(world, issue, status_text="BOARD")
    assert replies(world, issue) == ["BOARD 7"]


def test_an_applied_reset_is_forgotten() -> None:
    from concert.runner.admin import mark_reset_applied

    world = World()
    issue = steering(world)
    world.say(ALICE, issue, "/pm reset #7 spec")
    state = run(world, issue)
    mark_reset_applied(world.pm, world.mirror, issue, world.config, 7, now=world.fake.now)
    assert state.resets and run(world, issue).resets == {}


def test_the_steering_issue_state_is_just_a_log() -> None:
    world = World()
    issue = steering(world)
    run(world, issue)
    assert isinstance(world.state(issue), IssueState)
