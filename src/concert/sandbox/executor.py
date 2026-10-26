"""Running an agent command inside a locked-down container.

The command line is built by a pure function so its hardening can be tested without Docker: no
capabilities, no privilege escalation, read-only root, a non-root user, resource limits, only the
run's directory mounted, and no network except the egress proxy. Secrets are passed by name and
carried in the process environment, so their values never appear in `ps` or logs.
"""

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Executor(Protocol):
    def execute(
        self, command: list[str], workdir: Path, name: str, timeout: float
    ) -> ExecResult: ...


@dataclass(frozen=True)
class DockerSpec:
    image: str
    network: str  # a network whose only exit is the egress proxy
    proxy_url: str
    runtime: str = "runsc"  # gVisor; use "runc" only where the host is already a disposable VM
    memory: str = "4g"
    cpus: str = "2"
    pids: int = 512
    user: str = "10001:10001"
    passthrough_env: tuple[str, ...] = ()  # names only; values come from the runner's environment


def docker_argv(spec: DockerSpec, workdir: Path, command: list[str], name: str) -> list[str]:
    env: list[str] = []
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        env += ["-e", f"{key}={spec.proxy_url}"]
    for key in ("NO_PROXY", "no_proxy"):
        env += ["-e", f"{key}=127.0.0.1,localhost,::1"]  # loopback is the app under test
    env += ["-e", "PYTHONDONTWRITEBYTECODE=1"]
    for key in spec.passthrough_env:
        env += ["-e", key]
    return [
        "docker",
        "run",
        "--rm",
        f"--name={name}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs=/tmp:rw,size=512m",
        f"--pids-limit={spec.pids}",
        f"--memory={spec.memory}",
        f"--cpus={spec.cpus}",
        f"--runtime={spec.runtime}",
        f"--network={spec.network}",
        "--user",
        spec.user,
        "-v",
        f"{workdir}:/work:rw",
        "-w",
        "/work",
        *env,
        spec.image,
        *command,
    ]


_CLI_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")

Run = Callable[[list[str], dict[str, str], float], "subprocess.CompletedProcess[str]"]


def _run(
    args: list[str], env: dict[str, str], timeout: float
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        args, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _kill(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, check=False)


class DockerExecutor:
    def __init__(
        self,
        spec: DockerSpec,
        run: Run = _run,
        environ: Mapping[str, str] | None = None,
        kill: Callable[[list[str]], None] = _kill,
    ) -> None:
        self.spec = spec
        self._run = run
        self._environ: Mapping[str, str] = environ if environ is not None else {}
        self._kill = kill

    def execute(self, command: list[str], workdir: Path, name: str, timeout: float) -> ExecResult:
        args = docker_argv(self.spec, workdir, command, name)
        # What the docker CLI needs to find itself, its context and its daemon, then the named
        # secrets: nothing else from the runner's environment reaches the child.
        wanted = (*_CLI_ENV, *self.spec.passthrough_env)
        env = {k: self._environ[k] for k in wanted if k in self._environ}
        try:
            done = self._run(args, env, timeout)
        except subprocess.TimeoutExpired:
            self._kill(["docker", "rm", "-f", name])
            return ExecResult(124, "", "timed out", timed_out=True)
        return ExecResult(done.returncode, done.stdout, done.stderr)
