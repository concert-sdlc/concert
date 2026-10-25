"""The `concert` command line."""

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from concert.cli.settings import Settings, SettingsError
from concert.cli.wiring import build_service
from concert.github.http import GitHubHttp, Transport
from concert.github.rest import RestGitHub
from concert.github.transport import UrllibTransport
from concert.runner.service import Service
from concert.runner.watchdog import run_watchdog
from concert.sandbox.egress import Allowlist
from concert.sandbox.proxy import EgressProxy

ServiceFactory = Callable[[Settings], Service]
ProxyRunner = Callable[[tuple[str, int], list[str]], None]


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    service_factory: ServiceFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    stderr: TextIO | None = None,
    proxy_runner: ProxyRunner | None = None,
    watchdog_runner: Callable[..., list[str]] | None = None,
    stdout: TextIO | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    err = stderr if stderr is not None else sys.stderr
    environment = os.environ if env is None else env
    args = _parser().parse_args(argv)
    if args.command == "proxy":
        return _proxy(args, err, proxy_runner or _serve_proxy)
    if args.command == "watchdog":
        return _watchdog(args, environment, err, stdout or sys.stdout, watchdog_runner, clock)
    try:
        settings = Settings.from_env(environment)
        if service_factory is None and not environment.get(settings.agent_credential):
            raise SettingsError(
                f"{settings.agent_credential} is not set (CONCERT_AGENT_AUTH={settings.agent_auth})"
            )
        service = (
            service_factory(settings)
            if service_factory is not None
            else build_service(
                settings, transport or UrllibTransport(require_https=True), time.time
            )
        )
    except SettingsError as error:
        print(f"concert: {error}", file=err)
        return 2
    return _run(service, settings.interval, args.once, sleep, err)


def _run(
    service: Service, interval: int, once: bool, sleep: Callable[[float], None], err: TextIO
) -> int:
    while True:
        try:
            service.tick()
        except KeyboardInterrupt:
            return 0
        except Exception as error:  # noqa: BLE001  a bad tick must not end the service
            print(f"concert: tick failed: {type(error).__name__}: {error}", file=err)
            if once:
                return 1
        else:
            for problem in service.errors:
                print(f"concert: {problem}", file=err)
            service.errors.clear()
            if once:
                return 0
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return 0


ACTIONS_BOT_ID = 41898282  # github-actions[bot]: the account behind the workflow's GITHUB_TOKEN


def _watchdog(
    args: argparse.Namespace,
    env: Mapping[str, str],
    err: TextIO,
    out: TextIO,
    runner: Callable[..., list[str]] | None,
    clock: Callable[[], float],
) -> int:
    owner, _, repo = str(args.repo).partition("/")
    token = env.get("GITHUB_TOKEN")
    problems: list[str] = []
    if not owner or not repo:
        problems.append("--repo must look like owner/name")
    if not token:
        problems.append("GITHUB_TOKEN is required")
    if not args.steering:
        problems.append("--steering is required")
    if problems:
        print("concert: " + "; ".join(problems), file=err)
        return 2
    run = runner or run_watchdog
    github = runner_client(owner, repo, token or "")
    done = run(
        github=github,
        steering=args.steering,
        runners=_split(args.runners),
        admins=_split(args.admins),
        stale_after=int(args.stale_after) * 60,
        now=int(clock()),
        authors={ACTIONS_BOT_ID},
    )
    for line in done:
        print(line, file=out)
    return 0


def _split(text: str | None) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def runner_client(owner: str, repo: str, token: str) -> RestGitHub:
    http = GitHubHttp(UrllibTransport(require_https=True), token=lambda: token)
    return RestGitHub(http, owner, repo)


def _proxy(args: argparse.Namespace, err: TextIO, run: ProxyRunner) -> int:
    hosts = [h.strip() for h in (args.allow or "").split(",") if h.strip()]
    if not hosts:
        print("concert: the proxy needs --allow host[,host...]", file=err)
        return 2
    host, _, port = str(args.listen).rpartition(":")
    if not host or not port.isdigit():
        print("concert: --listen must look like host:port", file=err)
        return 2
    run((host, int(port)), hosts)
    return 0


def _serve_proxy(listen: tuple[str, int], hosts: list[str]) -> None:
    async def serve() -> None:
        proxy = EgressProxy(Allowlist(hosts))
        host, port = await proxy.start(*listen)
        print(
            f"concert: egress proxy on {host}:{port}, allowing {', '.join(hosts)}", file=sys.stderr
        )
        await asyncio.Event().wait()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="concert")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the PM and this runner's worker")
    run.add_argument("--once", action="store_true", help="do one tick and exit")
    watchdog = commands.add_parser("watchdog", help="alert in Steering when a runner goes quiet")
    watchdog.add_argument("--repo", required=True)
    watchdog.add_argument("--steering", type=int)
    watchdog.add_argument("--stale-after", type=int, default=30, help="minutes of silence")
    watchdog.add_argument("--runners", default="")
    watchdog.add_argument("--admins", default="")
    proxy = commands.add_parser("proxy", help="run the egress proxy for sandboxed agents")
    proxy.add_argument("--listen", default="127.0.0.1:3128")
    proxy.add_argument("--allow", help="comma-separated host names agents may reach over https")
    return parser
