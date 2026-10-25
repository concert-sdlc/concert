"""The PM rebases a branch onto a moved staging: before the human UAT kit (bounded, uncharged) and
when the issue reaches the head of the queue. A conflict goes back to implementing."""

from dataclasses import replace

from concert.core.decide import plan_refresh
from concert.core.derive import IssueState
from concert.core.effects import plan_effect
from concert.core.transitions import Status
from tests.runner.helpers import ALICE
from tests.runner.test_journey import Project


def to_uat(project: Project) -> None:
    project.step()
    project.human("/dispatch")
    project.run_until(Status.SPEC_APPROVAL)
    project.human(f"/approve spec {project.world.state(project.issue).spec_rev}")
    project.run_until(Status.UAT)


def staging_moves(project: Project, files: dict[str, str | None]) -> str:
    return project.world.fake.client(ALICE).commit(
        "staging", "someone else merged", files, author="bob", committer="bob"
    )


def approve(project: Project) -> None:
    world, issue = project.world, project.issue
    head = world.state(issue).head_sha or ""
    world.fake.add_check_run(head, "tests", "success", "w1")
    project.step()  # records the CI result and posts the kit
    project.human(f"/approve uat {head}")


# --- before the human kit ----------------------------------------------------------------------


def test_a_moved_staging_is_absorbed_before_the_kit_and_costs_no_retry() -> None:
    project = Project()
    world, issue = project.world, project.issue
    to_uat(project)
    before = world.state(issue).head_sha
    moved = staging_moves(project, {"docs/other.md": "other work"})
    project.run_until(Status.HUMAN_UAT)
    state = world.state(issue)
    assert state.refreshes == 1 and state.head_sha != before
    assert world.pm.commit_parents(state.head_sha or "") == (moved,)  # on top of the new staging
    tree = world.pm.read_tree(state.head_sha or "")
    assert (
        tree["docs/other.md"] == "other work" and tree["backend/export.py"] == "def export(): ..."
    )
    assert world.pm.get_ref(f"pm/{issue}") == state.head_sha
    assert state.counters == IssueState().counters  # staging churn is never charged
    approve(project)
    project.run_until(Status.ON_STAGING)
    final = world.pm.read_tree(world.pm.get_ref("staging") or "")
    assert final["docs/other.md"] == "other work" and "backend/export.py" in final


def test_the_old_ci_and_reviews_do_not_count_for_the_rebased_head() -> None:
    project = Project()
    world, issue = project.world, project.issue
    to_uat(project)
    old = world.state(issue).head_sha
    staging_moves(project, {"docs/other.md": "x"})
    project.run_until(Status.HUMAN_UAT)
    new = world.state(issue).head_sha
    assert new != old
    assert all(e.head_sha != new for e in world.state(issue).evidence if e.kind == "approval")


def test_with_no_refresh_allowed_the_kit_is_posted_for_the_old_head() -> None:
    project = Project()
    project.world.config = replace(project.world.config, pregate_refresh=0)
    world, issue = project.world, project.issue
    to_uat(project)
    old = world.state(issue).head_sha
    staging_moves(project, {"docs/other.md": "x"})
    project.run_until(Status.HUMAN_UAT)
    assert world.state(issue).head_sha == old and world.state(issue).refreshes == 0


def test_a_conflicting_refresh_returns_to_implementing_uncharged_then_asks_the_admins() -> None:
    project = Project()
    world, issue = project.world, project.issue
    to_uat(project)
    staging_moves(project, {"backend/export.py": "def export(): return 'theirs'"})
    project.run_until(Status.IMPLEMENTING)
    state = world.state(issue)
    assert state.note and "backend/export.py" in state.note
    assert state.counters == IssueState().counters
    assert world.pm.get_ref(f"pm/{issue}") == state.head_sha  # the branch was left untouched
    project.run_until(Status.NEEDS_INPUT)  # the same conflict again: a person decides
    again = world.state(issue)
    assert again.refresh_conflicts == 2 and "admin" in (again.note or "")


# --- at the head of the queue ---------------------------------------------------------------------


def test_staging_moving_while_queued_is_rebased_and_needs_a_new_approval() -> None:
    project = Project()
    world, issue = project.world, project.issue
    to_uat(project)
    project.run_until(Status.HUMAN_UAT)
    old = world.state(issue).head_sha or ""
    world.fake.add_check_run(old, "tests", "success", "w1")
    project.step()  # the kit for the old head
    staging_moves(project, {"docs/other.md": "late"})
    project.human(f"/approve uat {old}")
    for _ in range(10):  # approved: integrating notices staging moved and rebases
        project.step()
        if world.state(issue).head_sha != old:
            break
    project.run_until(Status.HUMAN_UAT)  # review and UAT ran again on the new head
    state = world.state(issue)
    assert state.head_sha != old and state.refreshes == 0  # a queue rebase is not a pre-gate one
    assert world.pm.read_tree(state.head_sha or "")["docs/other.md"] == "late"
    approve(project)
    project.run_until(Status.ON_STAGING)


# --- the pure parts ---------------------------------------------------------------------------------


def test_a_clean_rebase_leads_back_to_review_and_a_conflict_to_implementing() -> None:
    clean = IssueState(status=Status.UAT, refresh_result="clean")
    [move] = plan_refresh(clean)
    assert move.data["to"] == "in_review" and move.data["trigger"] == "refreshed"
    conflict = replace(clean, refresh_result="conflict", refresh_note="a.py", refresh_conflicts=1)
    assert plan_refresh(conflict)[-1].data["to"] == "implementing"
    twice = replace(conflict, refresh_conflicts=2)
    assert plan_refresh(twice)[-1].data["to"] == "needs_input"
    assert plan_refresh(IssueState(status=Status.UAT)) == []
    assert plan_refresh(replace(clean, status=Status.HUMAN_UAT)) == []


def test_no_rebase_is_planned_without_a_moved_staging_or_past_the_limit() -> None:
    state = IssueState(
        status=Status.UAT,
        settled_runs=frozenset({"r1"}),
        head_sha="h1",
        branch_base="b1",
        branch="pm/3",
        refreshes=1,
        pr_number=5,
        pr_ready=True,
    )
    effect = plan_effect(state, 3, "staging", staging_moved=True, refresh_limit=2)
    assert effect is not None and effect.action == "rebase" and effect.data["head"] == "h1"
    assert plan_effect(state, 3, "staging", staging_moved=False, refresh_limit=2) is None
    assert plan_effect(state, 3, "staging", staging_moved=True, refresh_limit=1) is None
