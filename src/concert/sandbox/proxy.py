"""A small HTTPS CONNECT proxy that enforces the egress policy for sandboxed agents.

The sandbox has no route to the internet except through this proxy. Each CONNECT is checked
against the allowlist; an allowed tunnel is opened to the address that was just vetted (never
resolving the name a second time, which would let DNS rebinding swap in a private address), then
bytes are piped both ways. Anything but CONNECT is refused, and every decision is recorded.
"""

import asyncio
import socket
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from concert.sandbox.egress import Allowlist, check_connect

Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

_MAX_HEAD = 8_192
_IDLE_SECONDS = 300
_MAX_LOG = 1_000  # recent decisions kept for inspection; the proxy runs for weeks


@dataclass(frozen=True)
class Logged:
    host: str
    port: int
    allowed: bool
    reason: str


def system_resolve(host: str) -> list[str]:
    return sorted(
        {str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    )


@dataclass
class EgressProxy:
    allowlist: Allowlist
    resolve: Callable[[str], list[str]] = system_resolve
    connect: Connect = asyncio.open_connection
    decisions: deque[Logged] = field(default_factory=lambda: deque[Logged](maxlen=_MAX_LOG))
    _server: asyncio.Server | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, host, port, limit=_MAX_HEAD)
        bound = self._server.sockets[0].getsockname()
        return str(bound[0]), int(bound[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except asyncio.LimitOverrunError:
            await _reply(writer, 431, "Request Header Fields Too Large")
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) != 3:
            await _reply(writer, 400, "Bad Request")
            return
        method, target, _version = parts
        if method != "CONNECT":
            await _reply(writer, 405, "Method Not Allowed")
            return
        host, _, port_text = target.rpartition(":")
        if not host or not port_text.isdigit():
            await _reply(writer, 400, "Bad Request")
            return
        port = int(port_text)
        addresses: list[str] = []

        def resolver(name: str) -> list[str]:
            addresses.extend(self.resolve(name))
            return addresses

        # name lookups block, and a sandboxed agent can ask for slow ones: keep them off the loop
        decision = await asyncio.get_running_loop().run_in_executor(
            None, check_connect, host, port, self.allowlist, resolver
        )
        self.decisions.append(Logged(host, port, decision.allowed, decision.reason))
        if not decision.allowed:
            await _reply(writer, 403, "Forbidden")
            return
        upstream_reader, upstream_writer = await self.connect(addresses[0], port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            _pipe(reader, upstream_writer), _pipe(upstream_reader, writer), return_exceptions=True
        )


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while data := await asyncio.wait_for(source.read(65_536), _IDLE_SECONDS):
            sink.write(data)
            await sink.drain()
    finally:
        sink.close()


async def _reply(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
    writer.write(f"HTTP/1.1 {code} {reason}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
