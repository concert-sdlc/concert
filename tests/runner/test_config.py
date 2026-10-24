"""process.yaml becomes the runner configuration (design: process.yaml, runners)."""

import pytest

from concert.core.assign import RunnerInfo
from concert.core.commands import Role
from concert.runner.config import ConfigError, load_runner_config

YAML = """
version: 0
humans:
  admins:       [{ login: alice, id: 583231 }]
  approvers:    [{ login: alice, id: 583231 }, { login: pat, id: 9912004 }]
  contributors: [{ login: bob, id: 7741120 }]
runners:
  - { name: vm-1, identity: "concert-vm1[bot]", app_id: 910002, installation_id: 52000102,
      bot_user_id: 346900002, caps: [docker, browser, claude-code], max_parallel: 2,
      can_coordinate: true }
pm_identity: { login: "concert-pm[bot]", app_id: 910000, installation_id: 52000100, bot_user_id: 346900000 }
defaults: { retry_budget: 4 }
dod:
  tests: pass
  coverage_changed_lines_min: 80
  lint: pass
  sast: { max_severity: medium }
  a11y: { when: frontend_changed, level: AA }
  docs: [changelog]
protected_paths: [process.yaml, ".github/**"]
security:
  sensitive_paths: ["**/auth/**"]
workflows:
  allowed_hashes:
    concert-ci.yml:
      - { sha256: aaa, template: concert-ci@1.0.0, inputs: bbb }
states:
  verifying_staging: { rerun_on_failure: 1, on_failure: auto_revert }
"""


def test_a_complete_file_becomes_a_runner_config() -> None:
    config = load_runner_config(YAML, epoch=3, base_branch="staging")
    assert config.pm.user_id == 346900000 and config.pm.login == "concert-pm[bot]"
    assert config.runner_ids == {"vm-1": 346900002}
    assert config.epoch == 3 and config.budget == 4 and config.base_branch == "staging"
    assert config.runners == (
        RunnerInfo("vm-1", caps=frozenset({"docker", "browser", "claude-code"}), max_parallel=2),
    )
    assert config.on_failure == "auto_revert" and config.verify_reruns == 1
    assert config.protected_paths == ("process.yaml", ".github/**")
    assert config.sensitive_paths == ("**/auth/**",)


def test_the_highest_role_wins_for_a_user_listed_twice() -> None:
    config = load_runner_config(YAML, epoch=1, base_branch="staging")
    assert config.roles == {
        583231: Role.ADMIN,
        9912004: Role.APPROVER,
        7741120: Role.CONTRIBUTOR,
    }


def test_the_gate_policy_comes_from_the_dod_and_the_allowed_workflows() -> None:
    policy = load_runner_config(YAML, epoch=1, base_branch="staging").gate_policy
    assert policy is not None
    # items with a CI result; docs has none and a11y applies only when the frontend changed
    assert policy.dod_items == frozenset({"tests", "coverage_changed_lines_min", "lint", "sast"})
    assert policy.allowed_workflows == frozenset({"aaa"})
    assert policy.admins == frozenset({583231})
    assert policy.approvers == frozenset({583231, 9912004})


def test_the_policy_revision_changes_when_the_file_changes() -> None:
    first = load_runner_config(YAML, epoch=1, base_branch="staging").gate_policy
    second = load_runner_config(
        YAML.replace("max_severity: medium", "max_severity: low"), 1, "staging"
    ).gate_policy
    assert first is not None and second is not None
    assert first.policy_rev != second.policy_rev
    assert first.policy_rev == load_runner_config(YAML, 1, "staging").gate_policy.policy_rev  # type: ignore[union-attr]


def test_defaults_apply_to_a_minimal_file() -> None:
    minimal = """
version: 0
humans: { admins: [{ login: a, id: 1 }], approvers: [], contributors: [] }
runners: []
pm_identity: { login: pm, bot_user_id: 9 }
"""
    config = load_runner_config(minimal, epoch=1, base_branch="main")
    assert config.budget == 4 and config.on_failure == "freeze" and config.verify_reruns == 1
    assert config.runners == () and config.protected_paths == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not: [valid", "yaml"),
        ("- a\n- b", "mapping"),
        ("version: 1\nhumans: {}\npm_identity: {login: p, bot_user_id: 1}", "version"),
        ("version: 0\nhumans: {}", "pm_identity"),
        ("version: 0\npm_identity: {login: p, bot_user_id: 1}", "humans"),
        (
            "version: 0\nhumans: {admins: [{login: a}]}\npm_identity: {login: p, bot_user_id: 1}",
            "id",
        ),
        (
            (
                "version: 0\nhumans: {}\npm_identity: {login: p, bot_user_id: 1}\n"
                "states: {verifying_staging: {on_failure: explode}}"
            ),
            "on_failure",
        ),
        (
            (
                "version: 0\nhumans: {}\npm_identity: {login: p, bot_user_id: 1}\n"
                "defaults: {retry_budget: 0}"
            ),
            "retry_budget",
        ),
    ],
)
def test_an_invalid_file_is_refused_with_the_reason(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_runner_config(text, epoch=1, base_branch="staging")


def test_the_number_of_verification_re_runs_is_read_from_the_file() -> None:
    config = load_runner_config(
        YAML.replace("rerun_on_failure: 1", "rerun_on_failure: 3"), 1, "staging"
    )
    assert config.verify_reruns == 3
