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
from concert.github.http import Transport
from concert.github.transport import UrllibTransport
from concert.runner.service import Service
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
) -> int:
    err = stderr if stderr is not None else sys.stderr
    environment = os.environ if env is None else env
    args = _parser().parse_args(argv)
    if args.command == "proxy":
        return _proxy(args, err, proxy_runner or _serve_proxy)
    try:
        settings = Settings.from_env(environment)
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
    proxy = commands.add_parser("proxy", help="run the egress proxy for sandboxed agents")
    proxy.add_argument("--listen", default="127.0.0.1:3128")
    proxy.add_argument("--allow", help="comma-separated host names agents may reach over https")
    return parser
