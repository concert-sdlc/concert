"""The GitHub interface over the REST and GraphQL APIs.

`RestGitHub` implements `GitHubApi` for one repository (and, for the Status field, one Projects v2
board). It is thin on purpose: each method is one or two API calls with the response shaped into
the interface's data types, so the poll loop's behaviour is the same against the fake and the
real service. Things only GraphQL offers (the board field, marking a PR ready, reverting a PR)
go through GraphQL.
"""

import base64
import hashlib
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from concert.core.ci import CheckRun, PrReview
from concert.core.commit_binding import CommitMeta
from concert.github.api import (
    BinaryBlob,
    Comment,
    Conflict,
    GitHubError,
    Identity,
    Issue,
    NotFound,
    PullRequest,
    StaleHead,
)
from concert.github.http import GitHubHttp

_SHA = re.compile(r"^[0-9a-f]{40}$")
Json = dict[str, Any]
# Seconds to wait between merge attempts while GitHub catches up after a push (about 30 in all).
_MERGE_SETTLE = (1.0, 2.0, 4.0, 8.0, 15.0)


@dataclass(frozen=True)
class ProjectBoard:
    """The Projects v2 board whose single-select field mirrors the derived status."""

    owner: str
    number: int
    field_name: str = "Status"
    is_org: bool = True


class LazyTree(Mapping[str, str]):
    """A repository tree whose file contents are fetched only when asked for.

    Listing paths and comparing two trees by blob id costs no content requests; `tree[path]` and
    `dict(tree)` do.
    """

    def __init__(self, blob_ids: Mapping[str, str], fetch: Any) -> None:
        self.blob_ids: Mapping[str, str] = dict(blob_ids)
        self._fetch = fetch
        self._cache: dict[str, str] = {}

    def __getitem__(self, path: str) -> str:
        if path not in self.blob_ids:
            raise KeyError(path)
        if path not in self._cache:
            self._cache[path] = cast("str", self._fetch(self.blob_ids[path]))
        return self._cache[path]

    def __iter__(self) -> Iterator[str]:
        return iter(self.blob_ids)

    def __len__(self) -> int:
        return len(self.blob_ids)


