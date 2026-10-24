"""The HTTP layer under the REST client: errors, retries, pagination, GraphQL."""

import json
from collections.abc import Callable

import pytest

from concert.github.api import Conflict, Forbidden, GitHubError, NotFound
from concert.github.http import GitHubHttp, RateLimited, Response


class Script:
    """A transport that answers from a list and records what it was asked."""

    def __init__(self, *responses: Response | Callable[[str, str], Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        self.calls.append((method, url, headers, body))
        item = self.responses.pop(0)
        return item(method, url) if callable(item) else item


def ok(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, headers or {}, json.dumps(payload).encode())


def http(transport: Script, sleeps: list[float] | None = None, attempts: int = 4) -> GitHubHttp:
    record = sleeps if sleeps is not None else []
    return GitHubHttp(
        transport,
        token=lambda: "tkn",
        sleep=record.append,
        max_attempts=attempts,
        clock=lambda: 1000,
    )


def test_requests_carry_the_token_and_the_api_version() -> None:
    transport = Script(ok({"id": 1}))
    assert http(transport).json("GET", "/repos/o/r/issues/1") == {"id": 1}
    method, url, headers, body = transport.calls[0]
    assert (method, url, body) == ("GET", "https://api.github.com/repos/o/r/issues/1", None)
    assert headers["Authorization"] == "Bearer tkn"
    assert headers["Accept"] == "application/vnd.github+json" and "X-GitHub-Api-Version" in headers


def test_a_body_is_sent_as_json_and_params_are_encoded() -> None:
    transport = Script(ok({"ok": True}))
    http(transport).json("POST", "/x", body={"title": "é"}, params={"per_page": 100, "q": "a b"})
    _m, url, headers, body = transport.calls[0]
    assert url == "https://api.github.com/x?per_page=100&q=a+b"
    assert headers["Content-Type"] == "application/json" and json.loads(body or b"") == {
        "title": "é"
    }


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, NotFound),
        (403, Forbidden),
        (401, Forbidden),
        (409, Conflict),
        (422, Conflict),
        (500, GitHubError),
    ],
)
def test_error_statuses_map_to_the_interface_errors(status: int, error: type[Exception]) -> None:
    transport = Script(ok({"message": "nope"}, status))
    with pytest.raises(error, match="nope"):
        http(transport, attempts=1).json("GET", "/x")


def test_a_204_has_no_body() -> None:
    assert http(Script(Response(204, {}, b""))).json("DELETE", "/x") is None


def test_server_errors_are_retried_with_backoff_then_succeed() -> None:
    sleeps: list[float] = []
    transport = Script(ok({}, 502), ok({}, 503), ok({"fine": 1}))
    assert http(transport, sleeps).json("GET", "/x") == {"fine": 1}
    assert sleeps == [1, 2]


def test_server_errors_give_up_after_the_attempt_limit() -> None:
    transport = Script(ok({"message": "down"}, 502), ok({"message": "down"}, 502))
    with pytest.raises(GitHubError, match="down"):
        http(transport, attempts=2).json("GET", "/x")
    assert len(transport.calls) == 2


def test_a_rate_limit_waits_for_retry_after() -> None:
    sleeps: list[float] = []
    transport = Script(ok({"message": "slow down"}, 429, {"Retry-After": "7"}), ok({"x": 1}))
    assert http(transport, sleeps).json("GET", "/x") == {"x": 1}
    assert sleeps == [7]


def test_a_primary_rate_limit_waits_until_the_reset_time() -> None:
    sleeps: list[float] = []
    limited = ok(
        {"message": "API rate limit exceeded"},
        403,
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1012"},
    )
    assert http(Script(limited, ok({})), sleeps).json("GET", "/x") == {}
    assert sleeps == [12]


def test_a_wait_longer_than_the_cap_is_reported_not_slept() -> None:
    limited = ok({"message": "rate limit"}, 429, {"Retry-After": "3600"})
    with pytest.raises(RateLimited) as caught:
        http(Script(limited)).json("GET", "/x")
    assert caught.value.retry_after == 3600


def test_a_plain_403_is_forbidden_not_a_rate_limit() -> None:
    with pytest.raises(Forbidden):
        http(Script(ok({"message": "Resource not accessible by integration"}, 403))).json(
            "GET", "/x"
        )


def test_pagination_follows_the_next_link() -> None:
    first = ok(
        [1, 2],
        headers={
            "Link": '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=3>; rel="last"'
        },
    )
    second = ok([3], headers={"Link": '<https://api.github.com/x?page=1>; rel="prev"'})
    transport = Script(first, second)
    assert http(transport).paged("/x", {"per_page": 2}) == [1, 2, 3]
    assert transport.calls[1][1] == "https://api.github.com/x?page=2"


def test_pagination_stops_at_the_page_limit() -> None:
    loop = ok([1], headers={"Link": '<https://api.github.com/x?page=2>; rel="next"'})
    transport = Script(*([loop] * 5))
    assert http(transport).paged("/x", max_pages=3) == [1, 1, 1]


def test_graphql_returns_data_and_raises_on_errors() -> None:
    transport = Script(ok({"data": {"viewer": {"login": "bot"}}}))
    assert http(transport).graphql("query { viewer { login } }") == {"viewer": {"login": "bot"}}
    method, url, _h, body = transport.calls[0]
    assert (method, url) == ("POST", "https://api.github.com/graphql")
    assert json.loads(body or b"")["query"].startswith("query")
    failing = Script(ok({"errors": [{"message": "Could not resolve"}]}))
    with pytest.raises(GitHubError, match="Could not resolve"):
        http(failing).graphql("query { x }")


def test_graphql_not_found_errors_map_to_not_found() -> None:
    failing = Script(ok({"errors": [{"type": "NOT_FOUND", "message": "no such thing"}]}))
    with pytest.raises(NotFound):
        http(failing).graphql("query { x }")
