"""The urllib transport against a real local HTTP server."""

import http.server
import threading
from collections.abc import Iterator

import pytest

from concert.github.transport import UrllibTransport


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/ok":
            body = b'{"hello": "world"}'
            self.send_response(200)
            self.send_header("X-RateLimit-Remaining", "42")
        elif self.path == "/missing":
            body = b'{"message": "Not Found"}'
            self.send_response(404)
        else:
            body = b""
            self.send_response(204)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        echoed = self.rfile.read(length) + b"|" + (self.headers.get("Authorization") or "").encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(echoed)))
        self.end_headers()
        self.wfile.write(echoed)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_a_successful_response_carries_status_headers_and_body(server: str) -> None:
    response = UrllibTransport().send("GET", f"{server}/ok", {}, None)
    assert response.status == 200 and response.body == b'{"hello": "world"}'
    assert response.headers["X-RateLimit-Remaining"] == "42"


def test_an_error_status_is_a_response_not_an_exception(server: str) -> None:
    response = UrllibTransport().send("GET", f"{server}/missing", {}, None)
    assert response.status == 404 and b"Not Found" in response.body


def test_a_body_and_headers_are_sent(server: str) -> None:
    response = UrllibTransport().send("POST", f"{server}/x", {"Authorization": "Bearer t"}, b"{}")
    assert response.status == 201 and response.body == b"{}|Bearer t"


def test_no_content_has_an_empty_body(server: str) -> None:
    assert UrllibTransport().send("GET", f"{server}/nothing", {}, None).body == b""


def test_an_unreachable_host_raises_a_clear_error() -> None:
    with pytest.raises(ConnectionError, match="could not reach"):
        UrllibTransport(timeout=1).send("GET", "http://127.0.0.1:1/x", {}, None)


def test_only_https_to_github_is_allowed_by_default_for_real_use() -> None:
    with pytest.raises(ValueError, match="https"):
        UrllibTransport(require_https=True).send("GET", "http://example.com/x", {}, None)
