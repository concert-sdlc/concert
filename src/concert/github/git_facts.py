"""Repository facts for handoff acceptance, read through the GitHub interface."""

from collections.abc import Mapping

from concert.core.derive import IssueState
from concert.core.handoff import Handoff, RunFacts
from concert.core.spec_hash import spec_tree_hash
from concert.github.api import GitHubApi, NotFound


class GitFacts:
    """Facts about a run's commits (its WIP ref, or the issue branch) at the end of a run."""

    def __init__(self, github: GitHubApi, base_branch: str) -> None:
        self._github = github
        self._base = base_branch

    def facts(
        self, issue: int, handoff: Handoff, state: IssueState, run_start_head: str | None
    ) -> RunFacts:
        sha = handoff.commit_sha
        exists = False
        if sha is not None:
            # a worker pushes its commits to its own WIP ref; the PM advances pm/<issue> later
            wip = f"pm-wip/{handoff.runner}/{issue}/{handoff.run_id}"
            for ref in (wip, state.branch or f"pm/{issue}"):
                try:
                    exists = sha in {c.sha for c in self._github.list_commits(self._base, ref)}
                except NotFound:
                    continue
                if exists:
                    break
            if not exists and sha == run_start_head:
                exists = True  # a run that made no commits reports the head it started from
        # not dict(): a real tree loads a file only when asked, and only the spec files are needed
        tree: Mapping[str, str] = self._github.read_tree(sha) if exists and sha is not None else {}
        return RunFacts(
            run_start_head=run_start_head,
            approved_spec_rev=state.approved_spec_rev,
            commit_exists_on_remote=exists,
            tree_hash_at_commit=spec_tree_hash(tree, issue) if exists else None,
            paths_at_commit=frozenset(tree),
            current_head=state.head_sha,
        )
