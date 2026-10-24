"""The GitHub surface Concert uses.

The `github` module is the only part of Concert that talks to GitHub. This file holds the
interface and the data types; a real client and the in-memory fake used by the tests both
implement `GitHubApi`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from concert.core.commit_binding import CommitMeta


class GitHubError(Exception):
    """Base class for API errors."""


class NotFound(GitHubError):
    pass


class Forbidden(GitHubError):
    pass


class Conflict(GitHubError):
    """The request conflicts with the current state (for example a non-fast-forward)."""


class StaleHead(Conflict):
    """A merge named an expected head that is no longer the PR head."""


@dataclass(frozen=True)
class Identity:
    """Identities are ids, not names: a login can be renamed and re-registered."""

    user_id: int
    login: str
    is_bot: bool


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    author: Identity
    state: str  # "open" or "closed"


@dataclass(frozen=True)
class Comment:
    id: int
    issue: int
    author: Identity
    body: str
    created_at: int
    edited_at: int | None  # visible on GitHub as lastEditedAt


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str  # head branch name
    base: str
    title: str
    draft: bool
    state: str  # "open", "merged" or "closed"


class GitHubApi(Protocol):
    # issues and comments
    def create_issue(self, title: str, body: str) -> Issue: ...
    def get_issue(self, number: int) -> Issue: ...
    def close_issue(self, number: int) -> Issue: ...
    def reopen_issue(self, number: int) -> Issue: ...
    def add_comment(self, issue: int, body: str) -> Comment: ...
    def edit_comment(self, comment_id: int, body: str) -> Comment: ...
    def delete_comment(self, comment_id: int) -> None: ...
    def list_comments(self, issue: int, after_id: int = 0) -> Sequence[Comment]: ...
    # Projects v2 Status field
    def set_status(self, issue: int, status: str) -> None: ...
    def get_status(self, issue: int) -> str | None: ...
    # git
    def create_branch(self, name: str, from_ref: str) -> str: ...
    def commit(
        self,
        branch: str,
        message: str,
        changes: Mapping[str, str | None],
        author: str,
        committer: str,
    ) -> str: ...
    def get_ref(self, name: str) -> str | None: ...
    def update_ref(self, name: str, sha: str, force: bool = False) -> None: ...
    def delete_ref(self, name: str) -> None: ...
    def list_commits(self, base: str, head: str) -> Sequence[CommitMeta]: ...
    def read_tree(self, sha: str) -> Mapping[str, str]: ...
    def commit_parents(self, sha: str) -> tuple[str, ...]: ...
    # pull requests
    def create_pr(self, head: str, base: str, title: str) -> PullRequest: ...
    def get_pr(self, number: int) -> PullRequest: ...
    def mark_ready(self, number: int) -> PullRequest: ...
    def merge_pr(self, number: int, expected_head: str) -> str: ...
