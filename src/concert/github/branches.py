"""Service branches that hold no project code: created on first use, never rewritten."""

from concert.github.api import Conflict, GitHubApi

EVENTS_BRANCH = "concert-events"


def ensure_orphan_branch(github: GitHubApi, name: str, author: str) -> None:
    if github.get_ref(name) is not None:
        return
    try:
        github.create_orphan_branch(
            name, {"README.md": f"Managed by Concert: {name}\n"}, "init", author
        )
    except Conflict:  # another runner created it first
        pass
