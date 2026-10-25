"""In-memory fake of the GitHub API subset Concert uses.

Deterministic: time is a counter advanced with `tick()`, SHAs are derived from content plus
a counter. Every call is made through a client bound to an identity, like a token, so
authorship and permissions behave as on GitHub. This first slice covers issues, comments
(with edit and delete metadata), the Projects Status field, commits and refs, and PRs.
Rulesets, workflow runs, rate limits and the activity feed are added with their own tests.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from concert.core.ci import CheckRun, PrReview
from concert.core.commit_binding import CommitMeta
from concert.github.api import (
    Comment,
    Conflict,
    Forbidden,
    Identity,
    Issue,
    NotFound,
    PullRequest,
    StaleHead,
)


@dataclass
class _Commit:
    sha: str
    parents: tuple[str, ...]
    author: str
    committer: str
    message: str
    tree: dict[str, str]
    seq: int


@dataclass
class _Pr:
    pr: PullRequest
    merged_sha: str | None = None


@dataclass
class FakeGitHub:
    now: int = 0
    numbers: int = 0
    comment_ids: int = 0
    commit_seq: int = 0
    issues: dict[int, Issue] = field(default_factory=dict[int, Issue])
    comments: dict[int, Comment] = field(default_factory=dict[int, Comment])
    status: dict[int, str] = field(default_factory=dict[int, str])
    commits: dict[str, _Commit] = field(default_factory=dict[str, _Commit])
    refs: dict[str, str] = field(default_factory=dict[str, str])
    prs: dict[int, _Pr] = field(default_factory=dict[int, _Pr])
    check_runs: list[CheckRun] = field(default_factory=list[CheckRun])
    dispatched: list[str] = field(default_factory=list[str])
    reviews: dict[int, list[PrReview]] = field(default_factory=dict[int, list[PrReview]])

    def tick(self, seconds: int) -> None:
        self.now += seconds

    def client(self, actor: Identity) -> "FakeClient":
        return FakeClient(self, actor)

    def seed_branch(self, name: str, files: Mapping[str, str]) -> str:
        """Create a branch with a root commit (test setup)."""
        return self.new_commit(name, (), "seed", "seed", "seed", dict(files))

    def add_check_run(
        self, sha: str, name: str, conclusion: str | None, workflow_hash: str
    ) -> None:
        """Test setup: a CI check run on a commit (conclusion None = still running)."""
        done = self.now if conclusion is not None else None
        self.check_runs.append(CheckRun(name, sha, conclusion, workflow_hash, done))

    def comments_text(self, issue: int) -> str:
        """Test helper: every comment body on an issue, joined."""
        return "\n".join(c.body for c in self.comments.values() if c.issue == issue)

    def add_review(self, pr: int, user_id: int, state: str, commit_id: str) -> None:
        """Test setup: a pull request review."""
        self.reviews.setdefault(pr, []).append(PrReview(user_id, state, commit_id))

    # --- internals ---------------------------------------------------------------------------

    def next_number(self) -> int:
        self.numbers += 1
        return self.numbers

    def new_commit(
        self,
        branch: str,
        parents: tuple[str, ...],
        message: str,
        author: str,
        committer: str,
        tree: dict[str, str],
    ) -> str:
        self.commit_seq += 1
        material = f"{parents}|{message}|{sorted(tree.items())}|{self.commit_seq}"
        sha = hashlib.sha1(material.encode()).hexdigest()  # fake ids, not security
        self.commits[sha] = _Commit(sha, parents, author, committer, message, tree, self.commit_seq)
        self.refs[branch] = sha
        return sha

    def resolve(self, ref: str) -> str:
        if ref in self.refs:
            return self.refs[ref]
        if ref in self.commits:
            return ref
        raise NotFound(f"no ref or commit {ref!r}")

    def ancestors(self, sha: str) -> set[str]:
        seen: set[str] = set()
        stack = [sha]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.commits[current].parents)
        return seen

    def merge_base(self, left: str, right: str) -> str | None:
        common = self.ancestors(left) & self.ancestors(right)
        if not common:
            return None
        return max(common, key=lambda sha: self.commits[sha].seq)


@dataclass
class FakeClient:
    """The fake as seen by one identity (one token)."""

    fake: FakeGitHub
    actor: Identity

    # --- issues and comments -----------------------------------------------------------------

    def create_issue(self, title: str, body: str) -> Issue:
        issue = Issue(self.fake.next_number(), title, body, self.actor, "open")
        self.fake.issues[issue.number] = issue
        return issue

    def get_issue(self, number: int) -> Issue:
        if number in self.fake.issues:
            return self.fake.issues[number]
        if number in self.fake.prs:  # GitHub's /issues/{n} answers for pull requests too
            pr = self.fake.prs[number].pr
            return Issue(
                number, pr.title, "", self.actor, "open" if pr.state == "open" else "closed"
            )
        raise NotFound(f"no issue #{number}")

    def list_open_issues(self) -> Sequence[int]:
        return sorted(n for n, i in self.fake.issues.items() if i.state == "open")

    def close_issue(self, number: int) -> Issue:
        return self._set_state(number, "closed")

    def reopen_issue(self, number: int) -> Issue:
        return self._set_state(number, "open")

    def _set_state(self, number: int, state: str) -> Issue:
        issue = replace(self.get_issue(number), state=state)
        self.fake.issues[number] = issue
        return issue

    def add_comment(self, issue: int, body: str) -> Comment:
        self.get_issue(issue)
        self.fake.comment_ids += 1
        comment = Comment(self.fake.comment_ids, issue, self.actor, body, self.fake.now, None)
        self.fake.comments[comment.id] = comment
        return comment

    def _own_comment(self, comment_id: int) -> Comment:
        try:
            comment = self.fake.comments[comment_id]
        except KeyError:
            raise NotFound(f"no comment {comment_id}") from None
        if comment.author.user_id != self.actor.user_id:
            raise Forbidden("only the author can change this comment")
        return comment

    def edit_comment(self, comment_id: int, body: str) -> Comment:
        comment = replace(self._own_comment(comment_id), body=body, edited_at=self.fake.now)
        self.fake.comments[comment_id] = comment
        return comment

    def delete_comment(self, comment_id: int) -> None:
        self._own_comment(comment_id)
        del self.fake.comments[comment_id]

    def list_comments(self, issue: int, after_id: int = 0) -> Sequence[Comment]:
        self.get_issue(issue)
        found = [c for c in self.fake.comments.values() if c.issue == issue and c.id > after_id]
        return sorted(found, key=lambda c: c.id)

    def set_status(self, issue: int, status: str) -> None:
        self.get_issue(issue)
        self.fake.status[issue] = status

    def get_status(self, issue: int) -> str | None:
        self.get_issue(issue)
        return self.fake.status.get(issue)

    # --- git ---------------------------------------------------------------------------------

    def create_branch(self, name: str, from_ref: str) -> str:
        if name in self.fake.refs:
            raise Conflict(f"branch {name!r} already exists")
        sha = self.fake.resolve(from_ref)
        self.fake.refs[name] = sha
        return sha

    def commit(
        self,
        branch: str,
        message: str,
        changes: Mapping[str, str | None],
        author: str,
        committer: str,
    ) -> str:
        head = self.fake.resolve(branch)
        tree = dict(self.fake.commits[head].tree)
        for path, content in changes.items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
        return self.fake.new_commit(branch, (head,), message, author, committer, tree)

    def create_orphan_branch(
        self, name: str, files: Mapping[str, str], message: str, author: str
    ) -> str:
        if name in self.fake.refs:
            raise Conflict(f"branch {name!r} already exists")
        return self.fake.new_commit(name, (), message, author, author, dict(files))

    def get_ref(self, name: str) -> str | None:
        return self.fake.refs.get(name)

    def update_ref(self, name: str, sha: str, force: bool = False) -> None:
        if sha not in self.fake.commits:
            raise NotFound(f"no commit {sha!r}")
        current = self.fake.refs.get(name)
        if current is not None and not force and current not in self.fake.ancestors(sha):
            raise Conflict(f"{name!r} is not a fast-forward to {sha}")
        self.fake.refs[name] = sha

    def delete_ref(self, name: str) -> None:
        if name not in self.fake.refs:
            raise NotFound(f"no ref {name!r}")
        del self.fake.refs[name]

    def list_commits(self, base: str, head: str) -> Sequence[CommitMeta]:
        wanted = self.fake.ancestors(self.fake.resolve(head)) - self.fake.ancestors(
            self.fake.resolve(base)
        )
        ordered = sorted((self.fake.commits[sha] for sha in wanted), key=lambda c: c.seq)
        return [
            CommitMeta(
                sha=c.sha,
                parent=c.parents[0] if c.parents else "",
                author=c.author,
                committer=c.committer,
            )
            for c in ordered
        ]

    def read_tree(self, sha: str) -> Mapping[str, str]:
        return dict(self.fake.commits[self.fake.resolve(sha)].tree)

    def read_file(self, ref: str, path: str) -> str | None:
        return self.read_tree(ref).get(path)

    def commit_parents(self, sha: str) -> tuple[str, ...]:
        return self.fake.commits[self.fake.resolve(sha)].parents

    # --- checks ------------------------------------------------------------------------------

    def list_check_runs(self, sha: str) -> Sequence[CheckRun]:
        return [run for run in self.fake.check_runs if run.head_sha == sha]

    # --- pull requests -------------------------------------------------------------------------

    def create_pr(self, head: str, base: str, title: str) -> PullRequest:
        self.fake.resolve(head)
        self.fake.resolve(base)
        pr = PullRequest(self.fake.next_number(), head, base, title, draft=True, state="open")
        self.fake.prs[pr.number] = _Pr(pr)
        return pr

    def _pr(self, number: int) -> _Pr:
        try:
            return self.fake.prs[number]
        except KeyError:
            raise NotFound(f"no pull request #{number}") from None

    def get_pr(self, number: int) -> PullRequest:
        return self._pr(number).pr

    def mark_draft(self, number: int) -> PullRequest:
        record = self._pr(number)
        record.pr = replace(record.pr, draft=True)
        return record.pr

    def revert_pr(self, number: int) -> PullRequest:
        record = self._pr(number)
        if record.pr.state != "merged" or record.merged_sha is None:
            raise Conflict(f"pull request #{number} is not merged")
        merge = self.fake.commits[record.merged_sha]
        before = self.fake.commits[merge.parents[0]].tree
        base = record.pr.base
        tree = dict(self.fake.commits[self.fake.resolve(base)].tree)
        for path in set(merge.tree) | set(before):
            if merge.tree.get(path) == before.get(path):
                continue
            if tree.get(path) != merge.tree.get(path):
                raise Conflict(f"revert conflict in {path}")
            if path in before:
                tree[path] = before[path]
            else:
                tree.pop(path, None)
        branch = f"revert-{number}-{record.pr.head}"  # GitHub's name for the branch
        self.create_branch(branch, base)
        self.fake.new_commit(
            branch,
            (self.fake.resolve(base),),
            f"Revert #{number}",
            self.actor.login,
            self.actor.login,
            tree,
        )
        pr = PullRequest(
            self.fake.next_number(), branch, base, f"Revert #{number}", draft=False, state="open"
        )
        self.fake.prs[pr.number] = _Pr(pr)
        return pr

    def list_pr_reviews(self, number: int) -> Sequence[PrReview]:
        self._pr(number)
        return list(self.fake.reviews.get(number, []))

    def find_pr(self, head: str) -> PullRequest | None:
        return next(
            (r.pr for r in self.fake.prs.values() if r.pr.head == head and r.pr.state == "open"),
            None,
        )

    def find_revert(self, number: int) -> PullRequest | None:
        prefix = f"revert-{number}-"
        return next(
            (
                r.pr
                for r in self.fake.prs.values()
                if r.pr.head.startswith(prefix) and r.pr.state == "open"
            ),
            None,
        )

    def dispatch_ci(self, ref: str) -> None:
        """A new run starts on the branch head: its checks are running until a test sets them."""
        self.fake.dispatched.append(ref)
        head = self.fake.resolve(ref)
        self.fake.check_runs = [
            CheckRun(r.name, r.head_sha, None, r.workflow_hash, None) if r.head_sha == head else r
            for r in self.fake.check_runs
        ]

    def mark_ready(self, number: int) -> PullRequest:
        record = self._pr(number)
        record.pr = replace(record.pr, draft=False)
        return record.pr

    def merge_pr(self, number: int, expected_head: str) -> str:
        record = self._pr(number)
        pr = record.pr
        if pr.state != "open":
            raise Conflict(f"pull request #{number} is {pr.state}")
        if pr.draft:
            raise Conflict(f"pull request #{number} is a draft")
        head = self.fake.resolve(pr.head)
        if head != expected_head:
            raise StaleHead(f"expected head {expected_head}, the PR head is {head}")
        base = self.fake.resolve(pr.base)
        ancestor = self.fake.merge_base(base, head)
        ancestor_tree = self.fake.commits[ancestor].tree if ancestor else {}
        base_tree = self.fake.commits[base].tree
        head_tree = self.fake.commits[head].tree
        merged = dict(base_tree)
        for path in set(head_tree) | set(ancestor_tree):
            theirs, original = head_tree.get(path), ancestor_tree.get(path)
            if theirs == original:
                continue
            if base_tree.get(path) not in (original, theirs):
                raise Conflict(f"merge conflict in {path}")
            if theirs is None:
                merged.pop(path, None)
            else:
                merged[path] = theirs
        sha = self.fake.new_commit(
            pr.base, (base, head), f"Merge #{number}", self.actor.login, self.actor.login, merged
        )
        record.pr = replace(pr, state="merged", merge_sha=sha)
        record.merged_sha = sha
        return sha
