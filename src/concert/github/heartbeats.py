"""Heartbeat refs: `concert-heartbeats/<runner>`, pushed by that runner's Worker App.

The heartbeat is a one-file commit listing the run_ids the runner is executing and the time.
The PM reads it to decide whether a lease still holds. An unreadable heartbeat counts as no
heartbeat, so a corrupt ref can only make a lease expire, never extend it.
"""

import json
from collections.abc import Iterable
from typing import Any, cast

from concert.core.assign import Heartbeat
from concert.github.api import GitHubApi, NotFound

_FILE = "heartbeat.json"


def heartbeat_ref(runner: str) -> str:
    return f"concert-heartbeats/{runner}"


def write_heartbeat(github: GitHubApi, runner: str, run_ids: Iterable[str], now: int) -> None:
    body = json.dumps({"runner": runner, "at": now, "run_ids": sorted(run_ids)}, sort_keys=True)
    github.commit(
        heartbeat_ref(runner), "heartbeat", {_FILE: body}, author=runner, committer=runner
    )


def read_heartbeat(github: GitHubApi, runner: str) -> Heartbeat | None:
    try:
        text = github.read_file(heartbeat_ref(runner), _FILE)
    except NotFound:
        return None
    if not text:
        return None
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    at, run_ids = data.get("at"), data.get("run_ids")
    if not isinstance(at, int) or not isinstance(run_ids, list):
        return None
    ids = cast("list[Any]", run_ids)
    if not all(isinstance(item, str) for item in ids):
        return None
    return Heartbeat(runner=runner, at=at, run_ids=frozenset(cast("list[str]", ids)))
