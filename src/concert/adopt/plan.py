"""What adopting a project puts in its repository: `process.yaml` and the two workflows.

Pure. The admin reviews and merges the result; the config pins the hash of each workflow it
ships, so the runner can later tell a generated file from a hand-edited one.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from concert.workflows import (
    CI_TEMPLATE,
    WATCHDOG_TEMPLATE,
    render_ci,
    render_watchdog,
    workflow_hash,
)

CI_PATH = ".github/workflows/concert-ci.yml"
WATCHDOG_PATH = ".github/workflows/concert-watchdog.yml"
# Every file an agent reads at run start is protected, as are the files that define the process.
PROTECTED = [
    "process.yaml",
    ".concert/**",
    ".github/**",
    "CODEOWNERS",
    "docs/decisions/**",
    "docs/lessons/**",
    "AGENTS.md",
    "docs/architecture.md",
    "Makefile",
    "package.json",
    "pyproject.toml",
    "tox.ini",
    "pytest.ini",
    "jest.config.*",
    "vitest.config.*",
]
SENSITIVE = [
    "**/auth/**",
    "**/permissions/**",
    "**/payments/**",
    "**/settings*",
    "**/migrations/**",
]


@dataclass(frozen=True)
class Human:
    login: str
    id: int


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    identity: str
    bot_user_id: int
    caps: tuple[str, ...]
    max_parallel: int


@dataclass(frozen=True)
class InitInputs:
    owner: str
    repo: str
    staging_branch: str
    admins: Sequence[Human]
    approvers: Sequence[Human]
    contributors: Sequence[Human]
    pm_login: str
    pm_bot_id: int
    runners: Sequence[RunnerSpec]
    ci_local: Mapping[str, str]
    ci_setup: str | None
    steering_issue: int
    watchdog_every_hours: int = 2
    watchdog_stale_minutes: int = 30
    protection: str = "enforce"  # "detect" where no rulesets protect the branches


def plan_init(inputs: InitInputs) -> dict[str, str]:
    ci = render_ci(inputs.ci_local, inputs.ci_setup)
    watchdog = render_watchdog(
        inputs.watchdog_every_hours,
        inputs.watchdog_stale_minutes,
        inputs.steering_issue,
        [*(r.name for r in inputs.runners), "pm"],
        [a.login for a in inputs.admins],
    )
    ci_inputs = hashlib.sha256(
        json.dumps(
            {"local": dict(inputs.ci_local), "setup": inputs.ci_setup}, sort_keys=True
        ).encode()
    ).hexdigest()
    watchdog_inputs = hashlib.sha256(
        json.dumps(
            [inputs.watchdog_every_hours, inputs.watchdog_stale_minutes, inputs.steering_issue],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    config = _process_yaml(inputs, ci, watchdog, ci_inputs, watchdog_inputs)
    return {
        "process.yaml": yaml.safe_dump(config, sort_keys=False, width=100),
        CI_PATH: ci,
        WATCHDOG_PATH: watchdog,
    }


def _person(human: Human) -> dict[str, Any]:
    return {"login": human.login, "id": human.id}


def _process_yaml(
    inputs: InitInputs, ci: str, watchdog: str, ci_inputs: str, watchdog_inputs: str
) -> dict[str, Any]:
    return {
        "version": 0,
        "tracker": {
            "kind": "github-projects-v2",
            "project": f"{inputs.owner}/{inputs.repo}",
            "status_field": "Status",
            "steering_issue": inputs.steering_issue,
        },
        "protection": inputs.protection,
        "humans": {
            "admins": [_person(h) for h in inputs.admins],
            "approvers": [_person(h) for h in inputs.approvers],
            "contributors": [_person(h) for h in inputs.contributors],
        },
        "runners": [
            {
                "name": r.name,
                "identity": r.identity,
                "bot_user_id": r.bot_user_id,
                "caps": list(r.caps),
                "max_parallel": r.max_parallel,
                "can_coordinate": True,
            }
            for r in inputs.runners
        ],
        "pm_identity": {"login": inputs.pm_login, "bot_user_id": inputs.pm_bot_id},
        "defaults": {"retry_budget": 4, "infra_retries": 3, "timeout": "60m"},
        "ci": {
            "local": dict(inputs.ci_local),
            **({"setup": inputs.ci_setup} if inputs.ci_setup else {}),
            "triggers": ["ready_for_review", "sha_changed_after_ready"],
            "runs_on": "github-hosted",
            "trust_local": False,
        },
        "dod": {name: "pass" for name in inputs.ci_local},
        "protected_paths": PROTECTED,
        "security": {"sensitive_paths": SENSITIVE},
        "staging": {"persistent_deploy": False, "irreversible_paths": ["**/migrations/**"]},
        "workflows": {
            "allowed_hashes": {
                "concert-ci.yml": [
                    {"sha256": workflow_hash(ci), "template": CI_TEMPLATE, "inputs": ci_inputs}
                ],
                "concert-watchdog.yml": [
                    {
                        "sha256": workflow_hash(watchdog),
                        "template": WATCHDOG_TEMPLATE,
                        "inputs": watchdog_inputs,
                    }
                ],
            }
        },
        "integration": {"pregate_refresh": 2},
        "states": {"verifying_staging": {"rerun_on_failure": 1, "on_failure": "auto_revert"}},
    }
