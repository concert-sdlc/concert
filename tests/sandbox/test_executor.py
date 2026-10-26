"""The container command line: least privilege, no secrets in argv, only the workdir mounted."""

import subprocess
from pathlib import Path

from concert.sandbox.executor import DockerExecutor, DockerSpec, ExecResult, docker_argv

SPEC = DockerSpec(
    image="concert-agent:1",
    network="concert-egress",
    proxy_url="http://172.20.0.2:3128",
    runtime="runsc",
    memory="4g",
    cpus="2",
    pids=512,
    passthrough_env=("ANTHROPIC_API_KEY",),
)


def argv(command: list[str] | None = None, name: str = "concert-r-1") -> list[str]:
    return docker_argv(SPEC, Path("/tmp/run1"), command or ["claude", "-p", "go"], name)


def test_the_container_runs_with_least_privilege() -> None:
    args = argv()
    for flag in (
        "--rm",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--pids-limit=512",
        "--memory=4g",
        "--cpus=2",
        "--runtime=runsc",
        "--network=concert-egress",
        "--name=concert-r-1",
    ):
        assert flag in args
    assert "--privileged" not in args
    assert not any("docker.sock" in a for a in args)


def test_only_the_workdir_is_mounted_and_it_is_the_working_directory() -> None:
    args = argv()
    mounts = [args[i + 1] for i, a in enumerate(args) if a in ("-v", "--volume")]
    assert mounts == ["/tmp/run1:/work:rw"]
    assert args[args.index("-w") + 1] == "/work"
    assert any(a.startswith("--tmpfs=/tmp") for a in args)


def test_the_agent_does_not_run_as_root() -> None:
    args = argv()
    assert args[args.index("--user") + 1] not in ("0", "0:0", "root")


def test_traffic_goes_through_the_proxy() -> None:
    args = argv()
    env = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    assert (
        "HTTPS_PROXY=http://172.20.0.2:3128" in env and "https_proxy=http://172.20.0.2:3128" in env
    )
    # only loopback bypasses it: the app under test runs inside the container, and the proxy
    # answers 405 to a request for 127.0.0.1; nothing external is exempt
    assert "NO_PROXY=127.0.0.1,localhost,::1" in env
    assert "no_proxy=127.0.0.1,localhost,::1" in env


def test_secret_values_never_appear_in_the_command_line() -> None:
    args = argv()
    assert "ANTHROPIC_API_KEY" in args  # passed by name only
    assert not any("sk-" in a for a in args)


def test_the_command_comes_last_after_the_image() -> None:
    args = argv(["claude", "-p", "do it"])
    assert args[-4:] == ["concert-agent:1", "claude", "-p", "do it"]


class Recorder:
    def __init__(self, result: subprocess.CompletedProcess[str] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    def __call__(
        self, args: list[str], env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, env, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_the_executor_passes_secrets_through_the_environment_not_argv() -> None:
    done = subprocess.CompletedProcess([], 0, stdout="out", stderr="err")
    recorder = Recorder(done)
    executor = DockerExecutor(
        SPEC,
        run=recorder,
        environ={"ANTHROPIC_API_KEY": "sk-secret", "HOME": "/h", "GITHUB_TOKEN": "ghs_other"},
    )
    result = executor.execute(["claude"], Path("/tmp/w"), "concert-r-2", timeout=30)
    assert result == ExecResult(0, "out", "err", timed_out=False)
    args, env, timeout = recorder.calls[0]
    assert env["ANTHROPIC_API_KEY"] == "sk-secret" and env["HOME"] == "/h"
    assert "GITHUB_TOKEN" not in env  # only the CLI's own variables and the named secrets
    assert "sk-secret" not in " ".join(args) and timeout == 30


def test_a_timeout_kills_the_container_and_is_reported() -> None:
    killed: list[list[str]] = []
    recorder = Recorder(subprocess.TimeoutExpired(["docker"], 5))

    def kill(args: list[str]) -> None:
        killed.append(args)

    executor = DockerExecutor(SPEC, run=recorder, environ={}, kill=kill)
    result = executor.execute(["claude"], Path("/tmp/w"), "concert-r-3", timeout=5)
    assert result.timed_out and result.exit_code != 0
    assert killed == [["docker", "rm", "-f", "concert-r-3"]]
