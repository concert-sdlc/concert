"""`concert init`: steering issue, staging branch and the two review PRs."""

from concert.adopt.init import run_init
from concert.adopt.plan import Human, InitInputs, RunnerSpec
from concert.github.api import Identity
from tests.fakes.github import FakeClient, FakeGitHub

ADMIN = Identity(user_id=583231, login="alice", is_bot=False)


def inputs(steering: int = 0) -> InitInputs:
    return InitInputs(
        owner="acme",
        repo="app",
        staging_branch="staging",
        admins=(Human("alice", 583231),),
        approvers=(Human("alice", 583231),),
        contributors=(),
        pm_login="concert-pm[bot]",
        pm_bot_id=346900000,
        runners=(RunnerSpec("vm-1", "concert-vm1[bot]", 346900002, ("docker",), 1),),
        ci_local={"tests": "make test"},
        ci_setup=None,
        steering_issue=steering,
    )


def repo() -> tuple[FakeGitHub, FakeClient]:
    fake = FakeGitHub()
    fake.seed_branch("main", {"README.md": "# app\n"})
    return fake, fake.client(ADMIN)


def test_init_creates_staging_the_steering_issue_and_two_prs() -> None:
    fake, gh = repo()
    result = run_init(gh, inputs(), default_branch="main")
    assert gh.get_ref("staging") == gh.get_ref("main")
    steering = gh.get_issue(result.steering)
    assert "steering" in steering.title
    to_staging = fake.prs[result.staging_pr].pr
    to_main = fake.prs[result.default_pr].pr
    assert (to_staging.base, to_main.base) == ("staging", "main")
    assert to_staging.draft is False and to_main.draft is False


def test_the_staging_pr_carries_the_config_and_both_workflows_and_the_default_pr_the_workflows() -> (
    None
):
    fake, gh = repo()
    result = run_init(gh, inputs(), default_branch="main")
    staging_tree = gh.read_tree(fake.prs[result.staging_pr].pr.head)
    main_tree = gh.read_tree(fake.prs[result.default_pr].pr.head)
    assert {
        "process.yaml",
        ".github/workflows/concert-ci.yml",
        ".github/workflows/concert-watchdog.yml",
    } <= set(staging_tree)
    assert ".github/workflows/concert-ci.yml" in main_tree and "process.yaml" not in main_tree
    assert (
        staging_tree[".github/workflows/concert-ci.yml"]
        == main_tree[".github/workflows/concert-ci.yml"]
    )
    assert f"steering_issue: {result.steering}" in staging_tree["process.yaml"]


def test_running_init_again_changes_nothing() -> None:
    fake, gh = repo()
    first = run_init(gh, inputs(), default_branch="main")
    prs_before, issues_before = len(fake.prs), len(fake.issues)
    second = run_init(gh, inputs(), default_branch="main")
    assert (second.staging_pr, second.default_pr, second.steering) == (
        first.staging_pr,
        first.default_pr,
        first.steering,
    )
    assert len(fake.prs) == prs_before and len(fake.issues) == issues_before


def test_an_existing_staging_branch_is_kept() -> None:
    _fake, gh = repo()
    existing = gh.create_branch("staging", "main")
    gh.commit("staging", "work", {"x": "1"}, author="a", committer="a")
    run_init(gh, inputs(), default_branch="main")
    assert gh.get_ref("staging") != existing  # not reset
    assert "x" in gh.read_tree("staging")
