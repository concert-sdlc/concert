"""The HTTP layer under the REST client.

One place that knows about the wire: authentication, the API version header, retries for
server errors, waiting out rate limits (and refusing to sleep for an hour), pagination through
`Link` headers, and the mapping of statuses onto the interface's error types. The transport
itself is injected, so everything above it is testable without a network.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlencode

from concert.github.api import Conflict, Forbidden, GitHubError, NotFound

API = "https://api.github.com"
_MAX_WAIT = 120  # a longer rate-limit wait is reported to the caller instead of slept
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response: ...


class RateLimited(GitHubError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited for {retry_after:.0f}s")
        self.retry_after = retry_after


class GitHubHttp:
    def __init__(
        self,
        transport: Transport,
        token: Callable[[], str],
        api: str = API,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._token = token
        self._api = api
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._clock = clock

    def json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._parse(self._send(method, self._url(path, params), body))

    def paged(
        self, path: str, params: Mapping[str, Any] | None = None, max_pages: int = 50
    ) -> list[Any]:
        url: str | None = self._url(path, params)
        items: list[Any] = []
        for _ in range(max_pages):
            if url is None:
                return items
            response = self._send("GET", url, None)
            page: Any = self._parse(response)
            items.extend(cast("list[Any]", page) if isinstance(page, list) else [page])
            found = _NEXT.search(response.headers.get("Link", ""))
            url = found.group(1) if found else None
        if url is not None:  # a partial list would be read as the whole truth
            raise GitHubError(f"{path} has more than {max_pages} pages")
        return items

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload: Any = self.json("POST", "/graphql", {"query": query, "variables": variables or {}})
        errors = cast("list[dict[str, Any]]", payload.get("errors") or [])
        if errors:
            message = "; ".join(str(e.get("message")) for e in errors)
            if any(e.get("type") == "NOT_FOUND" for e in errors):
                raise NotFound(message)
            raise GitHubError(message)
        return cast("dict[str, Any]", payload.get("data") or {})

    # --- internals -----------------------------------------------------------------------------

    def _url(self, path: str, params: Mapping[str, Any] | None) -> str:
        query = f"?{urlencode(params)}" if params else ""
        return f"{self._api}{path}{query}"

    def _send(self, method: str, url: str, body: Mapping[str, Any] | None) -> Response:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(1, self._max_attempts + 1):
            response = self._transport.send(method, url, headers, data)
            wait = self._wait_for(response, method)
            last = attempt == self._max_attempts
            if wait is None or last:
                return response
            self._sleep(wait if wait > 0 else float(2 ** (attempt - 1)))
        raise AssertionError("unreachable")  # pragma: no cover

    def _wait_for(self, response: Response, method: str) -> float | None:
        """Seconds to wait before retrying this response, 0 for plain backoff, None: don't retry.

        A server error is retried only for reads: a write that got a 502 may have been applied
        (a merge, a comment), and sending it again would duplicate or misreport it. The caller's
        intent/outcome record reconciles a failed write on the next poll instead."""
        status = response.status
        if status in (500, 502, 503, 504):
            return 0 if method in _SAFE_METHODS else None
        limited = status == 429 or (status == 403 and _is_rate_limit(response))
        if not limited:
            return None
        seconds = _retry_seconds(response, self._clock())
        if seconds > _MAX_WAIT:
            raise RateLimited(seconds)
        return seconds

    def _parse(self, response: Response) -> Any:
        status = response.status
        if status == 204 or not response.body:
            payload: Any = None
        else:
            try:
                payload = json.loads(response.body)
            except json.JSONDecodeError:
                payload = None
        if status < 300:
            return payload
        message = _message(payload) or f"HTTP {status}"
        if status == 404:
            raise NotFound(message)
        if status in (401, 403):
            raise Forbidden(message)
        if status in (409, 422):
            raise Conflict(message)
        raise GitHubError(f"{status}: {message}")


def _message(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(cast("dict[str, Any]", payload).get("message", ""))
    return ""


def _is_rate_limit(response: Response) -> bool:
    headers = response.headers
    if headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in headers:
        return True
    return "rate limit" in response.body.decode("utf-8", "replace").lower()


def _retry_seconds(response: Response, now: float) -> float:
    headers = response.headers
    if "Retry-After" in headers:
        return float(headers["Retry-After"])
    if headers.get("X-RateLimit-Remaining") == "0" and "X-RateLimit-Reset" in headers:
        return max(0.0, float(headers["X-RateLimit-Reset"]) - now)
    return 60.0  # a secondary limit without a hint: wait a minute
