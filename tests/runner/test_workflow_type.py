"""The workflow issue type: an agent's request to change CI workflows goes to an admin as a diff."""

from dataclasses import replace

from concert.core.commands import Role
from concert.core.transitions import IssueType, Status
from concert.core.workflow import diff_hash, problem_with, render_diff
from concert.runner.worker import AgentResult
from tests.runner.helpers import ALICE, World, spec_files
from tests.runner.test_worker import Scripted, worker

WORKFLOW = ".github/workflows/concert-ci.yml"
CONTENT = "name: concert-ci\non: pull_request\njobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n"


def with_request(
    files: dict[str, str] | None = None, role: Role = Role.ADMIN
) -> tuple[World, int, int]:
    """A spec run proposed workflow files; the PM accepted it and filed the workflow issue."""
    world = World()
    world.config = replace(world.config, roles={ALICE.user_id: role})
    world.fake.seed_branch("staging", {"README": "x", WORKFLOW: "name: concert-ci\non: push\n"})
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    world.handoff(issue, world.assign(issue, head=None), "triage", "spec")
    world.poll(issue)
    world.assign(issue, head=world.pm.get_ref(f"pm/{issue}"))
    changes = {**spec_files(issue), **({WORKFLOW: CONTENT} if files is None else files)}
    agent = Scripted({"spec": AgentResult(exit="ready_for_approval", changes=changes)})
    worker(world, agent).run_once(issue, now=world.fake.now)
    world.poll(issue)
    others = [n for n in world.pm.list_open_issues() if n != issue]
    return world, issue, (others[0] if others else 0)


def test_a_workflow_request_becomes_a_workflow_issue_at_the_human_gate() -> None:
    world, issue, wf = with_request()
    assert wf != 0
    state = world.state(wf)
    assert state.issue_type is IssueType.WORKFLOW and state.status is Status.HUMAN_UAT
    assert state.workflow_parent == issue and state.workflow_files == {WORKFLOW: CONTENT}
    assert state.workflow_diff_hash == diff_hash({WORKFLOW: CONTENT})
    assert (
        world.state(issue).workflow_issues == (wf,) and world.state(issue).workflow_requests == ()
    )
    assert world.state(issue).status is Status.SPEC_APPROVAL  # the parent carries on
    notes = [c.body for c in world.pm.list_comments(issue) if "Filed #" in c.body]
    assert len(notes) == 1 and f"#{wf}" in notes[0]


def test_the_kit_shows_the_diff_against_staging_and_the_one_command() -> None:
    world, _issue, wf = with_request()
    world.poll(wf)
    [kit] = [
        c.body for c in world.pm.list_comments(wf) if "Workflow change for your approval" in c.body
    ]
    assert f"/approve workflow {diff_hash({WORKFLOW: CONTENT})}" in kit
    assert "-on: push" in kit and "+on: pull_request" in kit  # against what staging has now
    assert kit.rstrip().endswith("-->")
    world.poll(wf)
    assert len([c for c in world.pm.list_comments(wf) if "approval: #" in c.body]) <= 1


def test_nothing_is_pushed_before_an_admin_approves_its_hash() -> None:
    world, _issue, wf = with_request()
    world.poll(wf)
    assert world.pm.get_ref(f"pm/{wf}-workflow") is None and world.state(wf).pr_number is None
    world.say(ALICE, wf, "/approve workflow deadbeef")
    result = world.poll(wf)
    assert result.status is Status.HUMAN_UAT and result.rejected_commands


def test_an_approver_who_is_not_an_admin_cannot_approve_it() -> None:
    world, _issue, wf = with_request(role=Role.APPROVER)
    world.poll(wf)
    world.say(ALICE, wf, f"/approve workflow {world.state(wf).workflow_diff_hash}")
    result = world.poll(wf)
    assert result.status is Status.HUMAN_UAT and "admin" in result.rejected_commands[0]


def test_approval_pushes_the_exact_files_opens_the_pr_and_an_admin_merge_ends_the_issue() -> None:
    world, _issue, wf = with_request()
    world.poll(wf)
    world.say(ALICE, wf, f"/approve workflow {world.state(wf).workflow_diff_hash}")
    result = world.poll(wf)
    state = world.state(wf)
    assert result.status is Status.INTEGRATING and state.active_runs == ()  # no agent runs
    branch = f"pm/{wf}-workflow"
    assert world.pm.read_tree(branch)[WORKFLOW] == CONTENT
    pr = world.fake.prs[state.pr_number or 0].pr
    assert (pr.head, pr.base, pr.draft, pr.state) == (branch, "staging", False, "open")
    assert not result.ready_for_integration
    world.poll(wf)
    assert world.state(wf).status is Status.INTEGRATING  # the PM never merges it
    world.fake.client(ALICE).merge_pr(pr.number, world.pm.get_ref(branch) or "")
    assert world.poll(wf).status is Status.ON_STAGING
    assert world.pm.get_status(wf) == "on_staging"


def test_a_request_that_is_not_a_workflow_file_files_nothing() -> None:
    world, issue, wf = with_request({".github/workflows/notes.md": "# not yaml"})
    assert wf == 0
    state = world.state(issue)
    assert state.workflow_issues == () and state.workflow_requests == ()  # settled, nothing filed


def test_a_crash_after_creating_the_issue_does_not_file_a_second_one() -> None:
    world = World()
    world.config = replace(world.config, roles={ALICE.user_id: Role.ADMIN})
    world.fake.seed_branch("staging", {"README": "x"})
    issue = world.new_issue()
    world.say(ALICE, issue, "/dispatch")
    world.poll(issue)
    world.handoff(issue, world.assign(issue, head=None), "triage", "spec")
    world.poll(issue)
    run = world.assign(issue, head=world.pm.get_ref(f"pm/{issue}"))
    orphan = world.fake.client(world.pm_identity).create_issue(
        f"Workflow change from #{issue} ({run})", ""
    )
    agent = Scripted(
        {
            "spec": AgentResult(
                exit="ready_for_approval", changes={**spec_files(issue), WORKFLOW: CONTENT}
            )
        }
    )
    worker(world, agent).run_once(issue, now=world.fake.now)
    world.poll(issue)
    assert world.state(issue).workflow_issues == (orphan.number,)
    assert world.state(orphan.number).issue_type is IssueType.WORKFLOW
    assert len(world.pm.list_open_issues()) == 2


def test_a_workflow_issue_is_not_a_delivery_in_the_figures() -> None:
    from concert.core.metrics import compute, observe
    from tests.core.test_metrics import NOW, Log

    log = Log("workflow").landed(0, 3600)
    assert compute([observe(1, log.timed, "on_staging")], NOW).weeks[0].landed == 0


def test_the_request_rules_and_the_hash() -> None:
    assert problem_with({WORKFLOW: CONTENT}) is None
    assert "not a workflow file" in (problem_with({"src/a.py": "x"}) or "")
    assert "not a workflow file" in (problem_with({".github/workflows/a.md": "x"}) or "")
    assert "empty" in (problem_with({WORKFLOW: "  "}) or "")
    assert "at most" in (problem_with({f".github/workflows/{i}.yml": "x" for i in range(9)}) or "")
    assert diff_hash({"a": "1", "b": "2"}) == diff_hash({"b": "2", "a": "1"})
    assert diff_hash({"a": "1"}) != diff_hash({"a": "2"})
    assert render_diff("x.yml", None, "a\n").startswith("--- /dev/null")
