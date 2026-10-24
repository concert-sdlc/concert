"""Runner settings: environment variables in, a validated structure out.

Credentials are never values here: an App's private key is a path, read only when a token is
needed, and the model API key is passed into the sandbox by name from the runner's environment.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from concert.github.rest import ProjectBoard
from concert.sandbox.executor import DockerSpec


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class AppCredentials:
    app_id: int
    installation_id: int
    key_path: Path


@dataclass(frozen=True)
class Settings:
    owner: str
    repo: str
    runner: str
    base_branch: str
    pm: AppCredentials
    worker: AppCredentials
    board: ProjectBoard | None
    model: str
    interval: int
    agent_timeout: int
    docker: DockerSpec
    steering_issue: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        problems: list[str] = []

        def need(name: str) -> str:
            if not env.get(name):
                problems.append(f"{name} is required")
                return ""
            return env[name]

        def number(name: str, default: int | None = None) -> int:
            raw = env.get(name)
            if raw is None:
                if default is None:
                    need(name)
                    return 0
                return default
            if not raw.isdigit() or int(raw) < 1:
                problems.append(f"{name} must be a positive integer")
                return 0
            return int(raw)

        repo_full = need("CONCERT_REPO")
        owner, _, repo = repo_full.partition("/")
        if repo_full and (not owner or not repo or "/" in repo):
            problems.append("CONCERT_REPO must look like owner/name")
        runner = need("CONCERT_RUNNER")
        pm = AppCredentials(
            number("CONCERT_PM_APP_ID"),
            number("CONCERT_PM_INSTALLATION_ID"),
            Path(need("CONCERT_PM_KEY")),
        )
        worker = AppCredentials(
            number("CONCERT_WORKER_APP_ID"),
            number("CONCERT_WORKER_INSTALLATION_ID"),
            Path(need("CONCERT_WORKER_KEY")),
        )
        board: ProjectBoard | None = None
        if env.get("CONCERT_BOARD_OWNER") or env.get("CONCERT_BOARD_NUMBER"):
            board = ProjectBoard(
                owner=need("CONCERT_BOARD_OWNER"), number=number("CONCERT_BOARD_NUMBER")
            )
        interval = number("CONCERT_INTERVAL", 20)
        timeout = number("CONCERT_AGENT_TIMEOUT", 3600)
        docker = DockerSpec(
            image=env.get("CONCERT_DOCKER_IMAGE", "concert-agent:latest"),
            network=env.get("CONCERT_DOCKER_NETWORK", "concert-egress"),
            proxy_url=env.get("CONCERT_PROXY_URL", "http://172.20.0.1:3128"),
            runtime=env.get("CONCERT_DOCKER_RUNTIME", "runsc"),
            passthrough_env=("ANTHROPIC_API_KEY",),
        )
        steering = (
            number("CONCERT_STEERING_ISSUE", 0) or None if "CONCERT_STEERING_ISSUE" in env else None
        )
        if problems:
            raise SettingsError("; ".join(problems))
        return cls(
            owner=owner,
            repo=repo,
            runner=runner,
            base_branch=env.get("CONCERT_BASE_BRANCH", "staging"),
            pm=pm,
            worker=worker,
            board=board,
            model=env.get("CONCERT_MODEL", "claude-opus-5-5"),
            interval=interval,
            agent_timeout=timeout,
            docker=docker,
            steering_issue=steering,
        )
