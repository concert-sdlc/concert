"""The command line: wiring, the run loop and the proxy command."""

import io
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from concert.cli import main
from concert.cli.settings import Settings
from concert.cli.wiring import build_service, file_name
from concert.github.http import Response


class Offline:
    """A transport that fails every call, so wiring can be built and inspected without a network."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        self.calls += 1
        raise ConnectionError("offline")


def write_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def env_for(tmp_path: Path) -> dict[str, str]:
    write_key(tmp_path / "pm.pem")
    write_key(tmp_path / "worker.pem")
    return {
        "CONCERT_REPO": "acme/app",
        "CONCERT_RUNNER": "vm-1",
        "CONCERT_PM_APP_ID": "910000",
        "CONCERT_PM_INSTALLATION_ID": "1",
        "CONCERT_PM_KEY": str(tmp_path / "pm.pem"),
        "CONCERT_WORKER_APP_ID": "910002",
        "CONCERT_WORKER_INSTALLATION_ID": "2",
        "CONCERT_WORKER_KEY": str(tmp_path / "worker.pem"),
    }


def test_wiring_builds_a_service_without_touching_the_network(tmp_path: Path) -> None:
    transport = Offline()
    service = build_service(Settings.from_env(env_for(tmp_path)), transport, clock=lambda: 0.0)
    assert service.runner == "vm-1" and service.base_branch == "staging"
    assert transport.calls == 0  # nothing is fetched until a tick


def test_a_missing_key_file_is_reported_as_a_settings_problem(tmp_path: Path) -> None:
    env = env_for(tmp_path) | {
        "CONCERT_PM_KEY": str(tmp_path / "nope.pem"),
        "ANTHROPIC_API_KEY": "sk-test",
    }
    err = io.StringIO()
    assert main(["run", "--once"], env=env, transport=Offline(), stderr=err) == 2
    assert "nope.pem" in err.getvalue()


def test_missing_settings_exit_with_a_message_and_status_2() -> None:
    err = io.StringIO()
    assert main(["run", "--once"], env={}, stderr=err) == 2
    assert "CONCERT_REPO is required" in err.getvalue()


class Ticking:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.ticks = 0
        self.fail_with = fail_with
        self.errors: list[str] = []

    def tick(self) -> None:
        self.ticks += 1
        if self.fail_with:
            raise self.fail_with


def test_run_once_ticks_one_time_and_succeeds(tmp_path: Path) -> None:
    ticking = Ticking()
    code = main(
        ["run", "--once"],
        env=env_for(tmp_path),
        service_factory=lambda _settings: ticking,  # type: ignore[arg-type,return-value]
        stderr=io.StringIO(),
    )
    assert code == 0 and ticking.ticks == 1


def test_run_once_reports_a_failed_tick_with_status_1(tmp_path: Path) -> None:
    err = io.StringIO()
    code = main(
        ["run", "--once"],
        env=env_for(tmp_path),
        service_factory=lambda _s: Ticking(RuntimeError("config is wrong")),  # type: ignore[arg-type,return-value]
        stderr=err,
    )
    assert code == 1 and "config is wrong" in err.getvalue()


def test_the_loop_keeps_going_after_a_failed_tick(tmp_path: Path) -> None:
    ticking = Ticking(ConnectionError("blip"))
    sleeps: list[float] = []

    def stop_after_three(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    code = main(
        ["run"],
        env=env_for(tmp_path),
        service_factory=lambda _s: ticking,  # type: ignore[arg-type,return-value]
        sleep=stop_after_three,
        stderr=io.StringIO(),
    )
    assert code == 0 and ticking.ticks == 3 and sleeps == [20, 20, 20]


def test_an_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["frobnicate"], env={}, stderr=io.StringIO())
    assert caught.value.code == 2


def test_the_proxy_command_rejects_a_missing_allowlist() -> None:
    err = io.StringIO()
    assert main(["proxy", "--listen", "127.0.0.1:0"], env={}, stderr=err) == 2
    assert "--allow" in err.getvalue()


def test_the_proxy_command_serves_until_interrupted() -> None:
    err = io.StringIO()
    started: list[tuple[str, int]] = []

    def serve(listen: tuple[str, int], hosts: list[str]) -> None:
        started.append(listen)
        assert hosts == ["pypi.org", "api.anthropic.com"]

    code = main(
        ["proxy", "--listen", "0.0.0.0:3128", "--allow", "pypi.org,api.anthropic.com"],
        env={},
        proxy_runner=serve,
        stderr=err,
    )
    assert code == 0 and started == [("0.0.0.0", 3128)]


_ = Callable  # (kept for typing helpers above)


def test_the_watchdog_command_runs_the_check_with_the_actions_token() -> None:
    seen: dict[str, object] = {}

    def fake_watchdog(**kwargs: object) -> list[str]:
        seen.update(kwargs)
        return ["alert vm-1"]

    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "watchdog",
            "--repo",
            "acme/app",
            "--steering",
            "7",
            "--stale-after",
            "30",
            "--runners",
            "vm-1,pm",
            "--admins",
            "alice,bob",
        ],
        env={"GITHUB_TOKEN": "ghs_x"},
        stderr=err,
        stdout=out,
        watchdog_runner=fake_watchdog,
        clock=lambda: 5000.0,
    )
    assert code == 0 and "alert vm-1" in out.getvalue()
    assert seen["steering"] == 7 and seen["stale_after"] == 1800 and seen["now"] == 5000
    assert seen["runners"] == ["vm-1", "pm"] and seen["admins"] == ["alice", "bob"]
    assert seen["authors"] == {41898282}


def test_the_watchdog_needs_its_token_and_arguments() -> None:
    err = io.StringIO()
    args = [
        "watchdog",
        "--repo",
        "acme/app",
        "--steering",
        "7",
        "--runners",
        "vm-1",
        "--admins",
        "a",
    ]
    assert main(args, env={}, stderr=err) == 2 and "GITHUB_TOKEN" in err.getvalue()
    err = io.StringIO()
    assert main(["watchdog", "--repo", "bad"], env={"GITHUB_TOKEN": "x"}, stderr=err) == 2


def test_a_missing_agent_credential_stops_the_run_before_anything_starts(tmp_path: Path) -> None:
    err = io.StringIO()
    code = main(["run", "--once"], env=env_for(tmp_path), transport=Offline(), stderr=err)
    assert code == 2 and "ANTHROPIC_API_KEY" in err.getvalue()
    err = io.StringIO()
    env = env_for(tmp_path) | {"CONCERT_AGENT_AUTH": "oauth_token", "ANTHROPIC_API_KEY": "sk-x"}
    assert main(["run", "--once"], env=env, transport=Offline(), stderr=err) == 2
    assert "CLAUDE_CODE_OAUTH_TOKEN" in err.getvalue()  # the API key does not stand in for it


def test_a_runner_name_cannot_escape_the_state_directory() -> None:
    assert file_name("../../etc/x") == "_.._etc_x"
    assert "/" not in file_name("a/b") and not file_name("../x").startswith(".")
    assert file_name("live-test") == "live-test" and file_name("") == "runner"