@dataclass
class RestGitHub:
    http: GitHubHttp
    owner: str
    repo: str
    board: ProjectBoard | None = None
    sleep: Callable[[float], None] = time.sleep
    _project: Json = field(default_factory=dict[str, Any])
    _workflow_hashes: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])

    # --- issues and comments -----------------------------------------------------------------

    def _repo(self, path: str = "") -> str:
        return f"/repos/{self.owner}/{self.repo}{path}"

    def create_issue(self, title: str, body: str) -> Issue:
        return _issue(self.http.json("POST", self._repo("/issues"), {"title": title, "body": body}))

    def get_issue(self, number: int) -> Issue:
        return _issue(self.http.json("GET", self._repo(f"/issues/{number}")))

    def list_open_issues(self) -> Sequence[int]:
        # GraphQL, not REST /issues: live, REST omitted a new issue for ~2 s and kept listing a
        # closed one, while GraphQL reflected both writes at once. It never returns pull requests.
        numbers: list[int] = []
        cursor: str | None = None
        while True:
            data = self.http.graphql(
                "query($o:String!,$r:String!,$after:String){repository(owner:$o,name:$r)"
                "{issues(states:OPEN,first:100,after:$after){nodes{number} "
                "pageInfo{hasNextPage endCursor}}}}",
                {"o": self.owner, "r": self.repo, "after": cursor},
            )
            page = data["repository"]["issues"]
            numbers.extend(int(n["number"]) for n in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return sorted(numbers)
            cursor = str(page["pageInfo"]["endCursor"])

    def close_issue(self, number: int) -> Issue:
        return _issue(
            self.http.json(
                "PATCH",
                self._repo(f"/issues/{number}"),
                {"state": "closed", "state_reason": "completed"},
            )
        )

    def reopen_issue(self, number: int) -> Issue:
        return _issue(self.http.json("PATCH", self._repo(f"/issues/{number}"), {"state": "open"}))

    def add_comment(self, issue: int, body: str) -> Comment:
        return _comment(
            self.http.json("POST", self._repo(f"/issues/{issue}/comments"), {"body": body}), issue
        )

    def edit_comment(self, comment_id: int, body: str) -> Comment:
        raw = self.http.json("PATCH", self._repo(f"/issues/comments/{comment_id}"), {"body": body})
        return _comment(raw, _issue_number(raw))

    def delete_comment(self, comment_id: int) -> None:
        self.http.json("DELETE", self._repo(f"/issues/comments/{comment_id}"))

    def list_comments(self, issue: int, after_id: int = 0) -> Sequence[Comment]:
        pages = self.http.paged(self._repo(f"/issues/{issue}/comments"), {"per_page": 100})
        return [_comment(raw, issue) for raw in pages if int(raw["id"]) > after_id]

    # --- the Projects v2 Status field ------------------------------------------------------------

    def set_status(self, issue: int, status: str) -> None:
        board = self._resolved_board()
        option = board["options"].get(status)
        if option is None:
            raise GitHubError(f"the board has no {status!r} option in {board['field_name']}")
        node = self._issue_node(issue)
        item = self._item_for(node, board) or self._add_item(node, board)
        self.http.graphql(
            "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
            "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}})"
            "{projectV2Item{id}}}",
            {"p": board["id"], "i": item, "f": board["field_id"], "o": option},
        )

    def get_status(self, issue: int) -> str | None:
        board = self._resolved_board()
        node = self._issue_node(issue)
        data = self.http.graphql(
            "query($n:ID!,$f:String!){node(id:$n){... on Issue{projectItems(first:20){nodes{id "
            "project{id} fieldValueByName(name:$f){... on "
            "ProjectV2ItemFieldSingleSelectValue{name}}}}}}}",
            {"n": node, "f": board["field_name"]},
        )
        for item in _nodes(data, "node", "projectItems"):
            if item["project"]["id"] == board["id"] and item.get("fieldValueByName"):
                return str(item["fieldValueByName"]["name"])
        return None

    def _issue_node(self, issue: int) -> str:
        return str(self.http.json("GET", self._repo(f"/issues/{issue}"))["node_id"])

    def _item_for(self, node: str, board: Json) -> str | None:
        data = self.http.graphql(
            "query($n:ID!){node(id:$n){... on Issue{projectItems(first:20){nodes{id project{id}}}}}}",
            {"n": node},
        )
        for item in _nodes(data, "node", "projectItems"):
            if item["project"]["id"] == board["id"]:
                return str(item["id"])
        return None

    def _add_item(self, node: str, board: Json) -> str:
        data = self.http.graphql(
            "mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c})"
            "{item{id}}}",
            {"p": board["id"], "c": node},
        )
        return str(data["addProjectV2ItemById"]["item"]["id"])

    def _resolved_board(self) -> Json:
        if self._project:
            return self._project
        if self.board is None:
            raise GitHubError("no Projects v2 board is configured")
        kind = "organization" if self.board.is_org else "user"
        data = self.http.graphql(
            f"query($o:String!,$n:Int!,$f:String!){{{kind}(login:$o){{projectV2(number:$n){{id "
            "field(name:$f){... on ProjectV2SingleSelectField{id options{id name}}}}}}",
            {"o": self.board.owner, "n": self.board.number, "f": self.board.field_name},
        )
        project = data[kind]["projectV2"]
        options = {str(o["name"]): str(o["id"]) for o in project["field"]["options"]}
        self._project = {
            "id": str(project["id"]),
            "field_id": str(project["field"]["id"]),
            "field_name": self.board.field_name,
            "options": options,
        }
        return self._project

    # --- git ---------------------------------------------------------------------------------

    def _sha_of(self, ref: str) -> str:
        if _SHA.match(ref):
            return ref
        sha = self.get_ref(ref)
        if sha is None:
            raise NotFound(f"no ref {ref!r}")
        return sha

    def create_branch(self, name: str, from_ref: str) -> str:
        sha = self._sha_of(from_ref)
        self.http.json("POST", self._repo("/git/refs"), {"ref": f"refs/heads/{name}", "sha": sha})
        return sha

    def create_orphan_branch(
        self, name: str, files: Mapping[str, str], message: str, author: str
    ) -> str:
        entries = [
            {"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()
        ]
        tree = self.http.json("POST", self._repo("/git/trees"), {"tree": entries})
        created = self.http.json(
            "POST",
            self._repo("/git/commits"),
            {
                "message": message,
                "tree": tree["sha"],
                "parents": [],
                "author": _person(author),
                "committer": _person(author),
            },
        )
        self.http.json(
            "POST", self._repo("/git/refs"), {"ref": f"refs/heads/{name}", "sha": created["sha"]}
        )
        return str(created["sha"])

    def get_ref(self, name: str) -> str | None:
        try:
            raw = self.http.json("GET", self._repo(f"/git/ref/heads/{name}"))
        except NotFound:
            return None
        if isinstance(raw, list):  # a prefix matched several refs: not this branch
            return None
        return str(cast("Json", raw)["object"]["sha"])

    def update_ref(self, name: str, sha: str, force: bool = False) -> None:
        self.http.json("PATCH", self._repo(f"/git/refs/heads/{name}"), {"sha": sha, "force": force})

    def delete_ref(self, name: str) -> None:
        self.http.json("DELETE", self._repo(f"/git/refs/heads/{name}"))

    def commit(
        self,
        branch: str,
        message: str,
        changes: Mapping[str, str | None],
        author: str,
        committer: str,
    ) -> str:
        head = self._sha_of(branch)
        base_tree = self.http.json("GET", self._repo(f"/git/commits/{head}"))["tree"]["sha"]
        modes = self._modes(str(base_tree))
        entries: list[Json] = []
        for path, content in changes.items():
            entry: Json = {"path": path, "mode": modes.get(path, "100644"), "type": "blob"}
            if content is None:
                entry["sha"] = None
            elif isinstance(content, BinaryBlob):
                entry["sha"] = content.sha  # reuse the blob: the bytes were never decoded
            else:
                entry["content"] = content
            entries.append(entry)
        tree = self.http.json(
            "POST", self._repo("/git/trees"), {"base_tree": base_tree, "tree": entries}
        )
        created = self.http.json(
            "POST",
            self._repo("/git/commits"),
            {
                "message": message,
                "tree": tree["sha"],
                "parents": [head],
                "author": _person(author),
                "committer": _person(committer),
            },
        )
        self.update_ref(branch, str(created["sha"]))
        return str(created["sha"])

    def _modes(self, tree: str) -> dict[str, str]:
        """File modes in the base tree, so editing an executable script keeps it executable."""
        try:
            raw = self.http.json("GET", self._repo(f"/git/trees/{tree}"), None, {"recursive": 1})
        except NotFound:
            return {}
        return {
            str(e["path"]): str(e["mode"]) for e in raw.get("tree", []) if e.get("type") == "blob"
        }

    def list_commits(self, base: str, head: str) -> Sequence[CommitMeta]:
        pages = self.http.paged(self._repo(f"/compare/{base}...{head}"), {"per_page": 100})
        commits: list[Json] = []
        for page in pages:
            commits.extend(cast("list[Json]", page.get("commits", [])))
        return [
            CommitMeta(
                sha=str(c["sha"]),
                parent=str(c["parents"][0]["sha"]) if c["parents"] else "",
                author=str(c["commit"]["author"]["name"]),
                committer=str(c["commit"]["committer"]["name"]),
            )
            for c in commits
        ]

    def read_tree(self, sha: str) -> Mapping[str, str]:
        raw = self.http.json(
            "GET", self._repo(f"/git/trees/{self._sha_of(sha)}"), None, {"recursive": 1}
        )
        if raw.get("truncated"):
            raise GitHubError("the repository tree is too large to list in one request")
        ids = {str(e["path"]): str(e["sha"]) for e in raw["tree"] if e["type"] == "blob"}
        return LazyTree(ids, self._blob)

    def _blob(self, sha: str) -> str:
        raw = self.http.json("GET", self._repo(f"/git/blobs/{sha}"))
        try:
            return base64.b64decode(raw["content"]).decode("utf-8")
        except UnicodeDecodeError:
            return BinaryBlob(sha)

    def read_file(self, ref: str, path: str) -> str | None:
        try:
            raw = self.http.json("GET", self._repo(f"/contents/{path}"), None, {"ref": ref})
        except NotFound:
            return None
        if not isinstance(raw, dict):
            return None
        content = cast("Json", raw)
        if content.get("encoding") != "base64":
            # Over 1 MB the contents API sends no content at all: read the blob instead, never
            # report the file as empty (the event mirror would overwrite its own history).
            if not content.get("sha"):
                return None
            return self._blob(str(content["sha"]))
        try:
            return base64.b64decode(content["content"]).decode("utf-8")
        except UnicodeDecodeError:
            return BinaryBlob(str(content["sha"]))

    def commit_parents(self, sha: str) -> tuple[str, ...]:
        raw = self.http.json("GET", self._repo(f"/git/commits/{self._sha_of(sha)}"))
        return tuple(str(p["sha"]) for p in raw["parents"])

    # --- checks --------------------------------------------------------------------------------

    def list_check_runs(self, sha: str) -> Sequence[CheckRun]:
        pages = self.http.paged(self._repo(f"/commits/{sha}/check-runs"), {"per_page": 100})
        runs: list[Json] = []
        for page in pages:
            runs.extend(cast("list[Json]", page.get("check_runs", [])))
        workflows = self._workflow_paths(sha)
        found: list[CheckRun] = []
        for run in runs:
            path = workflows.get(int(cast("Json", run.get("check_suite") or {}).get("id", 0)))
            found.append(
                CheckRun(
                    name=str(run["name"]),
                    head_sha=str(run["head_sha"]),
                    conclusion=run.get("conclusion"),
                    workflow_hash=self._workflow_hash(sha, path) if path else "",
                )
            )
        return found

    def _workflow_paths(self, sha: str) -> dict[int, str]:
        pages = self.http.paged(self._repo("/actions/runs"), {"head_sha": sha, "per_page": 100})
        paths: dict[int, str] = {}
        for page in pages:
            for run in cast("list[Json]", page.get("workflow_runs", [])):
                paths[int(run["check_suite_id"])] = str(run["path"]).split("@")[0]
        return paths

    def _workflow_hash(self, sha: str, path: str) -> str:
        key = (sha, path)
        if key not in self._workflow_hashes:
            text = self.read_file(sha, path)
            self._workflow_hashes[key] = hashlib.sha256((text or "").encode()).hexdigest()
        return self._workflow_hashes[key]

    # --- pull requests -------------------------------------------------------------------------

    def create_pr(self, head: str, base: str, title: str) -> PullRequest:
        raw = self.http.json(
            "POST",
            self._repo("/pulls"),
            {"title": title, "head": head, "base": base, "draft": True},
        )
        return _pr(raw)

    def get_pr(self, number: int) -> PullRequest:
        return _pr(self.http.json("GET", self._repo(f"/pulls/{number}")))

    def find_pr(self, head: str) -> PullRequest | None:
        raw = self.http.json(
            "GET", self._repo("/pulls"), None, {"head": f"{self.owner}:{head}", "state": "open"}
        )
        return _pr(raw[0]) if raw else None

    def find_revert(self, number: int) -> PullRequest | None:
        # GitHub names the branch revert-<number>-<head branch of the reverted PR>
        pages = self.http.paged(self._repo("/pulls"), {"state": "open", "per_page": 100})
        prefix = f"revert-{number}-"
        found = next((raw for raw in pages if str(raw["head"]["ref"]).startswith(prefix)), None)
        return _pr(found) if found else None

    def mark_ready(self, number: int) -> PullRequest:
        node = self.http.json("GET", self._repo(f"/pulls/{number}"))["node_id"]
        self.http.graphql(
            "mutation($p:ID!){markPullRequestReadyForReview(input:{pullRequestId:$p})"
            "{pullRequest{id}}}",
            {"p": node},
        )
        return self.get_pr(number)

    def mark_draft(self, number: int) -> PullRequest:
        node = self.http.json("GET", self._repo(f"/pulls/{number}"))["node_id"]
        self.http.graphql(
            "mutation($p:ID!){convertPullRequestToDraft(input:{pullRequestId:$p}){pullRequest{id}}}",
            {"p": node},
        )
        return self.get_pr(number)

    def merge_pr(self, number: int, expected_head: str) -> str:
        for delay in (*_MERGE_SETTLE, None):
            try:
                raw = self.http.json(
                    "PUT",
                    self._repo(f"/pulls/{number}/merge"),
                    {"sha": expected_head, "merge_method": "merge"},
                )
            except Conflict as error:
                text = str(error).lower()
                if "modified" in text:
                    raise StaleHead(str(error)) from error
                if "out of date" in text:  # also GitHub's wording for "needs updating"
                    pr = self.http.json("GET", self._repo(f"/pulls/{number}"))
                    if str(pr["head"]["sha"]) != expected_head:
                        raise StaleHead(str(error)) from error
                raise
            except GitHubError as error:
                if not str(error).startswith("405"):
                    raise
                # Live: right after a push the PR still shows the old head and `mergeable` is
                # null, and the merge answers 405 even for the right sha. Wait for it to settle.
                pr = self.http.json("GET", self._repo(f"/pulls/{number}"))
                at_head = str(pr["head"]["sha"]) == expected_head
                if (at_head and pr.get("mergeable") is not None) or delay is None:
                    if not at_head:
                        raise StaleHead(str(error)) from error
                    raise Conflict(str(error)) from error
                self.sleep(delay)
            else:
                return str(raw["sha"])
        raise AssertionError("unreachable")  # pragma: no cover

    def revert_pr(self, number: int) -> PullRequest:
        pr = self.http.json("GET", self._repo(f"/pulls/{number}"))
        if not pr.get("merged"):
            raise Conflict(f"pull request #{number} is not merged")
        data = self.http.graphql(
            "mutation($p:ID!,$t:String!){revertPullRequest(input:{pullRequestId:$p,title:$t,"
            "draft:false}){revertPullRequest{number}}}",
            {"p": pr["node_id"], "t": f"Revert #{number}"},
        )
        return self.get_pr(int(data["revertPullRequest"]["revertPullRequest"]["number"]))

    def list_pr_reviews(self, number: int) -> Sequence[PrReview]:
        pages = self.http.paged(self._repo(f"/pulls/{number}/reviews"), {"per_page": 100})
        return [
            PrReview(
                user_id=int(r["user"]["id"]), state=str(r["state"]), commit_id=str(r["commit_id"])
            )
            for r in pages
            if r.get("user")
        ]


# --- shaping --------------------------------------------------------------------------------------


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


def _identity(raw: Json) -> Identity:
    return Identity(
        user_id=int(raw["id"]), login=str(raw["login"]), is_bot=raw.get("type") == "Bot"
    )


def _issue(raw: Json) -> Issue:
    return Issue(
        number=int(raw["number"]),
        title=str(raw["title"]),
        body=str(raw.get("body") or ""),
        author=_identity(raw["user"]),
        state=str(raw["state"]),
    )


def _issue_number(raw: Json) -> int:
    return int(str(raw["issue_url"]).rstrip("/").rsplit("/", 1)[1])


def _comment(raw: Json, issue: int) -> Comment:
    created, updated = str(raw["created_at"]), str(raw["updated_at"])
    return Comment(
        id=int(raw["id"]),
        issue=issue,
        author=_identity(raw["user"]),
        body=str(raw.get("body") or ""),
        created_at=_epoch(created),
        edited_at=_epoch(updated) if updated != created else None,
    )


def _pr(raw: Json) -> PullRequest:
    merged = bool(raw.get("merged") or raw.get("merged_at"))
    state = "merged" if merged else str(raw["state"])
    return PullRequest(
        number=int(raw["number"]),
        head=str(raw["head"]["ref"]),
        base=str(raw["base"]["ref"]),
        title=str(raw["title"]),
        draft=bool(raw.get("draft")),
        state=state,
        merge_sha=str(raw["merge_commit_sha"]) if merged and raw.get("merge_commit_sha") else None,
    )


def _person(login: str) -> Json:
    return {"name": login, "email": f"{login}@users.noreply.github.com"}


def _nodes(data: Json, key: str, connection: str) -> list[Json]:
    node = cast("Json", data.get(key) or {})
    return cast("list[Json]", cast("Json", node.get(connection) or {}).get("nodes") or [])
