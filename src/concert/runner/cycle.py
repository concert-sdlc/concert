"""One pass over the open issues: poll each, then let the head of the queue integrate.

Issues are polled first without permission to integrate; the ones in human_uat with every
merge requirement met become the waiting queue, and only the issue `pick_queue_head` chooses
is polled again with that permission. The queue holds while another merge is being integrated
or verified, and while a failed verification has frozen it; reverts and hotfixes pass both.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from concert.core.derive import IssueState
from concert.core.queue import QueueEntry, pick_queue_head
from concert.core.transitions import Status
from concert.github.api import GitHubApi
from concert.github.mirror import Mirror
from concert.runner.poll import FactsProvider, PollResult, RunnerConfig, poll_issue

_HOLDING = frozenset({Status.INTEGRATING, Status.VERIFYING_STAGING})


@dataclass(frozen=True)
class CycleResult:
    results: Mapping[int, PollResult]
    head: int | None  # the issue allowed to integrate in this cycle
    errors: Mapping[int, str] = field(default_factory=dict[int, str])  # polls that failed

    @property
    def states(self) -> dict[int, IssueState]:
        """Pass these as `known` to the next cycle."""
        return {n: r.state for n, r in self.results.items()}


def run_cycle(
    github: GitHubApi,
    mirror: Mirror,
    issues: Sequence[int],
    config: RunnerConfig,
    facts: FactsProvider,
    now: int = 0,
    known: Mapping[int, IssueState] | None = None,
) -> CycleResult:
    """`known`: the states seen in the previous cycle, so an issue can tell whether the issues
    it waits on have landed (an issue not seen yet counts as unfinished)."""
    results: dict[int, PollResult] = {}
    errors: dict[int, str] = {}
    for n in issues:
        others = {**(known or {}), **{m: r.state for m, r in results.items()}}
        try:
            results[n] = poll_issue(
                github, mirror, n, config, facts, now, at_queue_head=False, others=others
            )
        except Exception as error:  # noqa: BLE001  one issue's failure must not stop the others
            errors[n] = f"{type(error).__name__}: {error}"
    states = {n: r.state for n, r in results.items()}
    busy = any(s.status in _HOLDING for s in states.values())
    frozen = any(s.froze_queue and s.status is Status.BLOCKED for s in states.values())
    waiting = [
        QueueEntry(n, states[n].issue_type, states[n].dispatched_at)
        for n in issues
        if n in results and results[n].ready_for_integration
    ]
    chosen = pick_queue_head(waiting, busy=busy, frozen=frozen)
    if chosen is None:
        return CycleResult(results, None, errors)
    others = {**(known or {}), **states}
    try:
        results[chosen.number] = poll_issue(
            github, mirror, chosen.number, config, facts, now, at_queue_head=True, others=others
        )
    except Exception as error:  # noqa: BLE001
        errors[chosen.number] = f"{type(error).__name__}: {error}"
    return CycleResult(results, chosen.number, errors)
