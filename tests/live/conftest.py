"""Live contract tests: the same behaviour the fake promises, checked against real GitHub.

Opt-in. They create issues, branches and PRs in a THROWAWAY repository, so they refuse to run
unless `CONCERT_LIVE=1` and `CONCERT_LIVE_CONFIRM` names the same repository as `CONCERT_REPO`.
The rest of the settings are the runner's own (see docs/STATUS.md). See docs/LIVE_VALIDATION.md.
"""

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from concert.cli.settings import AppCredentials, Settings
from concert.github.app_auth import AppAuth
from concert.github.http import GitHubHttp
from concert.github.rest import ProjectBoard, RestGitHub
from concert.github.transport import UrllibTransport


@dataclass(frozen=True)
class Live:
    settings: Settings
    pm: RestGitHub  # the PM App: issues, board, merges
    worker: RestGitHub  # the Worker App: branches and PRs
    prefix: str  # every branch and title this run creates starts with it


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("CONCERT_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live tests need CONCERT_LIVE=1 (see docs/LIVE_VALIDATION.md)")
    for item in items:
        if "tests/live" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


def _client(app: AppCredentials, settings: Settings, board: ProjectBoard | None) -> RestGitHub:
    auth = AppAuth(
        UrllibTransport(require_https=True),
        Path(app.key_path).read_text(encoding="utf-8"),
        app.app_id,
        app.installation_id,
        time.time,
    )
    http = GitHubHttp(UrllibTransport(require_https=True), token=auth.token)
    return RestGitHub(http, settings.owner, settings.repo, board)


@pytest.fixture(scope="session")
def live() -> Iterator[Live]:
    settings = Settings.from_env(os.environ)
    full = f"{settings.owner}/{settings.repo}"
    if os.environ.get("CONCERT_LIVE_CONFIRM") != full:
        pytest.exit(
            f"refusing to run: set CONCERT_LIVE_CONFIRM={full} to confirm this is a throwaway repo",
            returncode=2,
        )
    prefix = f"live-{int(time.time())}"
    client = Live(
        settings,
        _client(settings.pm, settings, settings.board),
        _client(settings.worker, settings, None),
        prefix,
    )
    yield client
    _cleanup(client)


def _cleanup(live: Live) -> None:
    """Best effort: close this run's issues and PRs, delete its branches. Merged work stays."""
    for number in live.pm.list_open_issues():
        issue = live.pm.get_issue(number)
        if issue.title.startswith(live.prefix):
            live.pm.close_issue(number)
    pulls = live.pm.http.paged(
        f"/repos/{live.settings.owner}/{live.settings.repo}/pulls", {"state": "open"}
    )
    for raw in pulls:
        if str(raw["head"]["ref"]).startswith(live.prefix):
            live.pm.http.json(
                "PATCH",
                f"/repos/{live.settings.owner}/{live.settings.repo}/pulls/{raw['number']}",
                {"state": "closed"},
            )
    refs = live.pm.http.paged(
        f"/repos/{live.settings.owner}/{live.settings.repo}/git/matching-refs/heads/{live.prefix}"
    )
    for raw in refs:
        live.pm.delete_ref(str(raw["ref"]).removeprefix("refs/heads/"))


def wait_for[T](produce: Callable[[], T | None], seconds: int = 180, every: int = 5) -> T | None:
    """Poll until `produce` returns something other than None, or give up."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = produce()
        if found is not None:
            return found
        time.sleep(every)
    return None
