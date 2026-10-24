"""RestGitHub against a scripted API: request shapes in, interface types out."""

import base64
import json
import re
from typing import Any, Protocol

import pytest

from concert.github.api import Conflict, GitHubError, NotFound, StaleHead
from concert.github.http import GitHubHttp, Response
from concert.github.rest import LazyTree, ProjectBoard, RestGitHub


class Handler(Protocol):
    def __call__(self, m: re.Match[str], q: dict[str, str], b: Any, /) -> tuple[int, Any]: ...


class Api:
    """Routes (method, path regex) to handlers; records every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self.requests: list[tuple[str, str, Any]] = []

    def on(self, method: str, pattern: str, handler: Handler | tuple[int, Any]) -> None:
        fn: Handler
        if callable(handler):
            fn = handler
        else:
            fixed = handler

            def constant(_m: re.Match[str], _q: dict[str, str], _b: Any, /) -> tuple[int, Any]:
                return fixed

            fn = constant
        self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        path, _, query = url.removeprefix("https://api.github.com").partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if p)
        payload = json.loads(body) if body else None
        self.requests.append((method, path + (f"?{query}" if query else ""), payload))
        for verb, pattern, handler in self.routes:
            found = pattern.match(path)
            if verb == method and found:
                status, data = handler(found, params, payload)
                return Response(status, {}, json.dumps(data).encode())
        return Response(404, {}, b'{"message": "Not Found"}')


def client(api: Api, board: ProjectBoard | None = None) -> RestGitHub:
    http = GitHubHttp(api, token=lambda: "t", sleep=lambda _s: None, max_attempts=1)
    return RestGitHub(http, "acme", "app", board)


USER = {"id": 7, "login": "alice", "type": "User"}
BOT = {"id": 9, "login": "concert-pm[bot]", "type": "Bot"}
SHA = "a" * 40
SHA2 = "b" * 40


def issue_json(number: int = 3, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": "T",
        "body": "B",
        "user": USER,
        "state": "open",
        "node_id": "I_1",
    } | extra


def comment_json(
    cid: int, body: str = "hi", edited: bool = False, user: dict[str, Any] = USER
) -> dict[str, Any]:
    return {
        "id": cid,
        "body": body,
        "user": user,
        "created_at": "2026-10-24T12:00:00Z",
        "updated_at": "2026-10-24T12:05:00Z" if edited else "2026-10-24T12:00:00Z",
        "issue_url": "https://api.github.com/repos/acme/app/issues/3",
    }


# --- issues and comments -------------------------------------------------------------------------


def test_issues_are_created_read_closed_and_reopened() -> None:
    api = Api()
    api.on("POST", "/repos/acme/app/issues", (201, issue_json()))
    api.on("GET", "/repos/acme/app/issues/3", (200, issue_json()))
    api.on("PATCH", "/repos/acme/app/issues/3", lambda m, q, b: (200, issue_json(state=b["state"])))
    gh = client(api)
    created = gh.create_issue("T", "B")
    assert (created.number, created.title, created.author.login, created.author.is_bot) == (
        3,
        "T",
        "alice",
        False,
    )
    assert api.requests[0][2] == {"title": "T", "body": "B"}
    assert gh.get_issue(3).state == "open"
    assert gh.close_issue(3).state == "closed"
    assert gh.reopen_issue(3).state == "open"


def test_comments_carry_the_author_kind_and_the_edit_time() -> None:
    api = Api()
    api.on(
        "GET",
        "/repos/acme/app/issues/3/comments",
        (200, [comment_json(1), comment_json(2, edited=True, user=BOT)]),
    )
    first, second = client(api).list_comments(3)
    assert first.edited_at is None and not first.author.is_bot
    assert (
        second.edited_at is not None
        and second.edited_at > second.created_at
        and second.author.is_bot
    )
    assert first.issue == 3 and first.created_at == 1792843200


def test_listing_after_an_id_filters_client_side() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/issues/3/comments", (200, [comment_json(1), comment_json(5)]))
    assert [c.id for c in client(api).list_comments(3, after_id=1)] == [5]


def test_comments_are_added_edited_and_deleted() -> None:
    api = Api()
    api.on("POST", "/repos/acme/app/issues/3/comments", (201, comment_json(11, "new")))
    api.on(
        "PATCH",
        "/repos/acme/app/issues/comments/11",
        (200, comment_json(11, "edited", edited=True)),
    )
    api.on("DELETE", "/repos/acme/app/issues/comments/11", (204, None))
    gh = client(api)
    assert gh.add_comment(3, "new").id == 11
    edited = gh.edit_comment(11, "edited")
    assert edited.issue == 3 and edited.edited_at is not None
    gh.delete_comment(11)
    assert api.requests[-1][:2] == ("DELETE", "/repos/acme/app/issues/comments/11")


# --- the Status field -----------------------------------------------------------------------------


def graphql_board(
    api: Api, current: str | None = "triage", has_item: bool = True
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def handler(_m: re.Match[str], _q: dict[str, str], body: Any) -> tuple[int, Any]:
        seen.append(body)
        query: str = body["query"]
        if "projectV2(number" in query:
            return 200, {
                "data": {
                    "organization": {
                        "projectV2": {
                            "id": "P1",
                            "field": {
                                "id": "F1",
                                "options": [
                                    {"id": "O_triage", "name": "triage"},
                                    {"id": "O_spec", "name": "spec"},
                                ],
                            },
                        }
                    }
                }
            }
        if "fieldValueByName" in query:
            node = {
                "id": "IT1",
                "project": {"id": "P1"},
                "fieldValueByName": {"name": current} if current else None,
            }
            return 200, {"data": {"node": {"projectItems": {"nodes": [node] if has_item else []}}}}
        if "projectItems" in query:
            nodes = [{"id": "IT1", "project": {"id": "P1"}}] if has_item else []
            return 200, {"data": {"node": {"projectItems": {"nodes": nodes}}}}
        if "addProjectV2ItemById" in query:
            return 200, {"data": {"addProjectV2ItemById": {"item": {"id": "IT2"}}}}
        return 200, {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}}

    api.on("POST", "/graphql", handler)
    api.on("GET", "/repos/acme/app/issues/3", (200, issue_json()))
    return seen


BOARD = ProjectBoard(owner="acme", number=4)


def test_the_status_is_read_from_the_board_item_of_the_issue() -> None:
    api = Api()
    graphql_board(api)
    assert client(api, BOARD).get_status(3) == "triage"


def test_an_issue_with_no_board_item_has_no_status() -> None:
    api = Api()
    graphql_board(api, current=None, has_item=False)
    assert client(api, BOARD).get_status(3) is None


def test_setting_the_status_updates_the_existing_item() -> None:
    api = Api()
    seen = graphql_board(api)
    client(api, BOARD).set_status(3, "spec")
    update = seen[-1]
    assert update["variables"] == {"p": "P1", "i": "IT1", "f": "F1", "o": "O_spec"}


def test_setting_the_status_adds_the_issue_to_the_board_first_when_needed() -> None:
    api = Api()
    seen = graphql_board(api, has_item=False)
    client(api, BOARD).set_status(3, "spec")
    assert any("addProjectV2ItemById" in s["query"] for s in seen)
    assert seen[-1]["variables"]["i"] == "IT2"


def test_a_status_the_board_does_not_offer_is_an_error() -> None:
    api = Api()
    graphql_board(api)
    with pytest.raises(GitHubError, match="no 'human_uat' option"):
        client(api, BOARD).set_status(3, "human_uat")


def test_the_board_is_looked_up_once() -> None:
    api = Api()
    seen = graphql_board(api)
    gh = client(api, BOARD)
    gh.get_status(3)
    gh.get_status(3)
    assert sum("projectV2(number" in s["query"] for s in seen) == 1


def test_without_a_board_status_calls_fail_clearly() -> None:
    with pytest.raises(GitHubError, match="no Projects v2 board"):
        client(Api()).get_status(3)


# --- git ------------------------------------------------------------------------------------------


def test_refs_are_read_created_moved_and_deleted() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/git/ref/heads/staging", (200, {"object": {"sha": SHA}}))
    api.on("POST", "/repos/acme/app/git/refs", (201, {}))
    api.on("PATCH", "/repos/acme/app/git/refs/heads/pm/3", (200, {}))
    api.on("DELETE", "/repos/acme/app/git/refs/heads/pm/3", (204, None))
    gh = client(api)
    assert gh.get_ref("staging") == SHA and gh.get_ref("nope") is None
    assert gh.create_branch("pm/3", "staging") == SHA
    assert api.requests[-1][2] == {"ref": "refs/heads/pm/3", "sha": SHA}
    assert gh.create_branch("pm/4", SHA2) == SHA2  # a sha is used as is
    gh.update_ref("pm/3", SHA2, force=True)
    assert api.requests[-1][2] == {"sha": SHA2, "force": True}
    gh.delete_ref("pm/3")


def test_a_prefix_match_is_not_a_branch() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/git/ref/heads/pm", (200, [{"object": {"sha": SHA}}]))
    assert client(api).get_ref("pm") is None


def test_a_commit_builds_a_tree_a_commit_and_moves_the_branch() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/git/ref/heads/pm/3", (200, {"object": {"sha": SHA}}))
    api.on(
        "GET", f"/repos/acme/app/git/commits/{SHA}", (200, {"tree": {"sha": "T0"}, "parents": []})
    )
    api.on("POST", "/repos/acme/app/git/trees", (201, {"sha": "T1"}))
    api.on("POST", "/repos/acme/app/git/commits", (201, {"sha": SHA2}))
    api.on("PATCH", "/repos/acme/app/git/refs/heads/pm/3", (200, {}))
    sha = client(api).commit("pm/3", "msg", {"a.py": "x", "old.py": None}, "vm-1", "vm-1")
    assert sha == SHA2
    tree = next(b for _m, p, b in api.requests if p.endswith("/git/trees"))
    assert tree["base_tree"] == "T0"
    assert {"path": "a.py", "mode": "100644", "type": "blob", "content": "x"} in tree["tree"]
    assert {"path": "old.py", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]
    created = next(b for m, p, b in api.requests if p.endswith("/git/commits") and m == "POST")
    assert (
        created["parents"] == [SHA]
        and created["tree"] == "T1"
        and created["author"]["name"] == "vm-1"
    )
    assert api.requests[-1][2] == {"sha": SHA2, "force": False}


def test_listing_commits_uses_the_compare_api() -> None:
    api = Api()
    commit = {
        "sha": SHA2,
        "parents": [{"sha": SHA}],
        "commit": {"author": {"name": "vm-1"}, "committer": {"name": "web-flow"}},
    }
    api.on("GET", "/repos/acme/app/compare/staging\\.\\.\\.pm/3", (200, {"commits": [commit]}))
    [meta] = client(api).list_commits("staging", "pm/3")
    assert (meta.sha, meta.parent, meta.author, meta.committer) == (SHA2, SHA, "vm-1", "web-flow")


def test_a_tree_lists_paths_without_loading_contents() -> None:
    api = Api()
    api.on(
        "GET",
        f"/repos/acme/app/git/trees/{SHA}",
        (
            200,
            {
                "truncated": False,
                "tree": [
                    {"path": "a.py", "type": "blob", "sha": "B1"},
                    {"path": "dir", "type": "tree", "sha": "T"},
                    {"path": "dir/b.py", "type": "blob", "sha": "B2"},
                ],
            },
        ),
    )
    api.on(
        "GET",
        "/repos/acme/app/git/blobs/B2",
        (200, {"content": base64.b64encode("héllo".encode()).decode(), "encoding": "base64"}),
    )
    tree = client(api).read_tree(SHA)
    assert isinstance(tree, LazyTree) and sorted(tree) == ["a.py", "dir/b.py"]
    assert all("blobs" not in p for _m, p, _b in api.requests)
    assert tree["dir/b.py"] == "héllo" and tree["dir/b.py"] == "héllo"
    assert sum("blobs" in p for _m, p, _b in api.requests) == 1  # cached
    with pytest.raises(KeyError):
        tree["dir"]


def test_a_truncated_tree_is_refused() -> None:
    api = Api()
    api.on("GET", f"/repos/acme/app/git/trees/{SHA}", (200, {"truncated": True, "tree": []}))
    with pytest.raises(GitHubError, match="too large"):
        client(api).read_tree(SHA)


def test_files_are_read_at_a_ref_and_missing_ones_are_none() -> None:
    api = Api()
    api.on(
        "GET",
        "/repos/acme/app/contents/docs/a\\.md",
        (200, {"encoding": "base64", "content": base64.b64encode(b"# a").decode()}),
    )
    api.on("GET", "/repos/acme/app/contents/docs", (200, [{"name": "a.md"}]))
    gh = client(api)
    assert gh.read_file("staging", "docs/a.md") == "# a"
    assert api.requests[0][1].endswith("?ref=staging")
    assert gh.read_file("staging", "missing") is None
    assert gh.read_file("staging", "docs") is None  # a directory


def test_parents_of_a_commit() -> None:
    api = Api()
    api.on(
        "GET",
        f"/repos/acme/app/git/commits/{SHA}",
        (200, {"tree": {"sha": "T"}, "parents": [{"sha": SHA2}]}),
    )
    assert client(api).commit_parents(SHA) == (SHA2,)


# --- pull requests ----------------------------------------------------------------------------------


def pr_json(number: int = 5, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": "t",
        "state": "open",
        "draft": True,
        "node_id": "PR_1",
        "head": {"ref": "pm/3"},
        "base": {"ref": "staging"},
    } | extra


def test_a_draft_pr_is_opened_found_and_read() -> None:
    api = Api()
    api.on("POST", "/repos/acme/app/pulls", (201, pr_json()))
    api.on(
        "GET",
        "/repos/acme/app/pulls/5",
        (200, pr_json(state="closed", merged=True, merge_commit_sha=SHA2, draft=False)),
    )
    api.on(
        "GET",
        "/repos/acme/app/pulls",
        lambda m, q, b: (200, [pr_json()] if q["head"] == "acme%3Apm%2F3" else []),
    )
    gh = client(api)
    pr = gh.create_pr("pm/3", "staging", "t")
    assert (pr.number, pr.head, pr.base, pr.draft, pr.state, pr.merge_sha) == (
        5,
        "pm/3",
        "staging",
        True,
        "open",
        None,
    )
    assert api.requests[0][2] == {"title": "t", "head": "pm/3", "base": "staging", "draft": True}
    merged = gh.get_pr(5)
    assert merged.state == "merged" and merged.merge_sha == SHA2 and not merged.draft
    found = gh.find_pr("pm/3")
    assert found is not None and found.number == 5
    assert gh.find_pr("pm/9") is None


def test_marking_a_pr_ready_uses_graphql() -> None:
    api = Api()
    seen: list[Any] = []
    api.on("GET", "/repos/acme/app/pulls/5", (200, pr_json(draft=False)))
    api.on(
        "POST",
        "/graphql",
        lambda m, q, b: (
            seen.append(b) or 200,
            {"data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "PR_1"}}}},
        ),
    )  # type: ignore[arg-type,return-value]
    assert client(api).mark_ready(5).draft is False
    assert (
        seen[0]["variables"] == {"p": "PR_1"}
        and "markPullRequestReadyForReview" in seen[0]["query"]
    )


def test_merging_sends_the_expected_head_and_returns_the_merge_commit() -> None:
    api = Api()
    api.on("PUT", "/repos/acme/app/pulls/5/merge", (200, {"sha": SHA2, "merged": True}))
    assert client(api).merge_pr(5, SHA) == SHA2
    assert api.requests[0][2] == {"sha": SHA, "merge_method": "merge"}


def test_a_moved_head_is_a_stale_head_and_other_refusals_are_conflicts() -> None:
    api = Api()
    api.on(
        "PUT",
        "/repos/acme/app/pulls/5/merge",
        (409, {"message": "Head branch was modified. Review and try the merge again."}),
    )
    with pytest.raises(StaleHead):
        client(api).merge_pr(5, SHA)
    other = Api()
    other.on(
        "PUT", "/repos/acme/app/pulls/5/merge", (405, {"message": "Pull Request is not mergeable"})
    )
    with pytest.raises(Conflict):
        client(other).merge_pr(5, SHA)
    broken = Api()
    broken.on("PUT", "/repos/acme/app/pulls/5/merge", (500, {"message": "boom"}))
    with pytest.raises(GitHubError) as failed:
        client(broken).merge_pr(5, SHA)
    assert not isinstance(failed.value, Conflict)
    forbidden = Api()
    forbidden.on("PUT", "/repos/acme/app/pulls/5/merge", (409, {"message": "something else"}))
    with pytest.raises(Conflict) as caught:
        client(forbidden).merge_pr(5, SHA)
    assert not isinstance(caught.value, StaleHead)


def test_reverting_a_merged_pr_goes_through_graphql_and_returns_the_new_pr() -> None:
    api = Api()
    api.on(
        "GET",
        "/repos/acme/app/pulls/5",
        (200, pr_json(merged=True, state="closed", merge_commit_sha=SHA2)),
    )
    api.on(
        "GET",
        "/repos/acme/app/pulls/6",
        (200, pr_json(6, draft=False, head={"ref": "revert-pr-5"})),
    )
    seen: list[Any] = []

    def graphql(_m: re.Match[str], _q: dict[str, str], body: Any) -> tuple[int, Any]:
        seen.append(body)
        return 200, {"data": {"revertPullRequest": {"revertPullRequest": {"number": 6}}}}

    api.on("POST", "/graphql", graphql)
    revert = client(api).revert_pr(5)
    assert revert.number == 6 and revert.head == "revert-pr-5" and not revert.draft
    assert seen[0]["variables"]["p"] == "PR_1"


def test_only_a_merged_pr_can_be_reverted() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/pulls/5", (200, pr_json()))
    with pytest.raises(Conflict):
        client(api).revert_pr(5)


def test_reviews_carry_the_reviewer_the_state_and_the_commit() -> None:
    api = Api()
    api.on(
        "GET",
        "/repos/acme/app/pulls/5/reviews",
        (
            200,
            [
                {"user": USER, "state": "APPROVED", "commit_id": SHA},
                {"user": None, "state": "COMMENTED", "commit_id": SHA},  # a deleted account
            ],
        ),
    )
    [review] = client(api).list_pr_reviews(5)
    assert (review.user_id, review.state, review.commit_id) == (7, "APPROVED", SHA)


# --- checks ------------------------------------------------------------------------------------------


def test_check_runs_are_tied_to_the_hash_of_the_workflow_file_that_ran() -> None:
    api = Api()
    api.on(
        "GET",
        f"/repos/acme/app/commits/{SHA}/check-runs",
        (
            200,
            {
                "check_runs": [
                    {
                        "name": "tests",
                        "head_sha": SHA,
                        "conclusion": "success",
                        "check_suite": {"id": 11},
                    },
                    {
                        "name": "third-party",
                        "head_sha": SHA,
                        "conclusion": None,
                        "check_suite": {"id": 99},
                    },
                ]
            },
        ),
    )
    api.on(
        "GET",
        "/repos/acme/app/actions/runs",
        (
            200,
            {
                "workflow_runs": [
                    {
                        "check_suite_id": 11,
                        "path": ".github/workflows/concert-ci.yml@refs/pull/5/merge",
                    },
                ]
            },
        ),
    )
    api.on(
        "GET",
        "/repos/acme/app/contents/\\.github/workflows/concert-ci\\.yml",
        (200, {"encoding": "base64", "content": base64.b64encode(b"on: pull_request").decode()}),
    )
    runs = client(api).list_check_runs(SHA)
    import hashlib

    assert [(r.name, r.conclusion) for r in runs] == [("tests", "success"), ("third-party", None)]
    assert runs[0].workflow_hash == hashlib.sha256(b"on: pull_request").hexdigest()
    assert runs[1].workflow_hash == ""  # not produced by a workflow in this repository


def test_a_workflow_file_is_hashed_once_per_commit() -> None:
    api = Api()
    api.on(
        "GET",
        f"/repos/acme/app/commits/{SHA}/check-runs",
        (
            200,
            {
                "check_runs": [
                    {"name": n, "head_sha": SHA, "conclusion": "success", "check_suite": {"id": 11}}
                    for n in ("a", "b", "c")
                ]
            },
        ),
    )
    api.on(
        "GET",
        "/repos/acme/app/actions/runs",
        (200, {"workflow_runs": [{"check_suite_id": 11, "path": ".github/workflows/ci.yml"}]}),
    )
    api.on(
        "GET",
        "/repos/acme/app/contents/\\.github/workflows/ci\\.yml",
        (200, {"encoding": "base64", "content": base64.b64encode(b"x").decode()}),
    )
    client(api).list_check_runs(SHA)
    assert sum("/contents/" in p for _m, p, _b in api.requests) == 1


def test_missing_things_raise_not_found() -> None:
    with pytest.raises(NotFound):
        client(Api()).get_issue(1)
