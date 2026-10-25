"""Fixes from the cubic whole-repo review (2026-10-25): the GitHub layer."""

import base64
import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from concert.github.api import BinaryBlob, GitHubError
from concert.github.git_facts import GitFacts
from concert.github.http import GitHubHttp, Response
from concert.github.rest import RestGitHub
from concert.github.transport import UrllibTransport
from tests.github.test_http import Script, ok
from tests.github.test_rest import SHA, Api, client, pr_json


def http(transport: Script, attempts: int = 4) -> GitHubHttp:
    return GitHubHttp(
        transport,
        token=lambda: "t",
        sleep=lambda _s: None,
        max_attempts=attempts,
        clock=lambda: 1000,
    )


# --- writes are not retried on a server error ------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_write_that_got_a_server_error_is_not_sent_again(method: str) -> None:
    """The first attempt may have been applied (a merge that returned 502, a duplicate comment)."""
    script = Script(Response(502, {}, b""), ok({"fine": True}))
    with pytest.raises(GitHubError, match="502"):
        http(script).json(method, "/x", {"a": 1})
    assert len(script.calls) == 1


def test_a_read_that_got_a_server_error_is_still_retried() -> None:
    script = Script(Response(502, {}, b""), ok({"fine": True}))
    assert http(script).json("GET", "/x") == {"fine": True}
    assert len(script.calls) == 2


def test_paging_that_would_be_cut_short_is_an_error_not_a_partial_list() -> None:
    link = {"Link": '<https://api.github.com/x?page=2>; rel="next"'}
    script = Script(*[ok([1], headers=link) for _ in range(3)])
    with pytest.raises(GitHubError, match="more than 2 pages"):
        http(script).paged("/x", max_pages=2)


# --- redirects are not followed with the token ----------------------------------------------


def test_a_redirect_is_returned_not_followed() -> None:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(f"{self.path} auth={self.headers.get('Authorization')}")
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/start"
        reply = UrllibTransport().send("GET", url, {"Authorization": "Bearer secret"}, None)
    finally:
        server.shutdown()
    assert reply.status == 302
    assert seen == ["/start auth=Bearer secret"]  # /elsewhere was never requested


# --- the REST client ------------------------------------------------------------------------


def test_the_board_field_name_is_a_variable_not_part_of_the_query() -> None:
    from concert.github.rest import ProjectBoard

    api = Api()
    queries: list[Any] = []

    def handler(_m: Any, _q: Any, body: Any) -> tuple[int, Any]:
        queries.append(body)
        if "projectV2(number" in body["query"]:
            return 200, {
                "data": {
                    "organization": {
                        "projectV2": {
                            "id": "P",
                            "field": {"id": "F", "options": [{"id": "o", "name": "triage"}]},
                        }
                    }
                }
            }
        return 200, {"data": {"node": {"projectItems": {"nodes": []}}}}

    api.on("POST", "/graphql", handler)
    api.on("GET", "/repos/acme/app/issues/3", (200, {"node_id": "I_1"}))
    board = ProjectBoard(owner="acme", number=4, field_name='Sta"tus')
    client(api, board).get_status(3)
    assert all('Sta"tus' not in q["query"] for q in queries)
    assert any(q["variables"].get("f") == 'Sta"tus' for q in queries)


def test_a_file_too_big_for_the_contents_api_is_read_through_its_blob() -> None:
    """Live contents API: over 1 MB it answers encoding "none" and no content. The event mirror
    must never read that as an empty file and overwrite it."""
    api = Api()
    api.on(
        "GET",
        "/repos/acme/app/contents/events/1.jsonl",
        (200, {"encoding": "none", "content": "", "sha": SHA}),
    )
    body = base64.b64encode(b"line\n" * 3).decode()
    api.on(
        "GET", f"/repos/acme/app/git/blobs/{SHA}", (200, {"content": body, "encoding": "base64"})
    )
    assert client(api).read_file("concert-events", "events/1.jsonl") == "line\n" * 3


