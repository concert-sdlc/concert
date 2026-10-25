"""concert adopt: stops before changing anything when it must, then waits for an admin."""

from dataclasses import replace

import pytest

from concert.adopt.adopt import (
    AdoptStop,
    RepoFacts,
    adopt,
    assess,
    check_prerequisites,
)
from tests.adopt.test_init import ADMIN, inputs, repo
from tests.runner.helpers import MALLORY

FACTS = RepoFacts(
    owner_type="Organization",
    caller_is_admin=True,
    default_branch="main",
    private=True,
    open_prs=2,
    open_issues=7,
    other_branches=1,
    labels=("bug", "feature"),
    workflows=("ci.yml",),
)
TREE = {
    "README.md": "# app",
    "app/main.py": "x",
    "AGENTS.md": "rules",
    "docs/decisions/0001.md": "d",
}


def test_a_personal_repository_is_refused_before_anything_changes() -> None:
    fake, gh = repo()
    with pytest.raises(AdoptStop, match="personal account"):
        adopt(
            gh,
            replace(FACTS, owner_type="User"),
            TREE,
            inputs(),
            "me",
            "app",
        )
    assert gh.get_ref("staging") is None and fake.issues == {}


def test_a_caller_who_is_not_an_admin_is_refused() -> None:
    with pytest.raises(AdoptStop, match="not an admin"):
        check_prerequisites(replace(FACTS, caller_is_admin=False), "acme", "app")


def test_the_report_describes_the_repository_and_is_stable() -> None:
    first, second = (
        assess(FACTS, TREE, {"tests": "make test"}),
        assess(FACTS, TREE, {"tests": "make test"}),
    )
    assert first == second
    assert "stack: python" in first.text and "AGENTS.md" in first.text
    assert "open pull requests: 2; open issues: 7" in first.text
    assert "applies no rulesets" in first.text
    changed = assess(replace(FACTS, open_issues=8), TREE, {"tests": "make test"})
    assert changed.digest != first.digest


def test_the_first_run_prepares_the_branches_and_asks_for_approval() -> None:
    fake, gh = repo()
    outcome = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    assert outcome.stage == "waiting_for_approval" and outcome.init is None
    assert gh.get_ref("staging") == gh.get_ref("main")
    assert gh.get_ref("concert-events") and gh.get_ref("concert-evidence")
    comments = gh.list_comments(outcome.steering)
    assert len(comments) == 1 and f"/approve adopt {outcome.report.digest}" in comments[0].body
    assert fake.prs == {}  # no pull request before the approval


def test_running_again_does_not_repeat_the_report() -> None:
    _fake, gh = repo()
    first = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    second = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    assert second.stage == "waiting_for_approval" and second.steering == first.steering
    assert len(gh.list_comments(first.steering)) == 1


def test_an_admins_approval_of_this_report_opens_the_adoption_prs() -> None:
    fake, gh = repo()
    waiting = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    fake.client(ADMIN).add_comment(waiting.steering, f"/approve adopt {waiting.report.digest}")
    opened = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    assert opened.stage == "opened" and opened.init is not None
    assert fake.prs[opened.init.staging_pr].pr.base == "staging"
    assert fake.prs[opened.init.default_pr].pr.base == "main"


def test_approvals_of_another_report_or_by_a_stranger_do_not_count() -> None:
    fake, gh = repo()
    waiting = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    fake.client(ADMIN).add_comment(waiting.steering, "/approve adopt 1234567")
    fake.client(MALLORY).add_comment(waiting.steering, f"/approve adopt {waiting.report.digest}")
    assert adopt(gh, FACTS, TREE, inputs(), "acme", "app").stage == "waiting_for_approval"
    assert fake.prs == {}


def test_a_changed_repository_needs_a_fresh_approval() -> None:
    fake, gh = repo()
    waiting = adopt(gh, FACTS, TREE, inputs(), "acme", "app")
    fake.client(ADMIN).add_comment(waiting.steering, f"/approve adopt {waiting.report.digest}")
    newer = replace(FACTS, open_issues=9)
    again = adopt(gh, newer, TREE, inputs(), "acme", "app")
    assert again.stage == "waiting_for_approval" and again.report.digest != waiting.report.digest
    assert len(gh.list_comments(waiting.steering)) == 3  # report, approval, the new report
