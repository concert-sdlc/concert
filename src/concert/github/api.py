"""The GitHub surface Concert uses.

The `github` module is the only part of Concert that talks to GitHub. This file holds the
interface and the data types; a real client and the in-memory fake used by the tests both
implement `GitHubApi`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

from concert.core.ci import CheckRun, PrReview
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


class BinaryBlob(str):
    """Stands in for a file that is not UTF-8 text.

    It compares equal exactly when the blob is the same, and a commit that includes it reuses the
    blob by id, so a repository with an image in it can be read, compared and committed on
    without ever decoding the bytes. Agents are never given one (they work on text only).
    """

    sha: str

    def __new__(cls, sha: str) -> Self:
        blob = super().__new__(cls, f"\0binary-blob:{sha}")
        blob.sha = sha
        return blob


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
    merge_sha: str | None = None  # the merge commit once merged


class GitHubApi(Protocol):
    # issues and comments
    def create_issue(self, title: str, body: str) -> Issue: ...
    def get_issue(self, number: int) -> Issue: ...
    def list_open_issues(self) -> Sequence[int]: ...  # numbers of open issues, never pull requests
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
    def create_orphan_branch(
        self, name: str, files: Mapping[str, str], message: str, author: str
    ) -> str: ...  # a branch with no parents (the event mirror, heartbeats, evidence)
    def get_ref(self, name: str) -> str | None: ...
    def update_ref(self, name: str, sha: str, force: bool = False) -> None: ...
    def delete_ref(self, name: str) -> None: ...
    def list_commits(self, base: str, head: str) -> Sequence[CommitMeta]: ...
    def read_tree(self, sha: str) -> Mapping[str, str]: ...
    def read_file(self, ref: str, path: str) -> str | None: ...
    def commit_parents(self, sha: str) -> tuple[str, ...]: ...
    # checks
    def list_check_runs(self, sha: str) -> Sequence[CheckRun]: ...
    # pull requests
    def create_pr(self, head: str, base: str, title: str) -> PullRequest: ...
    def get_pr(self, number: int) -> PullRequest: ...
    def list_pr_reviews(self, number: int) -> Sequence[PrReview]: ...
    def find_pr(self, head: str) -> PullRequest | None: ...  # the open PR for a head branch
    def find_revert(self, number: int) -> PullRequest | None: ...  # the open PR reverting PR number
    def dispatch_ci(self, ref: str) -> None: ...  # run concert-ci on a branch (workflow_dispatch)
    def mark_ready(self, number: int) -> PullRequest: ...
    def mark_draft(self, number: int) -> PullRequest: ...
    def revert_pr(self, number: int) -> PullRequest: ...  # a ready PR that undoes a merged one
    def merge_pr(self, number: int, expected_head: str) -> str: ...