def test_a_binary_file_in_a_tree_is_a_blob_reference_not_a_crash() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/git/ref/heads/main", (200, {"object": {"sha": SHA}}))
    api.on(
        "GET",
        f"/repos/acme/app/git/trees/{SHA}",
        (
            200,
            {
                "truncated": False,
                "tree": [
                    {"path": "logo.png", "type": "blob", "sha": "b1"},
                    {"path": "README", "type": "blob", "sha": "b2"},
                ],
            },
        ),
    )
    api.on(
        "GET",
        "/repos/acme/app/git/blobs/b1",
        (200, {"content": base64.b64encode(b"\xff\xfe\x00").decode(), "encoding": "base64"}),
    )
    api.on(
        "GET",
        "/repos/acme/app/git/blobs/b2",
        (200, {"content": base64.b64encode(b"hello").decode(), "encoding": "base64"}),
    )
    tree = dict(client(api).read_tree("main"))
    assert tree["README"] == "hello"
    assert isinstance(tree["logo.png"], BinaryBlob) and tree["logo.png"].sha == "b1"


def test_committing_a_blob_reference_reuses_the_blob_and_keeps_executable_modes() -> None:
    api = Api()
    api.on("GET", "/repos/acme/app/git/ref/heads/wip", (200, {"object": {"sha": SHA}}))
    api.on("GET", f"/repos/acme/app/git/commits/{SHA}", (200, {"tree": {"sha": "T0"}}))
    api.on(
        "GET",
        "/repos/acme/app/git/trees/T0",
        (
            200,
            {
                "truncated": False,
                "tree": [
                    {"path": "run.sh", "type": "blob", "mode": "100755", "sha": "s0"},
                    {"path": "a.txt", "type": "blob", "mode": "100644", "sha": "s1"},
                ],
            },
        ),
    )
    posted: dict[str, Any] = {}

    def tree(_m: Any, _q: Any, body: Any) -> tuple[int, Any]:
        posted["tree"] = body
        return 201, {"sha": "T1"}

    api.on("POST", "/repos/acme/app/git/trees", tree)
    api.on("POST", "/repos/acme/app/git/commits", (201, {"sha": "C1"}))
    api.on("PATCH", "/repos/acme/app/git/refs/heads/wip", (200, {}))
    client(api).commit(
        "wip",
        "m",
        {"run.sh": "#!/bin/sh\necho 2\n", "new.txt": "n", "logo.png": BinaryBlob("b1")},
        "a",
        "a",
    )
    entries = {e["path"]: e for e in posted["tree"]["tree"]}
    assert entries["run.sh"]["mode"] == "100755"  # an edit keeps the executable bit
    assert entries["new.txt"]["mode"] == "100644"
    assert entries["logo.png"]["sha"] == "b1" and "content" not in entries["logo.png"]


def test_a_revert_pr_is_found_by_the_name_github_gives_it() -> None:
    """Live: reverting PR 37 (branch live-1/pr) created the branch revert-37-live-1/pr."""
    api = Api()
    api.on(
        "GET",
        "/repos/acme/app/pulls",
        (
            200,
            [
                pr_json(8, head={"ref": "revert-38-other", "sha": SHA}),
                pr_json(9, head={"ref": "revert-37-live-1/pr", "sha": SHA}),
            ],
        ),
    )
    found = client(api).find_revert(37)
    assert found is not None and found.number == 9
    assert client(api).find_revert(36) is None


class Guarded(Mapping[str, str]):
    """A tree that fails if anything outside the spec directory is read."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths

    def __getitem__(self, path: str) -> str:
        assert path.startswith("docs/specs/3/"), f"read {path}"
        return "x"

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.paths)

    def __len__(self) -> int:
        return len(self.paths)


def test_handoff_facts_do_not_load_the_whole_tree() -> None:
    from concert.core.derive import IssueState
    from concert.core.handoff import Handoff

    class Github:
        def list_commits(self, _base: str, _ref: str) -> list[Any]:
            return [type("C", (), {"sha": SHA})()]

        def read_tree(self, _sha: str) -> Mapping[str, str]:
            return Guarded(["logo.png", "docs/specs/3/spec.md"])

    handoff = Handoff.__new__(Handoff)
    object.__setattr__(handoff, "commit_sha", SHA)
    object.__setattr__(handoff, "runner", "r")
    object.__setattr__(handoff, "run_id", "r-1")
    state = IssueState.__new__(IssueState)
    for name, value in (("branch", "pm/3"), ("approved_spec_rev", None)):
        object.__setattr__(state, name, value)
    facts = GitFacts(Github(), "staging").facts(3, handoff, state, None)  # type: ignore[arg-type]
    assert facts.paths_at_commit == {"logo.png", "docs/specs/3/spec.md"}


def test_pr_json_head_has_a_sha() -> None:  # keeps the helper honest for the tests above
    assert pr_json()["head"]["sha"] == SHA
    assert isinstance(json.dumps(pr_json()), str)
    assert RestGitHub is not None
