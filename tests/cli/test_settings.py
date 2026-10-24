"""Settings come from the environment and flags; secrets are paths, never values."""

from pathlib import Path

import pytest

from concert.cli.settings import Settings, SettingsError

ENV = {
    "CONCERT_REPO": "acme/app",
    "CONCERT_RUNNER": "vm-1",
    "CONCERT_PM_APP_ID": "910000",
    "CONCERT_PM_INSTALLATION_ID": "52000100",
    "CONCERT_PM_KEY": "/keys/pm.pem",
    "CONCERT_WORKER_APP_ID": "910002",
    "CONCERT_WORKER_INSTALLATION_ID": "52000102",
    "CONCERT_WORKER_KEY": "/keys/worker.pem",
}


def test_a_complete_environment_becomes_settings_with_defaults() -> None:
    s = Settings.from_env(ENV)
    assert (s.owner, s.repo, s.runner, s.base_branch) == ("acme", "app", "vm-1", "staging")
    assert (s.pm.app_id, s.pm.installation_id, s.pm.key_path) == (
        910000,
        52000100,
        Path("/keys/pm.pem"),
    )
    assert (s.worker.app_id, s.worker.key_path) == (910002, Path("/keys/worker.pem"))
    assert s.interval == 20 and s.board is None and s.model == "claude-opus-5-5"
    assert s.docker.runtime == "runsc" and s.docker.network == "concert-egress"


def test_optional_settings_are_read() -> None:
    env = ENV | {
        "CONCERT_BASE_BRANCH": "develop",
        "CONCERT_INTERVAL": "45",
        "CONCERT_BOARD_OWNER": "acme",
        "CONCERT_BOARD_NUMBER": "4",
        "CONCERT_MODEL": "claude-sonnet-5-5",
        "CONCERT_DOCKER_IMAGE": "ghcr.io/acme/agent:1",
        "CONCERT_DOCKER_RUNTIME": "runc",
        "CONCERT_PROXY_URL": "http://10.0.0.2:3128",
    }
    s = Settings.from_env(env)
    assert s.base_branch == "develop" and s.interval == 45 and s.model == "claude-sonnet-5-5"
    assert s.board is not None and (s.board.owner, s.board.number) == ("acme", 4)
    assert s.docker.image == "ghcr.io/acme/agent:1" and s.docker.runtime == "runc"
    assert s.docker.proxy_url == "http://10.0.0.2:3128"


def test_every_missing_required_setting_is_named_at_once() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"CONCERT_REPO": "acme/app"})
    message = str(caught.value)
    for name in ("CONCERT_RUNNER", "CONCERT_PM_APP_ID", "CONCERT_WORKER_KEY"):
        assert name in message


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"CONCERT_REPO": "justaname"}, "owner/name"),
        ({"CONCERT_PM_APP_ID": "abc"}, "CONCERT_PM_APP_ID"),
        ({"CONCERT_INTERVAL": "0"}, "CONCERT_INTERVAL"),
        ({"CONCERT_BOARD_OWNER": "acme"}, "CONCERT_BOARD_NUMBER"),
    ],
)
def test_invalid_values_are_refused_with_the_setting_named(
    change: dict[str, str], text: str
) -> None:
    with pytest.raises(SettingsError, match=text):
        Settings.from_env(ENV | change)


def test_the_model_api_key_is_passed_into_the_sandbox_by_name_only() -> None:
    assert Settings.from_env(ENV).docker.passthrough_env == ("ANTHROPIC_API_KEY",)
