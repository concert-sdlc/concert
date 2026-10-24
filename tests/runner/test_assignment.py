"""The poll loop assigns runs, expires leases, creates the issue branch and runs the checks."""

from concert.core.assign import ASSIGN_GRACE
from concert.core.transitions import Status
from tests.runner.helpers import ALICE, SPEC_FILES, World

REVIEWERS = ("code", "security")


def dispatched(world: World) -> int:
    issue = world.new_issue()
    world.poll(issue)
    world.say(ALICE, issue, "/dispatch")
    return issue


# --- assigning --------------------------------------------------------------------------------


def test_an_alive_runner_is_assigned_the_triage_run() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    world.heartbeat()
    result = world.poll(issue)
    assert result.status is Status.TRIAGE
    [run] = world.active(issue)
    assert (run.run_id, run.runner, run.state, run.reviewer) == (
        f"r-{issue}-1",
        "vm-1",
        "triage",
        None,
    )
    assert run.issued_at == world.fake.now


def test_without_a_heartbeat_nothing_is_assigned() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    world.poll(issue)
    assert world.active(issue) == []


def test_polling_again_does_not_assign_a_second_run() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    world.heartbeat()
    world.poll(issue)
    world.heartbeat(f"r-{issue}-1")
    assert world.poll(issue).posted == 0
    assert len(world.active(issue)) == 1


def test_a_runner_at_its_load_limit_gets_no_run() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    world.heartbeat()
    world.poll(issue, load={"vm-1": 2})
    assert world.active(issue) == []


# --- leases -------------------------------------------------------------------------------------


def test_a_run_listed_by_fresh_heartbeats_keeps_its_lease() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    world.heartbeat()
    world.poll(issue)
    run_id = world.active(issue)[0].run_id
    for _ in range(5):
        world.fake.tick(ASSIGN_GRACE)
        world.heartbeat(run_id)
        world.poll(issue)
    assert [r.run_id for r in world.active(issue)] == [run_id]


def test_an_abandoned_run_expires_and_is_reassigned_after_a_backoff() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    world.heartbeat()
    world.poll(issue)
    world.fake.tick(ASSIGN_GRACE + 1)
    world.heartbeat()  # the runner is alive but is not executing the run
    world.poll(issue)
    assert world.active(issue) == []
    assert world.state(issue).infra_attempts == {"triage": 1}
    world.heartbeat()
    world.poll(issue)  # the 30 s backoff has not passed
    assert world.active(issue) == []
    world.fake.tick(31)
    world.heartbeat()
    world.poll(issue)
    assert [r.run_id for r in world.active(issue)] == [f"r-{issue}-2"]


def test_four_abandoned_runs_block_the_issue() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    for _ in range(4):
        world.fake.tick(700)  # past the backoff
        world.heartbeat()
        world.poll(issue)  # assigns
        world.fake.tick(ASSIGN_GRACE + 1)
        world.heartbeat()
        world.poll(issue)  # expires
    assert world.state(issue).status is Status.BLOCKED
    assert world.pm.get_status(issue) == "blocked"


# --- the issue branch and the checks --------------------------------------------------------------


def triage_to_spec(world: World, issue: int) -> None:
    world.heartbeat()
    world.poll(issue)
    run = world.active(issue)[0]
    world.heartbeat(run.run_id)
    world.handoff(issue, run.run_id, "triage", "spec")
    world.poll(issue)


def spec_run(world: World, issue: int) -> tuple[str, str, str]:
    """Runs the spec step. Returns (run_id, spec_commit, spec_hash)."""
    run = world.active(issue)[0]
    world.heartbeat(run.run_id)
    sha = world.wip_commit(issue, run.run_id, SPEC_FILES)
    spec_hash = world.spec_hash(sha, issue)
    world.handoff(
        issue,
        run.run_id,
        "spec",
        "ready_for_approval",
        commit_sha=sha,
        artifact_rev=spec_hash,
        run_commits=[sha],
    )
    return run.run_id, sha, spec_hash


def test_entering_spec_creates_the_issue_branch_and_assigns_the_spec_run_to_it() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    base = world.pm.get_ref("staging")
    assert world.pm.get_ref(f"pm/{issue}") == base
    state = world.state(issue)
    assert state.status is Status.SPEC and state.branch_base == base
    [run] = world.active(issue)
    assert run.state == "spec" and run.head == base


