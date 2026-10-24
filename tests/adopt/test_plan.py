"""Adoption: the files an admin reviews and merges to put a project under Concert."""

import yaml

from concert.adopt.plan import Human, InitInputs, RunnerSpec, plan_init
from concert.runner.config import load_runner_config
from concert.workflows import render_ci, workflow_hash

INPUTS = InitInputs(
    owner="acme",
    repo="app",
    staging_branch="staging",
    admins=(Human("alice", 583231),),
    approvers=(Human("alice", 583231), Human("pat", 9912004)),
    contributors=(Human("bob", 7741120),),
    pm_login="concert-pm[bot]",
    pm_bot_id=346900000,
    runners=(
        RunnerSpec("vm-1", "concert-vm1[bot]", 346900002, ("docker", "browser", "claude-code"), 2),
    ),
    ci_local={"tests": "make test", "lint": "make lint"},
    ci_setup="make deps",
    steering_issue=7,
)


def test_the_plan_contains_the_config_and_both_workflows() -> None:
    files = plan_init(INPUTS)
    assert set(files) == {
        "process.yaml",
        ".github/workflows/concert-ci.yml",
        ".github/workflows/concert-watchdog.yml",
    }


def test_the_workflows_are_exactly_the_renders() -> None:
    files = plan_init(INPUTS)
    assert files[".github/workflows/concert-ci.yml"] == render_ci(INPUTS.ci_local, "make deps")
    watchdog = files[".github/workflows/concert-watchdog.yml"]
    assert "--steering 7" in watchdog and "--runners vm-1,pm" in watchdog
    assert "--admins alice" in watchdog


def test_the_config_pins_the_hashes_of_the_workflows_it_ships() -> None:
    files = plan_init(INPUTS)
    doc = yaml.safe_load(files["process.yaml"])
    pinned = doc["workflows"]["allowed_hashes"]
    ci = pinned["concert-ci.yml"][0]
    assert ci["sha256"] == workflow_hash(files[".github/workflows/concert-ci.yml"])
    assert ci["template"].startswith("concert-ci@") and len(ci["inputs"]) == 64
    wd = pinned["concert-watchdog.yml"][0]
    assert wd["sha256"] == workflow_hash(files[".github/workflows/concert-watchdog.yml"])


def test_the_config_loads_back_into_the_runner_configuration() -> None:
    config = load_runner_config(plan_init(INPUTS)["process.yaml"], epoch=1, base_branch="staging")
    assert config.pm.user_id == 346900000 and config.runner_ids == {"vm-1": 346900002}
    assert config.gate_policy is not None
    assert config.gate_policy.dod_items == frozenset({"tests", "lint"})
    assert len(config.gate_policy.allowed_workflows) == 2
    assert config.gate_policy.admins == frozenset({583231})
    assert config.gate_policy.approvers == frozenset({583231, 9912004})
    assert ".github/**" in config.protected_paths and "process.yaml" in config.protected_paths
    assert config.on_failure == "auto_revert" and config.verify_reruns == 1


def test_the_plan_is_deterministic() -> None:
    assert plan_init(INPUTS) == plan_init(INPUTS)


def test_the_config_records_the_local_commands() -> None:
    doc = yaml.safe_load(plan_init(INPUTS)["process.yaml"])
    assert doc["ci"]["local"] == {"tests": "make test", "lint": "make lint"}
    assert doc["ci"]["setup"] == "make deps"
    assert doc["staging"]["persistent_deploy"] is False


def test_an_admin_is_also_an_approver_in_the_file() -> None:
    doc = yaml.safe_load(plan_init(INPUTS)["process.yaml"])
    assert {"login": "alice", "id": 583231} in doc["humans"]["approvers"]
