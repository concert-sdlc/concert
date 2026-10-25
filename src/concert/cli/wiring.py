"""Building the service from settings: one place that knows how the pieces connect."""

import os
from collections.abc import Callable
from pathlib import Path

from concert.agents.claude_code import ClaudeCodeAgent
from concert.cli.settings import AppCredentials, Settings, SettingsError
from concert.github.api import Identity
from concert.github.app_auth import AppAuth
from concert.github.git_facts import GitFacts
from concert.github.http import GitHubHttp, Transport
from concert.github.mirror import Mirror
from concert.github.rest import ProjectBoard, RestGitHub
from concert.runner.service import Service
from concert.runner.worker import Worker
from concert.sandbox.executor import DockerExecutor


def build_service(settings: Settings, transport: Transport, clock: Callable[[], float]) -> Service:
    pm_github = _client(settings, settings.pm, transport, clock, settings.board)
    worker_github = _client(settings, settings.worker, transport, clock, None)
    agent = ClaudeCodeAgent(
        DockerExecutor(settings.docker, environ=dict(os.environ)),
        model=settings.model,
        timeout=settings.agent_timeout,
        max_budget_usd=settings.max_budget_usd,
    )
    # the real identities (bot user ids) come from process.yaml on every tick
    unknown = Identity(user_id=0, login="unconfigured", is_bot=True)
    worker = Worker(
        worker_github,
        settings.runner,
        unknown,
        unknown,
        agent,
        base_branch=settings.base_branch,
        memory=_state_dir() / f"{settings.runner}-unpostable.json",
    )
    return Service(
        pm_github=pm_github,
        mirror=Mirror(pm_github, author="concert-pm"),
        facts=GitFacts(pm_github, base_branch=settings.base_branch),
        worker=worker,
        runner=settings.runner,
        base_branch=settings.base_branch,
        clock=clock,
        steering=settings.steering_issue,
    )


def _client(
    settings: Settings,
    app: AppCredentials,
    transport: Transport,
    clock: Callable[[], float],
    board: ProjectBoard | None,
) -> RestGitHub:
    auth = AppAuth(transport, _read_key(app.key_path), app.app_id, app.installation_id, clock)
    http = GitHubHttp(transport, token=auth.token)
    return RestGitHub(http, settings.owner, settings.repo, board)


def _read_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise SettingsError(f"cannot read the private key {path}: {error.strerror}") from error


def _state_dir() -> Path:
    """Where this runner remembers what must survive a restart (CONCERT_STATE_DIR, else ~/.concert)."""
    return Path(os.environ.get("CONCERT_STATE_DIR") or Path.home() / ".concert")
