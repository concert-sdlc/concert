"""Retry budgets: which agent state a process return is charged to."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class AgentState(Enum):
    SPEC = "spec"
    IMPLEMENTING = "implementing"


class Cause(Enum):
    """A process return that sends an issue back to an agent state."""

    CHANGES_NEEDED = "changes_needed"
    UAT_FAILED = "uat_failed"
    REJECT_UAT = "reject_uat"
    REJECT_SPEC = "reject_spec"
    SPEC_DRIFT = "spec_drift"


_CHARGE_TARGET: dict[Cause, AgentState] = {
    Cause.REJECT_SPEC: AgentState.SPEC,
    Cause.CHANGES_NEEDED: AgentState.IMPLEMENTING,
    Cause.UAT_FAILED: AgentState.IMPLEMENTING,
    Cause.REJECT_UAT: AgentState.IMPLEMENTING,
    Cause.SPEC_DRIFT: AgentState.IMPLEMENTING,
}


def charge_target(cause: Cause) -> AgentState:
    """The agent state whose counter a return of this cause is charged to."""
    return _CHARGE_TARGET[cause]


class Outcome(Enum):
    CHARGED = "charged"
    BLOCKED = "blocked"  # the would-be re-entry exceeds the budget


@dataclass(frozen=True)
class RetryCounters:
    """Per-state re-entry counters. `budget` = re-entries allowed, so runs = budget + 1.

    Staging rebases, pre-gate refreshes and infra retries are never charged here.
    A spec correction prompted by review is two charges (CHANGES_NEEDED then
    SPEC_DRIFT); approving the corrected spec is not a charge.
    """

    budget: int
    _used: Mapping[AgentState, int] = field(default_factory=lambda: MappingProxyType({}))

    def used(self, state: AgentState) -> int:
        return self._used.get(state, 0)

    def charge(self, cause: Cause) -> tuple["RetryCounters", Outcome]:
        state = charge_target(cause)
        if self.used(state) >= self.budget:
            return self, Outcome.BLOCKED
        return self._with(state, self.used(state) + 1), Outcome.CHARGED

    def reset(self, state: AgentState) -> "RetryCounters":
        """`/unblock <state>` and `/pm reset #N <state>` reset the counter of the state they name."""
        return self._with(state, 0)

    def _with(self, state: AgentState, count: int) -> "RetryCounters":
        return RetryCounters(self.budget, MappingProxyType({**self._used, state: count}))