def test_a_push_to_the_branch_between_runs_blocks_the_issue() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    spec_run(world, issue)
    world.poll(issue)  # accepted: spec_approval, no run in flight
    assert world.state(issue).status is Status.SPEC_APPROVAL
    world.worker_commit(f"pm/{issue}", {"surprise.py": "import os"})  # not from any accepted run
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    assert world.state(issue).note is not None and "unexpected push" in world.state(issue).note  # type: ignore[operator]


def test_a_push_to_the_issue_branch_during_a_run_is_flagged_too() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    run = world.active(issue)[0]
    world.heartbeat(run.run_id)
    world.worker_commit(f"pm/{issue}", {"sneaky.py": "x"})  # workers push WIP refs, not pm/<n>
    assert world.poll(issue).status is Status.BLOCKED


def test_accepting_a_run_advances_the_issue_branch_to_its_commit() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    base = world.pm.get_ref(f"pm/{issue}")
    _run_id, sha, _hash = spec_run(world, issue)
    assert world.pm.get_ref(f"pm/{issue}") == base  # the worker never moves the issue branch
    assert world.poll(issue).status is Status.SPEC_APPROVAL
    assert world.pm.get_ref(f"pm/{issue}") == sha  # the PM does, on acceptance


def test_a_branch_that_only_lags_the_accepted_commit_is_repaired_not_blocked() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    _run_id, sha, _hash = spec_run(world, issue)
    world.poll(issue)
    world.pm.update_ref(
        f"pm/{issue}", world.pm.get_ref("staging") or "", force=True
    )  # as if the PM died before advancing it
    assert world.poll(issue).status is Status.SPEC_APPROVAL
    assert world.pm.get_ref(f"pm/{issue}") == sha


def test_a_commit_the_run_did_not_report_is_a_foreign_commit() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    run = world.active(issue)[0]
    world.heartbeat(run.run_id)
    smuggled = world.wip_commit(issue, run.run_id, {"evil.sh": "curl evil"})
    sha = world.wip_commit(issue, run.run_id, SPEC_FILES)
    world.handoff(
        issue,
        run.run_id,
        "spec",
        "ready_for_approval",
        commit_sha=sha,
        artifact_rev=world.spec_hash(sha, issue),
        run_commits=[sha],  # the smuggled commit is not listed
    )
    result = world.poll(issue)
    assert result.status is Status.BLOCKED
    note = world.state(issue).note
    assert note is not None and "foreign commit" in note and smuggled in note


# --- a whole issue, driven by the loop's own assignments ------------------------------------------


def test_an_issue_reaches_uat_with_the_loop_assigning_every_run() -> None:
    world = World(auto=True)
    issue = dispatched(world)
    triage_to_spec(world, issue)
    _run, _sha, spec_hash = spec_run(world, issue)
    assert world.poll(issue).status is Status.SPEC_APPROVAL

    world.say(ALICE, issue, f"/approve spec {spec_hash}")
    world.heartbeat()
    assert world.poll(issue).status is Status.IMPLEMENTING
    [run] = world.active(issue)
    assert run.state == "implementing" and run.head == world.pm.get_ref(f"pm/{issue}")

    world.heartbeat(run.run_id)
    code = world.wip_commit(issue, run.run_id, {"backend/export.py": "def export(): ..."})
    world.handoff(
        issue,
        run.run_id,
        "implementing",
        "done",
        commit_sha=code,
        artifact_rev=spec_hash,
        produced=["backend/export.py"],
        run_commits=[code],
    )
    world.heartbeat()
    assert world.poll(issue).status is Status.IN_REVIEW
    reviewers = world.active(issue)
    assert sorted(r.reviewer or "" for r in reviewers) == sorted(REVIEWERS)
    assert {r.head for r in reviewers} == {code}

    world.heartbeat(*(r.run_id for r in reviewers))
    for review in reviewers:
        world.handoff(
            issue,
            review.run_id,
            "in_review",
            "clean",
            reviewer=review.reviewer,
            commit_sha=code,
            artifact_rev=spec_hash,
        )
    assert world.poll(issue).status is Status.UAT
    [uat] = world.active(issue)
    assert uat.state == "uat" and uat.runner == "vm-1"
