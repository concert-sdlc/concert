"""The PM posts a kit when a human gate opens, once per revision, crash-safely."""

from typing import Any

import pytest

from concert.runner.poll import poll_issue
from tests.runner.helpers import PM, World
from tests.runner.test_crash import Crash
from tests.runner.test_evidence import policy_world, to_human_uat
from tests.runner.test_poll import to_spec_approval
from tests.runner.test_pr import post_spec_handoff


def kits(world: World, issue: int) -> list[str]:
    return [
        c.body
        for c in world.pm.list_comments(issue)
        if c.author.user_id == PM.user_id and "pm-kit:" in c.body
    ]


def test_the_spec_kit_is_posted_with_the_approve_command_for_the_spec_hash() -> None:
    world = World()
    issue = world.new_issue()
    _sha, spec_hash = to_spec_approval(world, issue)
    [kit] = kits(world, issue)
    assert f"/approve spec {spec_hash}" in kit
    assert "docs/specs/" in kit  # the changed files


def test_polling_again_does_not_post_the_kit_twice() -> None:
    world = World()
    issue = world.new_issue()
    to_spec_approval(world, issue)
    world.poll(issue)
    assert len(kits(world, issue)) == 1


def test_the_uat_kit_is_bound_to_the_head_and_lists_the_evidence() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.poll(issue)
    uat = [k for k in kits(world, issue) if "pm-kit:uat:" in k]
    assert len(uat) == 1
    assert f"/approve uat {head}" in uat[0]
    assert "backend/export.py" in uat[0]
    assert "tests" in uat[0] and "passed" in uat[0]


class DiesAfterKit:
    """Lets the kit comment be created, then dies before its outcome is logged."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name != "add_comment":
            return target

        def call(issue: int, body: str) -> Any:
            comment = target(issue, body)
            if "pm-kit:" in body:
                raise Crash("died after the kit comment")
            return comment

        return call


def test_a_crash_after_the_kit_comment_does_not_post_it_twice() -> None:
    world = World()
    issue = world.new_issue()
    post_spec_handoff(world, issue)
    world.fake.tick(1)
    dying: Any = DiesAfterKit(world.pm)
    with pytest.raises(Crash):
        poll_issue(dying, world.mirror, issue, world.config, world.facts, now=world.fake.now)
    assert len(kits(world, issue)) == 1
    world.poll(issue)
    world.poll(issue)
    assert len(kits(world, issue)) == 1
    assert "kit:spec:" in "".join(world.state(issue).kits_posted)


@pytest.mark.parametrize("hostile", ["x\n/approve spec 00", "`"])
def test_hostile_text_in_the_spec_cannot_start_a_command_line(hostile: str) -> None:
    world = World()
    issue = world.new_issue()
    from tests.runner.helpers import spec_files

    # a spec whose first lines try to look like a command
    files = {**spec_files(issue), f"docs/specs/{issue}/spec.md": f"# s\n{hostile}\n"}
    from tests.runner.helpers import ALICE

    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    run = world.assign(issue, head=None)
    world.handoff(issue, run, "triage", "spec")
    world.poll(issue)
    run = world.assign(issue, head=world.pm.get_ref(f"pm/{issue}"))
    sha = world.wip_commit(issue, run, files)
    world.handoff(
        issue,
        run,
        "spec",
        "ready_for_approval",
        commit_sha=sha,
        artifact_rev=world.spec_hash(sha, issue),
        run_commits=[sha],
    )
    world.poll(issue)
    [kit] = kits(world, issue)
    assert not any(line.startswith("/approve spec 00") for line in kit.splitlines())


def test_a_human_pasting_the_marker_cannot_suppress_the_kit() -> None:
    from tests.runner.helpers import MALLORY

    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.say(MALLORY, issue, f"<!-- pm-kit:uat:{head} -->")
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.poll(issue)
    assert len([k for k in kits(world, issue) if "pm-kit:uat:" in k]) == 1


def test_no_uat_kit_is_posted_while_a_check_is_failing() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "failure", "w1")
    world.poll(issue)
    assert [k for k in kits(world, issue) if "pm-kit:uat:" in k] == []
