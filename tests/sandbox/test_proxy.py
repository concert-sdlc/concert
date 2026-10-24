"""The CONNECT proxy enforces the egress policy and tunnels only what it allows."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from concert.sandbox.egress import Allowlist
from concert.sandbox.proxy import EgressProxy

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def echo_server() -> tuple[asyncio.Server, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def make_proxy(echo_port: int, resolved: list[str] | None = None) -> EgressProxy:
    seen: list[tuple[str, int]] = []

    async def connector(host: str, port: int):
        seen.append((host, port))
        return await asyncio.open_connection("127.0.0.1", echo_port)

    proxy = EgressProxy(
        Allowlist(["pypi.org"]),
        resolve=lambda _h: resolved or ["93.184.216.34"],
        connect=connector,
    )
    proxy.connected_to = seen  # type: ignore[attr-defined]
    return proxy


async def request(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    return data


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, 5))


def test_an_allowed_connect_is_tunnelled_to_the_checked_address() -> None:
    async def scenario() -> tuple[bytes, list[tuple[str, int]]]:
        server, echo_port = await echo_server()
        proxy = make_proxy(echo_port)
        host, port = await proxy.start()
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
        await writer.drain()
        status = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"hello")
        await writer.drain()
        body = await reader.read(100)
        writer.close()
        await proxy.stop()
        server.close()
        return status + body, proxy.connected_to  # type: ignore[attr-defined]

    data, connected = run(scenario())
    assert data.startswith(b"HTTP/1.1 200") and data.endswith(b"echo:hello")
    assert connected == [("93.184.216.34", 443)]  # the vetted address, not the name again


def test_a_host_off_the_allowlist_gets_403_and_no_connection() -> None:
    async def scenario() -> tuple[bytes, list[tuple[str, int]]]:
        server, echo_port = await echo_server()
        proxy = make_proxy(echo_port)
        _host, port = await proxy.start()
        reply = await request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await proxy.stop()
        server.close()
        return reply, proxy.connected_to  # type: ignore[attr-defined]

    reply, connected = run(scenario())
    assert reply.startswith(b"HTTP/1.1 403") and connected == []


def test_a_private_resolution_is_refused() -> None:
    async def scenario() -> bytes:
        server, echo_port = await echo_server()
        proxy = make_proxy(echo_port, resolved=["169.254.169.254"])
        _host, port = await proxy.start()
        reply = await request(port, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        await proxy.stop()
        server.close()
        return reply

    assert run(scenario()).startswith(b"HTTP/1.1 403")


def test_plain_http_requests_are_refused() -> None:
    async def scenario() -> bytes:
        server, echo_port = await echo_server()
        proxy = make_proxy(echo_port)
        _host, port = await proxy.start()
        reply = await request(port, b"GET http://pypi.org/ HTTP/1.1\r\n\r\n")
        await proxy.stop()
        server.close()
        return reply

    assert run(scenario()).startswith(b"HTTP/1.1 405")


def test_a_malformed_or_oversized_request_is_refused_without_crashing() -> None:
    async def scenario() -> list[bytes]:
        server, echo_port = await echo_server()
        proxy = make_proxy(echo_port)
        _host, port = await proxy.start()
        replies = [
            await request(port, b"CONNECT\r\n\r\n"),
            await request(port, b"CONNECT pypi.org:notaport HTTP/1.1\r\n\r\n"),
            await request(port, b"CONNECT " + b"a" * 100_000 + b":443 HTTP/1.1\r\n\r\n"),
        ]
        await proxy.stop()
        server.close()
        return replies

    replies = run(scenario())
    assert replies[0].startswith(b"HTTP/1.1 400")
    assert replies[1].startswith(b"HTTP/1.1 400")
    assert replies[2].startswith((b"HTTP/1.1 431", b"HTTP/1.1 400", b""))


def test_every_decision_is_logged() -> None:
    async def scenario() -> EgressProxy:
        server, echo_port = await echo_server()
        proxy = make_proxy(echo_port)
        _host, port = await proxy.start()
        await request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await proxy.stop()
        server.close()
        return proxy

    proxy = run(scenario())
    assert [(d.host, d.allowed) for d in proxy.decisions] == [("example.com", False)]  # type: ignore[attr-defined]
