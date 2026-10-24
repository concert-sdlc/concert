"""CI evidence and the move to integrating, through the poll loop (no agent)."""

from dataclasses import replace

from concert.core.commands import Role
from concert.core.evidence import GatePolicy
from concert.core.transitions import IssueType, Status
from tests.runner.helpers import ALICE, World
from tests.runner.test_poll import REVIEWERS, to_implementing

POLICY = GatePolicy(
    policy_rev="p1",
    dod_items=frozenset({"tests"}),
    allowed_workflows=frozenset({"w1"}),
    approvers=frozenset({ALICE.user_id}),
    admins=frozenset(),
    security_sensitive=False,
    protected_changed=False,
    issue_type=IssueType.CODE,
)


def to_human_uat(world: World, issue: int) -> str:
    """Implementation -> reviews -> automated UAT. Returns the head the issue sits at."""
    _sha, spec_hash = to_implementing(world, issue)
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
    for reviewer in REVIEWERS:
        run = world.assign(issue, head=head, reviewer=reviewer)
        world.handoff(
            issue,
            run,
            "in_review",
            "clean",
            reviewer=reviewer,
            commit_sha=head,
            artifact_rev=spec_hash,
        )
    assert world.poll(issue).status is Status.UAT
    run = world.assign(issue, head=head)
    world.handoff(issue, run, "uat", "passed", commit_sha=head, artifact_rev=spec_hash)
    assert world.poll(issue).status is Status.HUMAN_UAT
    return head


def policy_world() -> World:
    world = World()
    world.config = replace(world.config, gate_policy=POLICY)
    return world


def test_a_finished_check_on_the_head_is_recorded_as_evidence() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.poll(issue)
    ci = [e for e in world.state(issue).evidence if e.kind == "ci"]
    assert [(e.item, e.head_sha, e.passed, e.workflow_hash, e.policy_rev) for e in ci] == [
        ("tests", head, True, "w1", "p1")
    ]


def test_polling_again_does_not_record_the_same_result_twice() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.poll(issue)
    assert world.poll(issue).posted == 0


def test_without_a_gate_policy_check_runs_are_not_read() -> None:
    world = World()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.poll(issue)
    assert [e for e in world.state(issue).evidence if e.kind == "ci"] == []


def test_the_issue_moves_to_integrating_when_the_whole_gate_holds() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    assert world.poll(issue).status is Status.HUMAN_UAT  # the human has not approved yet
    world.say(ALICE, issue, f"/approve uat {head}")
    result = world.poll(issue)
    assert result.status is Status.INTEGRATING
    assert world.pm.get_status(issue) == "integrating"


def test_a_failing_check_keeps_the_issue_in_human_uat() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "failure", "w1")
    world.say(ALICE, issue, f"/approve uat {head}")
    assert world.poll(issue).status is Status.HUMAN_UAT


def test_a_disallowed_workflow_does_not_satisfy_the_gate() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "tampered")
    world.say(ALICE, issue, f"/approve uat {head}")
    assert world.poll(issue).status is Status.HUMAN_UAT


def test_an_issue_not_at_the_head_of_the_queue_waits() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.say(ALICE, issue, f"/approve uat {head}")
    waiting = world.poll(issue, at_queue_head=False)
    assert waiting.status is Status.HUMAN_UAT
    assert world.poll(issue).status is Status.INTEGRATING


def test_ci_that_finishes_in_the_same_poll_as_the_approval_moves_the_issue_on() -> None:
    world = policy_world()
    issue = world.new_issue()
    head = to_human_uat(world, issue)
    world.say(ALICE, issue, f"/approve uat {head}")
    world.fake.add_check_run(head, "tests", "success", "w1")
    assert world.poll(issue).status is Status.INTEGRATING


def revert_world() -> tuple[World, int, str]:
    world = policy_world()
    world.config = replace(
        world.config,
        gate_policy=replace(POLICY, admins=frozenset({ALICE.user_id})),
        roles={ALICE.user_id: Role.ADMIN},
    )
    issue = world.new_issue()
    world.poll(issue)
    head = "feedface" * 5
    world.pm_post(issue, "created", {"issue_type": "revert"})
    world.pm_post(
        issue,
        "handoff_accepted",
        {"run_id": "r-1", "state": "implementing", "commit_sha": head, "artifact_rev": None},
    )
    world.pm_post(
        issue,
        "transition",
        {"from": "implementing", "to": "human_uat", "trigger": "ci_green"},
    )
    assert world.state(issue).status is Status.HUMAN_UAT
    return world, issue, head


def test_a_revert_integrates_on_green_ci_and_an_admin_approval_without_reviews() -> None:
    world, issue, head = revert_world()
    world.fake.add_check_run(head, "tests", "success", "w1")
    world.say(ALICE, issue, f"/approve revert {head}")
    assert world.poll(issue).status is Status.INTEGRATING


def test_a_revert_does_not_integrate_without_the_admin_approval() -> None:
    world, issue, head = revert_world()
    world.fake.add_check_run(head, "tests", "success", "w1")
    assert world.poll(issue).status is Status.HUMAN_UAT
