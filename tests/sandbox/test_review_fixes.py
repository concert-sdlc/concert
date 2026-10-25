"""Fixes from the cubic whole-repo review (2026-10-25): sandbox side."""

import asyncio
import os
import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from concert.sandbox.egress import Allowlist
from concert.sandbox.executor import DockerExecutor, DockerSpec
from concert.sandbox.proxy import EgressProxy
from concert.sandbox.workspace import UnsafeWorkspace, snapshot


@contextmanager
def within(seconds: int) -> Generator[None]:
    """Fail instead of hanging forever (a FIFO read blocks without a writer)."""

    def alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError("blocked")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_fifo_in_the_workspace_is_refused_not_read(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("fine")
    os.mkfifo(tmp_path / "pipe")
    with within(5), pytest.raises(UnsafeWorkspace, match="not a regular file: pipe"):
        snapshot(tmp_path)


def test_a_fifo_in_a_subdirectory_is_refused_too(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    os.mkfifo(tmp_path / "d" / "pipe")
    with within(5), pytest.raises(UnsafeWorkspace, match="not a regular file: d/pipe"):
        snapshot(tmp_path)


def test_the_docker_cli_gets_the_variables_it_needs_and_only_those(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def run(
        _args: list[str], env: dict[str, str], _timeout: float
    ) -> "subprocess.CompletedProcess[str]":
        seen.update(env)
        return subprocess.CompletedProcess([], 0, "", "")

    spec = DockerSpec(
        image="i",
        network="n",
        proxy_url="http://p",
        runtime="runsc",
        passthrough_env=("ANTHROPIC_API_KEY",),
    )
    environ = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/Users/concert",
        "DOCKER_HOST": "unix:///x.sock",
        "ANTHROPIC_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghs_nope",
        "CONCERT_PM_KEY": "/k.pem",
    }
    DockerExecutor(spec, run=run, environ=environ).execute(["true"], tmp_path, "n", 5)
    assert seen["PATH"] == "/usr/local/bin:/usr/bin"
    assert seen["HOME"] == "/Users/concert"
    assert seen["DOCKER_HOST"] == "unix:///x.sock"
    assert seen["ANTHROPIC_API_KEY"] == "sk-secret"
    assert "GITHUB_TOKEN" not in seen and "CONCERT_PM_KEY" not in seen


def test_the_proxy_does_not_resolve_names_on_the_event_loop() -> None:
    """A slow lookup must not stall every tunnel: it runs in a worker thread."""
    import threading

    main = threading.get_ident()
    where: list[int] = []

    def resolve(_name: str) -> list[str]:
        where.append(threading.get_ident())
        return ["93.184.216.34"]

    async def refuse(_a: str, _p: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise OSError("no upstream in tests")

    async def go() -> None:
        proxy = EgressProxy(Allowlist(["example.com"]), resolve=resolve, connect=refuse)
        host, port = await proxy.start("127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), 5)
        writer.close()

    asyncio.run(go())
    assert where and where[0] != main


def test_the_proxy_keeps_a_bounded_decision_log() -> None:
    proxy = EgressProxy(Allowlist(["example.com"]))
    from concert.sandbox.proxy import Logged

    for i in range(5_000):
        proxy.decisions.append(Logged(f"h{i}", 443, False, "x"))
    assert len(proxy.decisions) <= 1_000
