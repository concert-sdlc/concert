"""`concert init`: put a project under Concert, as pull requests an admin reviews.

Creates the staging branch when missing, the Steering issue, and two PRs: one into staging with
`process.yaml` and both workflows, one into the default branch with the workflows (CI must exist
on both branches; the scheduled watchdog runs only from the default one). Everything is found
before it is created, so running it again after a partial failure finishes the job.
"""

from dataclasses import dataclass, replace

from concert.adopt.plan import CI_PATH, WATCHDOG_PATH, InitInputs, plan_init
from concert.github.api import GitHubApi

STEERING_TITLE = "Concert steering"
STAGING_BRANCH = "concert/init"
DEFAULT_BRANCH_BRANCH = "concert/init-workflows"
_AUTHOR = "concert-init"


@dataclass(frozen=True)
class InitResult:
    steering: int
    staging_pr: int
    default_pr: int


def run_init(github: GitHubApi, inputs: InitInputs, default_branch: str) -> InitResult:
    if github.get_ref(inputs.staging_branch) is None:
        github.create_branch(inputs.staging_branch, default_branch)
    steering = _steering_issue(github)
    files = plan_init(replace(inputs, steering_issue=steering))
    staging_pr = _pr(
        github, STAGING_BRANCH, inputs.staging_branch, files, "Adopt Concert: config and workflows"
    )
    workflows = {path: files[path] for path in (CI_PATH, WATCHDOG_PATH)}
    default_pr = _pr(
        github, DEFAULT_BRANCH_BRANCH, default_branch, workflows, "Adopt Concert: workflows"
    )
    return InitResult(steering, staging_pr, default_pr)


def _steering_issue(github: GitHubApi) -> int:
    for number in github.list_open_issues():
        if github.get_issue(number).title == STEERING_TITLE:
            return number
    body = (
        "Admins steer the project here with `/pm` commands "
        "(`/pm status`, `/pm pause`, `/pm resume`, `/pm reset #N <state>`). "
        "Concert answers in this thread."
    )
    return github.create_issue(STEERING_TITLE, body).number


def _pr(github: GitHubApi, branch: str, base: str, files: dict[str, str], title: str) -> int:
    existing = github.find_pr(branch)
    if existing is not None:
        return existing.number
    if github.get_ref(branch) is None:
        github.create_branch(branch, base)
        github.commit(branch, title, files, author=_AUTHOR, committer=_AUTHOR)
    pr = github.create_pr(branch, base, title)
    github.mark_ready(pr.number)
    return pr.number
