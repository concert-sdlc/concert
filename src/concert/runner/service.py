"""One pass of the long-running service: the PM cycle and this runner's worker.

A tick loads `process.yaml` from the base branch (so an admin-merged change takes effect without
a restart), beats the heart of this runner and of the PM, runs the cycle over every open issue and
lets the worker execute the runs assigned to it. An error in one issue's poll or run is recorded
and does not stop the others; a missing or invalid configuration stops the tick with the reason,
because acting on a guessed configuration would be worse than not acting.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from concert.core.admin import AdminState
from concert.core.derive import IssueState
from concert.github.api import GitHubApi, Identity, NotFound
from concert.github.branches import EVENTS_BRANCH, ensure_orphan_branch
from concert.github.heartbeats import heartbeat_ref, write_heartbeat
from concert.github.mirror import Mirror
from concert.runner.admin import mark_reset_applied, process_steering
from concert.runner.config import ConfigError, load_runner_config
from concert.runner.cycle import run_cycle
from concert.runner.poll import FactsProvider, RunnerConfig
from concert.runner.status import render_board, render_issue
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
    steering: int | None = None  # the issue admins steer from with /pm commands
    errors: list[str] = field(default_factory=list[str])
    _known: dict[int, IssueState] = field(default_factory=dict[int, IssueState])
    _config: tuple[str, RunnerConfig] | None = None
    _admin: AdminState = field(default_factory=AdminState)

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
        self.worker.beat(now)
        write_heartbeat(self.pm_github, "pm", [], now)
        issues = [n for n in self.pm_github.list_open_issues() if n != self.steering]
        admin = self._steer(config, now)
        cycle = run_cycle(
            self.pm_github,
            self.mirror,
            issues,
            config,
            self.facts,
            now,
            known=self._known,
            admin=admin,
        )
        for number, result in cycle.results.items():
            if result.reset_handled and self.steering is not None:
                mark_reset_applied(self.pm_github, self.mirror, self.steering, config, number, now)
        # a failed poll keeps its last known state; a closed issue is forgotten, or its last
        # state (say, integrating) would hold the queue until the process restarts
        still_open = set(issues)
        self._known = {n: s for n, s in {**self._known, **cycle.states}.items() if n in still_open}
        self.errors.extend(f"#{n}: {message}" for n, message in cycle.errors.items())
        for issue in issues:
            try:
                self.worker.run_once(issue, now)
            except Exception as error:  # noqa: BLE001  one issue's failure must not stop the others
                self.errors.append(f"#{issue}: {error}")

    def _steer(self, config: RunnerConfig, now: int) -> AdminState:
        """Read the admins' commands from the Steering issue; none without one."""
        if self.steering is None:
            return AdminState()

        def status(target: int | None) -> str:
            held = self._admin
            if target is None:
                return render_board(self._known, held.paused, held.paused_issues)
            return render_issue(target, self._known.get(target), held.holds(target))

        self._admin = process_steering(self.pm_github, self.mirror, self.steering, config, status)
        if self._admin.blocked is not None:
            self.errors.append(
                f"steering is not trusted, everything is on hold: {self._admin.blocked}"
            )
        return self._admin

    def _load_config(self) -> RunnerConfig:
        text = self.pm_github.read_file(self.base_branch, PROCESS_FILE)
        if text is None:
            raise ConfigError(f"{PROCESS_FILE} not found on {self.base_branch}")
        if self._config is None or self._config[0] != text:
            self._config = (text, load_runner_config(text, self.epoch, self.base_branch))
        return self._config[1]


__all__ = ["NotFound", "Service"]
