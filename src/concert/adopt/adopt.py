"""`concert adopt`: put an existing repository under Concert, with an admin's say-so in between.

The command is safe to run again at any point; each run does what the state allows:

1. check what must hold (an organization-owned repo, a caller who is a repo admin) and stop
   before changing anything when it does not;
2. create the service branches (`staging`, `concert-events`, `concert-evidence`) and the
   Steering issue, then post a read-only assessment of the repository there, with its hash;
3. wait for `/approve adopt <hash>` from one of the named admins;
4. only then open the adoption pull requests (`process.yaml` and the generated workflows).

Rulesets are not applied here: the report lists what to protect, and `process.yaml` records
`protection: detect` unless the admin states the rulesets are in place.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from concert.adopt.init import STEERING_TITLE, InitResult, _steering_issue, run_init  # pyright: ignore[reportPrivateUsage]
from concert.adopt.plan import Human, InitInputs
from concert.core.commands import IssueContext, Role, Valid, validate_command
from concert.core.kits import has_marker
from concert.core.lessons import stack_tags
from concert.core.transitions import IssueType, Status
from concert.github.api import GitHubApi
from concert.github.branches import ensure_orphan_branch
from concert.github.comments import extract_commands

EVIDENCE_BRANCH = "concert-evidence"
EVENTS_BRANCH = "concert-events"
_AUTHOR = "concert-adopt"


class AdoptStop(Exception):
    """Adoption cannot go on; the message says why and what to do."""


@dataclass(frozen=True)
class RepoFacts:
    """What the REST API says about the repository, fetched by the command line."""

    owner_type: str  # "Organization" or "User"
    caller_is_admin: bool
    default_branch: str
    private: bool
    open_prs: int
    open_issues: int
    other_branches: int
    labels: tuple[str, ...]
    workflows: tuple[str, ...]  # existing workflow file names


@dataclass(frozen=True)
class Report:
    text: str
    digest: str  # what `/approve adopt` names


@dataclass(frozen=True)
class Outcome:
    stage: str  # "waiting_for_approval" or "opened"
    steering: int
    report: Report
    init: InitResult | None = None


def check_prerequisites(facts: RepoFacts, owner: str, repo: str) -> None:
    if facts.owner_type != "Organization":
        raise AdoptStop(
            f"{owner}/{repo} belongs to a personal account. GitHub Apps cannot write the Status "
            "field of a user-owned project board, so Milestone 1 adopts organization repositories "
            "only. Transfer the repository to an organization (a free one works) and run this "
            "again. Nothing was changed."
        )
    if not facts.caller_is_admin:
        raise AdoptStop(
            f"your token is not an admin of {owner}/{repo}. Adopt needs admin rights to create "
            "branches and open the adoption pull requests. Nothing was changed."
        )


def assess(facts: RepoFacts, tree: Mapping[str, str], ci_local: Mapping[str, str]) -> Report:
    """A read-only description of the repository. The same facts give the same hash, so a repeated
    run does not ask for a second approval; a repository that changed gets a new report."""
    paths = sorted(tree)
    tags = sorted(stack_tags(paths))
    docs = [p for p in ("AGENTS.md", "docs/architecture.md", "docs/decisions") if _has(paths, p)]
    lines = [
        "## Adoption report",
        "",
        f"- default branch: `{facts.default_branch}`; {'private' if facts.private else 'public'}",
        f"- stack: {', '.join(tags) or 'not recognised'}",
        f"- existing workflows: {', '.join(sorted(facts.workflows)) or 'none'}",
        f"- Concert will check: {', '.join(f'{k} (`{v}`)' for k, v in sorted(ci_local.items()))}",
        f"- docs found: {', '.join(docs) or 'none (adopt will not invent them)'}",
        (
            f"- open pull requests: {facts.open_prs}; open issues: {facts.open_issues}; "
            f"other branches: {facts.other_branches}"
        ),
        f"- labels: {', '.join(sorted(facts.labels)) or 'none'}",
        "",
        "### What adopting does",
        (
            "- opens a pull request into `staging` with `process.yaml` and the generated "
            "workflows, and one into the default branch with the workflows (the watchdog runs "
            "only from there);"
        ),
        (
            "- never closes or rewrites an existing issue or pull request: open issues simply "
            "enter the inbox;"
        ),
        (
            "- applies no rulesets. Protect `staging`, the default branch and tags so that only "
            "the Concert apps and admins can merge, and set `protection: enforce` in "
            "`process.yaml`; until then it stays `detect`."
        ),
    ]
    text = "\n".join(lines)
    return Report(text, hashlib.sha256(text.encode()).hexdigest())


def _has(paths: Sequence[str], wanted: str) -> bool:
    return any(p == wanted or p.startswith(wanted + "/") for p in paths)


def _report_marker(digest: str) -> str:
    return f"<!-- pm-adopt:{digest} -->"


def _approved(github: GitHubApi, steering: int, digest: str, admins: Sequence[Human]) -> bool:
    roles = {admin.id: Role.ADMIN for admin in admins}
    context = IssueContext(
        status=Status.INBOX,
        issue_type=IssueType.CODE,
        spec_hash=None,
        head_sha=None,
        adoption_report_hash=digest,
        workflow_diff_hash=None,
        unblock_targets=frozenset(),
        existing_issues=frozenset(),
    )
    for item in extract_commands(github.list_comments(steering), roles):
        if (item.command.verb, item.command.kind) == ("approve", "adopt") and isinstance(
            validate_command(item.command, item.actor, item.facts, context), Valid
        ):
            return True
    return False


def adopt(
    github: GitHubApi,
    facts: RepoFacts,
    tree: Mapping[str, str],
    inputs: InitInputs,
    owner: str,
    repo: str,
    say: Callable[[str], None] = lambda _line: None,
) -> Outcome:
    check_prerequisites(facts, owner, repo)
    if github.get_ref(inputs.staging_branch) is None:
        github.create_branch(inputs.staging_branch, facts.default_branch)
        say(f"created {inputs.staging_branch} from {facts.default_branch}")
    for name in (EVENTS_BRANCH, EVIDENCE_BRANCH):
        ensure_orphan_branch(github, name, _AUTHOR)
    steering = _steering_issue(github)
    report = assess(facts, tree, inputs.ci_local)
    marker = _report_marker(report.digest)
    posted = any(has_marker(c.body, marker) for c in github.list_comments(steering))
    if not posted:
        body = f"{report.text}\n\nAn admin approves this with:\n\n```\n/approve adopt {report.digest}\n```\n\n{marker}"
        github.add_comment(steering, body)
        say(f"posted the adoption report in #{steering}")
    if not _approved(github, steering, report.digest, inputs.admins):
        return Outcome("waiting_for_approval", steering, report)
    result = run_init(github, replace(inputs, steering_issue=steering), facts.default_branch)
    return Outcome("opened", steering, report, result)


__all__ = ["STEERING_TITLE", "AdoptStop", "Outcome", "RepoFacts", "Report", "adopt", "assess"]
