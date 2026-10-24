"""Repository facts for handoff acceptance, read through the GitHub interface."""

from concert.core.derive import IssueState
from concert.core.handoff import Handoff, RunFacts
from concert.core.spec_hash import spec_tree_hash
from concert.github.api import GitHubApi, NotFound


class GitFacts:
    """Facts about an issue's branch `pm/<issue>` at the end of a run."""

    def __init__(self, github: GitHubApi, base_branch: str) -> None:
        self._github = github
        self._base = base_branch

    def facts(
        self, issue: int, handoff: Handoff, state: IssueState, run_start_head: str | None
    ) -> RunFacts:
        sha = handoff.commit_sha
        branch = f"pm/{issue}"
        exists = False
        if sha is not None:
            try:
                exists = sha in {c.sha for c in self._github.list_commits(self._base, branch)}
            except NotFound:
                exists = False
            if not exists and sha == run_start_head:
                exists = True  # a run that made no commits reports the head it started from
        tree = dict(self._github.read_tree(sha)) if exists and sha is not None else {}
        return RunFacts(
            run_start_head=run_start_head,
            approved_spec_rev=state.approved_spec_rev,
            commit_exists_on_remote=exists,
            tree_hash_at_commit=spec_tree_hash(tree, issue) if exists else None,
            paths_at_commit=frozenset(tree),
        )
