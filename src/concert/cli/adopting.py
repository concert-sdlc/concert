"""The REST side of `concert adopt`: read the repository, resolve people and Apps to ids, then
hand the facts to `concert.adopt.adopt`, which holds the decisions."""

import argparse
import subprocess
from collections.abc import Mapping
from typing import Any, TextIO, cast

from concert.adopt.adopt import AdoptStop, RepoFacts, adopt
from concert.adopt.plan import Human, InitInputs, RunnerSpec
from concert.github.api import NotFound
from concert.github.http import GitHubHttp, Transport
from concert.github.rest import RestGitHub

WAITING = 3  # exit status: prepared, waiting for an admin's /approve adopt


def run_adopt(
    args: argparse.Namespace, env: Mapping[str, str], out: TextIO, transport: Transport
) -> int:
    owner, _, repo = str(args.repo).partition("/")
    if not owner or not repo or "/" in repo:
        raise AdoptStop("--repo must look like owner/name")
    ci = _checks(args.ci)
    token = env.get("GITHUB_TOKEN") or _gh_token()
    if not token:
        raise AdoptStop("no token: set GITHUB_TOKEN or sign in with `gh auth login`")
    http = GitHubHttp(transport, token=lambda: token)
    github = RestGitHub(http, owner, repo)
    facts = _facts(http, github, owner, repo)
    admins = [_human(http, login) for login in args.admin]
    approvers = [_human(http, login) for login in args.approver] or admins
    pm_login, pm_id = _bot(http, args.pm_app)
    worker_login, worker_id = _bot(http, args.worker_app)
    inputs = InitInputs(
        owner=owner,
        repo=repo,
        staging_branch=args.staging,
        admins=admins,
        approvers=approvers,
        contributors=[],
        pm_login=pm_login,
        pm_bot_id=pm_id,
        runners=[RunnerSpec(args.runner, worker_login, worker_id, ("docker", "claude-code"), 1)],
        ci_local=ci,
        ci_setup=None,
        steering_issue=0,
        protection=args.protection,
    )

    def say(line: str) -> None:
        print(f"concert: {line}", file=out)

    outcome = adopt(github, facts, github.read_tree(facts.default_branch), inputs, owner, repo, say)
    if outcome.stage == "waiting_for_approval":
        say(f"waiting: an admin must reply on #{outcome.steering} with")
        say(f"  /approve adopt {outcome.report.digest}")
        say("then run this command again")
        return WAITING
    assert outcome.init is not None
    say(
        f"opened PR #{outcome.init.staging_pr} (into {args.staging}) and "
        f"#{outcome.init.default_pr} (into {facts.default_branch}); an admin reviews and merges both"
    )
    say(
        "next: install the Apps on the repository, create the Projects v2 board, and protect "
        "the branches (then set protection: enforce in process.yaml)"
    )
    return 0


def _checks(items: list[str]) -> dict[str, str]:
    checks: dict[str, str] = {}
    for item in items:
        name, found, command = item.partition("=")
        if not found or not name.strip() or not command.strip():
            raise AdoptStop(f"--ci must look like NAME=COMMAND, got {item!r}")
        checks[name.strip()] = command.strip()
    if not checks:
        raise AdoptStop("name at least one check, for example --ci tests='make test'")
    return checks


def _gh_token() -> str:
    try:
        done = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _facts(http: GitHubHttp, github: RestGitHub, owner: str, repo: str) -> RepoFacts:
    info = cast("dict[str, Any]", http.json("GET", f"/repos/{owner}/{repo}"))
    default = str(info["default_branch"])
    try:
        workflows = http.json("GET", f"/repos/{owner}/{repo}/contents/.github/workflows")
        names = tuple(str(f["name"]) for f in cast("list[dict[str, Any]]", workflows))
    except NotFound:
        names = ()
    branches = http.paged(f"/repos/{owner}/{repo}/branches", {"per_page": 100})
    labels = http.paged(f"/repos/{owner}/{repo}/labels", {"per_page": 100})
    pulls = http.paged(f"/repos/{owner}/{repo}/pulls", {"state": "open", "per_page": 100})
    return RepoFacts(
        owner_type=str(cast("dict[str, Any]", info["owner"])["type"]),
        caller_is_admin=bool(cast("dict[str, Any]", info.get("permissions") or {}).get("admin")),
        default_branch=default,
        private=bool(info["private"]),
        open_prs=len(pulls),
        open_issues=len(github.list_open_issues()),
        other_branches=sum(1 for b in branches if b["name"] != default),
        labels=tuple(str(label["name"]) for label in labels),
        workflows=names,
    )


def _human(http: GitHubHttp, login: str) -> Human:
    try:
        user = cast("dict[str, Any]", http.json("GET", f"/users/{login}"))
    except NotFound:
        raise AdoptStop(f"no GitHub user {login!r}") from None
    return Human(str(user["login"]), int(user["id"]))


def _bot(http: GitHubHttp, slug: str) -> tuple[str, int]:
    login = f"{slug}[bot]"
    try:
        user = cast("dict[str, Any]", http.json("GET", f"/users/{slug}%5Bbot%5D"))
    except NotFound:
        raise AdoptStop(f"no GitHub App {slug!r} (looked up {login})") from None
    return login, int(user["id"])
