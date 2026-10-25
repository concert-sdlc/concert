"""Assignments and leases.

Pure. The PM is the single assigner: workers never self-claim. A lease is the assignment
event plus a fresh heartbeat that lists the run (under 3 minutes old); a new assignment has
a grace period for the first heartbeat. An expired lease is an infra failure: the run is
reassigned after a backoff, up to 3 retries per state, then the issue is blocked.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from concert.core.decide import Proposed
from concert.core.derive import IssueState
from concert.core.transitions import IssueType, Status, Trigger, is_terminal, next_status

LEASE_SECONDS = 180
ASSIGN_GRACE = 180
INFRA_RETRIES = 3
REVIEWERS = ("code", "security")

_AGENT_STATES = frozenset(
    {
        Status.TRIAGE,
        Status.SPEC,
        Status.IMPLEMENTING,
        Status.IN_REVIEW,
        Status.UAT,
        Status.INTEGRATING,
        Status.VERIFYING_STAGING,
    }
)
_NEEDS: dict[Status, frozenset[str]] = {
    Status.UAT: frozenset({"docker", "browser"}),
    Status.VERIFYING_STAGING: frozenset({"docker", "browser"}),
}


@dataclass(frozen=True)
class RunnerInfo:
    name: str
    caps: frozenset[str]
    max_parallel: int


@dataclass(frozen=True)
class Heartbeat:
    runner: str
    at: int
    run_ids: frozenset[str]
    paused: Mapping[str, int] = field(default_factory=dict[str, int])  # run_id -> resumes at


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff between infra retries: 30 s, 60 s, 120 s, ... capped at 10 min."""
    return min(30 * 2 ** (attempt - 1), 600)


def _fresh(heartbeat: Heartbeat | None, now: int) -> bool:
    return heartbeat is not None and now - heartbeat.at < LEASE_SECONDS


def _inactive(state: IssueState) -> bool:
    return state.blocked_reason is not None or is_terminal(state.status)


def expire_leases(
    state: IssueState, heartbeats: Mapping[str, Heartbeat], now: int
) -> list[Proposed]:
    """Expire runs whose lease lapsed. The fourth infra failure in a state blocks the issue."""
    if _inactive(state):
        return []
    proposals: list[Proposed] = []
    attempts = dict(state.infra_attempts)
    for run in state.active_runs:
        heartbeat = heartbeats.get(run.runner)
        held = _fresh(heartbeat, now) and heartbeat is not None and run.run_id in heartbeat.run_ids
        if held or now - run.issued_at < ASSIGN_GRACE:
            continue
        proposals.append(
            Proposed("lease_expired", {"run_id": run.run_id, "state": run.state, "at": now})
        )
        attempts[run.state] = attempts.get(run.state, 0) + 1
        if attempts[run.state] > INFRA_RETRIES:
            target = next_status(state.status, Trigger.BUDGET_EXCEEDED, state.issue_type)
            proposals.append(
                Proposed(
                    "transition",
                    {
                        "from": state.status.value,
                        "to": target.value,
                        "trigger": Trigger.BUDGET_EXCEEDED.value,
                    },
                )
            )
            break
    return proposals


def plan_assignments(
    state: IssueState,
    issue: int,
    runners: Sequence[RunnerInfo],
    heartbeats: Mapping[str, Heartbeat],
    load: Mapping[str, int],
    branch_head: str | None,
    now: int,
) -> list[Proposed]:
    """Assign a run for every agent slot of the current state that has none."""
    status = state.status
    if _inactive(state) or status not in _AGENT_STATES:
        return []
    if status is Status.IMPLEMENTING and state.issue_type is IssueType.REVERT:
        return []  # a revert's implementing state is PM-only: no agent runs there
    attempts = state.infra_attempts.get(status.value, 0)
    if (
        attempts
        and state.last_expiry_at is not None
        and now < state.last_expiry_at + backoff_seconds(attempts)
    ):
        return []

    slots = _open_slots(state)
    proposals: list[Proposed] = []
    taken = dict(load)
    for offset, reviewer in enumerate(slots):
        runner = _choose(runners, heartbeats, taken, status, now)
        if runner is None:
            break
        taken[runner.name] = taken.get(runner.name, 0) + 1
        proposals.append(
            Proposed(
                "assignment",
                {
                    "run_id": f"r-{issue}-{state.assignments_made + offset + 1}",
                    "runner": runner.name,
                    "state": status.value,
                    "reviewer": reviewer,
                    "head": branch_head,
                    "issued_at": now,
                },
            )
        )
    return proposals


def _open_slots(state: IssueState) -> list[str | None]:
    """The agent runs this state still needs: one, or one per reviewer that has neither a
    run in flight nor a settled result."""
    here = [run for run in state.active_runs if run.state == state.status.value]
    if state.status is Status.IN_REVIEW:
        busy = {run.reviewer for run in here} | set(state.settled_reviewers)
        return [reviewer for reviewer in REVIEWERS if reviewer not in busy]
    if here or state.settled_runs:
        return []  # a run is in flight, or it finished and the issue is waiting
    return [None]


def _choose(
    runners: Sequence[RunnerInfo],
    heartbeats: Mapping[str, Heartbeat],
    taken: Mapping[str, int],
    status: Status,
    now: int,
) -> RunnerInfo | None:
    needs = _NEEDS.get(status, frozenset())
    for runner in runners:
        if (
            _fresh(heartbeats.get(runner.name), now)
            and needs <= runner.caps
            and taken.get(runner.name, 0) < runner.max_parallel
        ):
            return runner
    return None
