"""One pass of the long-running service: the PM cycle and this runner's worker.

A tick loads `process.yaml` from the base branch (so an admin-merged change takes effect without
a restart), beats the heart of this runner and of the PM, runs the cycle over every open issue and
lets the worker execute the runs assigned to it. An error in one issue's poll or run is recorded
and does not stop the others; a missing or invalid configuration stops the tick with the reason,
because acting on a guessed configuration would be worse than not acting.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from concert.core.derive import IssueState
from concert.github.api import GitHubApi, Identity, NotFound
from concert.github.branches import EVENTS_BRANCH, ensure_orphan_branch
from concert.github.heartbeats import heartbeat_ref, write_heartbeat
from concert.github.mirror import Mirror
from concert.runner.config import ConfigError, load_runner_config
from concert.runner.cycle import run_cycle
from concert.runner.poll import FactsProvider, RunnerConfig
from concert.runner.worker import Worker

PROCESS_FILE = "process.yaml"


@dataclass
class Service:
    pm_github: GitHubApi
    mirror: Mirror
    facts: FactsProvider
    worker: Worker
    runner: str
    base_branch: str
    clock: Callable[[], float]
    epoch: int = 1
    errors: list[str] = field(default_factory=list[str])
    _known: dict[int, IssueState] = field(default_factory=dict[int, IssueState])
    _config: tuple[str, RunnerConfig] | None = None

    def tick(self) -> None:
        now = int(self.clock())
        config = self._load_config()
        if self.runner not in config.runner_ids:
            raise ConfigError(f"runner {self.runner!r} is not registered in {PROCESS_FILE}")
        self.worker.pm = config.pm
        self.worker.me = Identity(config.runner_ids[self.runner], self.runner, is_bot=True)
        ensure_orphan_branch(self.pm_github, EVENTS_BRANCH, "concert")
        for name in (self.runner, "pm"):
            ensure_orphan_branch(self.pm_github, heartbeat_ref(name), "concert")
        write_heartbeat(self.worker.github, self.runner, [], now)
        write_heartbeat(self.pm_github, "pm", [], now)
        issues = self.pm_github.list_open_issues()
        cycle = run_cycle(
            self.pm_github, self.mirror, issues, config, self.facts, now, known=self._known
        )
        self._known = {**self._known, **cycle.states}  # a failed poll keeps its last known state
        self.errors.extend(f"#{n}: {message}" for n, message in cycle.errors.items())
        for issue in issues:
            try:
                self.worker.run_once(issue, now)
            except Exception as error:  # noqa: BLE001  one issue's failure must not stop the others
                self.errors.append(f"#{issue}: {error}")

    def _load_config(self) -> RunnerConfig:
        text = self.pm_github.read_file(self.base_branch, PROCESS_FILE)
        if text is None:
            raise ConfigError(f"{PROCESS_FILE} not found on {self.base_branch}")
        if self._config is None or self._config[0] != text:
            self._config = (text, load_runner_config(text, self.epoch, self.base_branch))
        return self._config[1]


__all__ = ["NotFound", "Service"]
