"""process.yaml -> RunnerConfig.

Pure: takes the file's text (the caller reads it from the staging head) and returns the
configuration the poll loop runs with, or refuses with the reason. The policy revision is a
hash of the whole file, so any change to it makes earlier CI evidence stale.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, cast

import yaml

from concert.core.assign import RunnerInfo
from concert.core.commands import Role
from concert.core.derive import DEFAULT_BUDGET
from concert.core.evidence import GatePolicy
from concert.core.transitions import IssueType
from concert.github.api import Identity
from concert.runner.poll import RunnerConfig

_ON_FAILURE = ("auto_revert", "freeze")
_NO_CI_RESULT = frozenset({"docs"})  # definition-of-done entries that CI does not report on


class ConfigError(Exception):
    pass


def load_runner_config(text: str, epoch: int, base_branch: str) -> RunnerConfig:
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"process.yaml is not valid yaml: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError("process.yaml must be a mapping")
    doc = cast("dict[str, Any]", raw)
    if doc.get("version") != 0:
        raise ConfigError(f"unsupported process.yaml version {doc.get('version')!r}")
    pm = _identity(doc, "pm_identity")
    roles = _roles(doc)
    runners = _list(doc.get("runners"))
    verify = _mapping(_mapping(doc.get("states")).get("verifying_staging"))
    on_failure = verify.get("on_failure", "freeze")
    if on_failure not in _ON_FAILURE:
        raise ConfigError(f"states.verifying_staging.on_failure must be one of {_ON_FAILURE}")
    budget = _mapping(doc.get("defaults")).get("retry_budget", DEFAULT_BUDGET)
    if not isinstance(budget, int) or budget < 1:
        raise ConfigError("defaults.retry_budget must be a positive integer")
    admins = {u for u, role in roles.items() if role is Role.ADMIN}
    approvers = {u for u, role in roles.items() if role >= Role.APPROVER}
    policy = GatePolicy(
        policy_rev=hashlib.sha256(text.encode()).hexdigest(),
        dod_items=frozenset(_dod_items(_mapping(doc.get("dod")))),
        allowed_workflows=frozenset(_allowed_workflows(_mapping(doc.get("workflows")))),
        approvers=frozenset(approvers),
        admins=frozenset(admins),
        security_sensitive=False,
        protected_changed=False,
        issue_type=IssueType.CODE,
    )
    return RunnerConfig(
        pm=pm,
        runner_ids={str(r["name"]): int(r["bot_user_id"]) for r in runners},
        roles=roles,
        epoch=epoch,
        budget=budget,
        base_branch=base_branch,
        runners=tuple(
            RunnerInfo(
                str(r["name"]),
                caps=frozenset(map(str, _list(r.get("caps")))),
                max_parallel=int(r.get("max_parallel", 1)),
            )
            for r in runners
        ),
        gate_policy=policy,
        sensitive_paths=tuple(
            map(str, _list(_mapping(doc.get("security")).get("sensitive_paths")))
        ),
        protected_paths=tuple(map(str, _list(doc.get("protected_paths")))),
        on_failure=str(on_failure),
        verify_reruns=int(verify.get("rerun_on_failure", 1)),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def _identity(doc: Mapping[str, Any], key: str) -> Identity:
    entry = _mapping(doc.get(key))
    if "login" not in entry or "bot_user_id" not in entry:
        raise ConfigError(f"{key} needs a login and a bot_user_id")
    return Identity(user_id=int(entry["bot_user_id"]), login=str(entry["login"]), is_bot=True)


def _roles(doc: Mapping[str, Any]) -> dict[int, Role]:
    if "humans" not in doc:
        raise ConfigError("humans is required")
    humans = _mapping(doc["humans"])
    roles: dict[int, Role] = {}
    for key, role in (
        ("contributors", Role.CONTRIBUTOR),
        ("approvers", Role.APPROVER),
        ("admins", Role.ADMIN),
    ):
        for person in _list(humans.get(key)):
            entry = _mapping(person)
            if not isinstance(entry.get("id"), int):
                raise ConfigError(f"humans.{key}: every entry needs a numeric id")
            roles[entry["id"]] = role  # ascending order: the highest role listed wins
    return roles


def _dod_items(dod: Mapping[str, Any]) -> Sequence[str]:
    """Items CI reports a result for: everything except notes and conditional checks."""
    return [
        name
        for name, rule in dod.items()
        if name not in _NO_CI_RESULT and not (isinstance(rule, dict) and "when" in rule)
    ]


def _allowed_workflows(workflows: Mapping[str, Any]) -> Sequence[str]:
    files = _mapping(workflows.get("allowed_hashes"))
    return [
        str(_mapping(entry).get("sha256"))
        for entries in files.values()
        for entry in _list(entries)
        if "sha256" in _mapping(entry)
    ]
