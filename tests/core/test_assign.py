"""Assignments and leases (design: "Runners: authorization, assignment, leases, failover")."""

from collections.abc import Mapping
from typing import Any

from concert.core.assign import (
    ASSIGN_GRACE,
    LEASE_SECONDS,
    Heartbeat,
    RunnerInfo,
    backoff_seconds,
    expire_leases,
    plan_assignments,
)
from concert.core.decide import Proposed
from concert.core.derive import IssueState, derive
from concert.core.events import Event, append_event
from concert.core.transitions import Status

VM1 = RunnerInfo("vm-1", caps=frozenset({"docker", "browser"}), max_parallel=2)
LAPTOP = RunnerInfo("laptop", caps=frozenset({"docker"}), max_parallel=1)
NOW = 10_000


def fresh(runner: str, *run_ids: str, age: int = 10) -> Heartbeat:
    return Heartbeat(runner=runner, at=NOW - age, run_ids=frozenset(run_ids))


class Log:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.add("created", {"issue_type": "code"})

    def add(self, kind: str, data: Mapping[str, Any]) -> "Log":
        self.events.append(append_event(self.events, epoch=1, kind=kind, data=data))
        return self

    def move(self, src: str, dst: str, trigger: str) -> "Log":
        return self.add("transition", {"from": src, "to": dst, "trigger": trigger})

    def state(self) -> IssueState:
        return derive([e.to_dict() for e in self.events])

    def apply(self, proposals: list[Proposed]) -> "Log":
        for p in proposals:
            self.add(p.kind, p.data)
        return self


def triage_log() -> Log:
    return Log().move("inbox", "triage", "dispatch")


def assign(log: Log, **kw: Any) -> list[Proposed]:
    args: dict[str, Any] = {
        "issue": 7,
        "runners": [VM1, LAPTOP],
        "heartbeats": {"vm-1": fresh("vm-1"), "laptop": fresh("laptop")},
        "load": {},
        "branch_head": None,
        "now": NOW,
    }
    return plan_assignments(log.state(), **(args | kw))


# --- assigning runs ---------------------------------------------------------------------------


def test_a_state_that_needs_an_agent_gets_one_assignment() -> None:
    [proposal] = assign(triage_log())
    assert proposal.kind == "assignment"
    assert proposal.data == {
        "run_id": "r-7-1",
        "runner": "vm-1",
        "state": "triage",
        "reviewer": None,
        "head": None,
        "issued_at": NOW,
    }


def test_the_assignment_records_the_branch_head_the_run_starts_from() -> None:
    log = Log().move("inbox", "triage", "dispatch").move("triage", "spec", "spec")
    [proposal] = assign(log, branch_head="aaa1111")
    assert proposal.data["head"] == "aaa1111"


def test_run_ids_keep_counting_for_the_issue() -> None:
    log = triage_log()
    log.apply(assign(log))
    log.add("lease_expired", {"run_id": "r-7-1", "state": "triage", "at": NOW})
    [again] = assign(log, now=NOW + 60)
    assert again.data["run_id"] == "r-7-2"


def test_no_second_assignment_while_a_run_is_active() -> None:
    log = triage_log()
    log.apply(assign(log))
    assert assign(log) == []


def test_no_assignment_when_the_state_already_has_an_accepted_run() -> None:
    log = triage_log()
    log.add(
        "assignment",
        {
            "run_id": "r-7-1",
            "runner": "vm-1",
            "state": "triage",
            "reviewer": None,
            "head": None,
            "issued_at": 1,
        },
    )
    log.add(
        "handoff_accepted",
        {"run_id": "r-7-1", "state": "triage", "commit_sha": None, "artifact_rev": None},
    )
    assert assign(log) == []  # it exited blocked_by: the issue waits, it is not re-run


def test_gates_inbox_and_terminal_states_get_no_assignment() -> None:
    assert assign(Log()) == []
    gate = Log()
    for src, dst, trigger in [
        ("inbox", "triage", "dispatch"),
        ("triage", "spec", "spec"),
        ("spec", "spec_approval", "ready_for_approval"),
    ]:
        gate.move(src, dst, trigger)
    assert assign(gate) == []


def test_a_revert_has_no_agent_run_in_its_pm_only_implementing_state() -> None:
    log = Log()
    log.events = []
    log.add("created", {"issue_type": "revert"})
    assert log.state().status is Status.IMPLEMENTING
    assert assign(log) == []


# --- choosing a runner ------------------------------------------------------------------------


def test_a_runner_without_a_fresh_heartbeat_is_not_alive() -> None:
    stale = {"vm-1": fresh("vm-1", age=LEASE_SECONDS + 1), "laptop": fresh("laptop")}
    [proposal] = assign(triage_log(), heartbeats=stale)
    assert proposal.data["runner"] == "laptop"
    assert assign(triage_log(), heartbeats={}) == []


def test_capabilities_must_cover_the_state() -> None:
    log = Log()
    for src, dst, trigger in [
        ("inbox", "triage", "dispatch"),
        ("triage", "spec", "spec"),
        ("spec", "spec_approval", "ready_for_approval"),
        ("spec_approval", "implementing", "approve_spec"),
        ("implementing", "in_review", "done"),
        ("in_review", "uat", "clean"),
    ]:
        log.move(src, dst, trigger)
    [proposal] = assign(log, runners=[LAPTOP, VM1])
    assert proposal.data["runner"] == "vm-1"  # uat needs docker and a browser
    assert assign(log, runners=[LAPTOP]) == []


def test_a_runner_at_max_parallel_gets_nothing_more() -> None:
    [proposal] = assign(triage_log(), load={"vm-1": 2})
    assert proposal.data["runner"] == "laptop"
    assert assign(triage_log(), load={"vm-1": 2, "laptop": 1}) == []


# --- reviewers -------------------------------------------------------------------------------------


def in_review_log() -> Log:
    log = Log()
    for src, dst, trigger in [
        ("inbox", "triage", "dispatch"),
        ("triage", "spec", "spec"),
        ("spec", "spec_approval", "ready_for_approval"),
        ("spec_approval", "implementing", "approve_spec"),
        ("implementing", "in_review", "done"),
    ]:
        log.move(src, dst, trigger)
    return log


def test_review_assigns_one_run_per_reviewer() -> None:
    proposals = assign(in_review_log(), branch_head="bbb2222")
    assert sorted(p.data["reviewer"] for p in proposals) == ["code", "security"]
    assert {p.data["head"] for p in proposals} == {"bbb2222"}
    assert [p.data["run_id"] for p in proposals] == ["r-7-1", "r-7-2"]


def test_review_assigns_only_the_missing_reviewer() -> None:
    log = in_review_log()
    log.add(
        "assignment",
        {
            "run_id": "r-7-1",
            "runner": "vm-1",
            "state": "in_review",
            "reviewer": "code",
            "head": "bbb2222",
            "issued_at": NOW,
        },
    )
    [proposal] = assign(log, branch_head="bbb2222")
    assert proposal.data["reviewer"] == "security" and proposal.data["run_id"] == "r-7-2"


# --- leases -----------------------------------------------------------------------------------------


def with_run(issued_at: int) -> Log:
    log = triage_log()
    log.add(
        "assignment",
        {
            "run_id": "r-7-1",
            "runner": "vm-1",
            "state": "triage",
            "reviewer": None,
            "head": None,
            "issued_at": issued_at,
        },
    )
    return log


def test_a_run_listed_by_a_fresh_heartbeat_keeps_its_lease() -> None:
    log = with_run(issued_at=NOW - 5_000)
    assert expire_leases(log.state(), {"vm-1": fresh("vm-1", "r-7-1")}, NOW) == []


def test_a_stale_heartbeat_expires_the_lease() -> None:
    log = with_run(issued_at=NOW - 5_000)
    stale = {"vm-1": fresh("vm-1", "r-7-1", age=LEASE_SECONDS + 1)}
    [proposal] = expire_leases(log.state(), stale, NOW)
    assert proposal == Proposed("lease_expired", {"run_id": "r-7-1", "state": "triage", "at": NOW})


def test_a_heartbeat_that_does_not_list_the_run_does_not_hold_the_lease() -> None:
    log = with_run(issued_at=NOW - 5_000)
    assert [p.kind for p in expire_leases(log.state(), {"vm-1": fresh("vm-1")}, NOW)] == [
        "lease_expired"
    ]


def test_a_new_assignment_has_a_grace_period_before_the_first_heartbeat() -> None:
    assert expire_leases(with_run(issued_at=NOW - 10).state(), {}, NOW) == []
    late = with_run(issued_at=NOW - ASSIGN_GRACE - 1).state()
    assert [p.kind for p in expire_leases(late, {}, NOW)] == ["lease_expired"]


def test_a_missing_runner_heartbeat_expires_the_lease_after_the_grace() -> None:
    log = with_run(issued_at=NOW - 5_000)
    assert [p.kind for p in expire_leases(log.state(), {}, NOW)] == ["lease_expired"]


def test_the_fourth_infra_failure_blocks_the_issue() -> None:
    log = triage_log()
    for n in range(1, 4):
        log.add(
            "assignment",
            {
                "run_id": f"r-7-{n}",
                "runner": "vm-1",
                "state": "triage",
                "reviewer": None,
                "head": None,
                "issued_at": 1,
            },
        )
        log.add("lease_expired", {"run_id": f"r-7-{n}", "state": "triage", "at": 2})
    log.add(
        "assignment",
        {
            "run_id": "r-7-4",
            "runner": "vm-1",
            "state": "triage",
            "reviewer": None,
            "head": None,
            "issued_at": 1,
        },
    )
    proposals = expire_leases(log.state(), {}, NOW)
    assert [p.kind for p in proposals] == ["lease_expired", "transition"]
    assert proposals[1].data["trigger"] == "budget_exceeded"
    assert log.apply(proposals).state().status is Status.BLOCKED


def test_a_third_infra_failure_is_still_retried() -> None:
    log = triage_log()
    for n in range(1, 3):
        log.add(
            "assignment",
            {
                "run_id": f"r-7-{n}",
                "runner": "vm-1",
                "state": "triage",
                "reviewer": None,
                "head": None,
                "issued_at": 1,
            },
        )
        log.add("lease_expired", {"run_id": f"r-7-{n}", "state": "triage", "at": 2})
    log.add(
        "assignment",
        {
            "run_id": "r-7-3",
            "runner": "vm-1",
            "state": "triage",
            "reviewer": None,
            "head": None,
            "issued_at": 1,
        },
    )
    assert [p.kind for p in expire_leases(log.state(), {}, NOW)] == ["lease_expired"]


def test_a_retry_waits_for_its_backoff() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3, 4, 10)] == [30, 60, 120, 240, 600]
    log = triage_log()
    log.add(
        "assignment",
        {
            "run_id": "r-7-1",
            "runner": "vm-1",
            "state": "triage",
            "reviewer": None,
            "head": None,
            "issued_at": 1,
        },
    )
    log.add("lease_expired", {"run_id": "r-7-1", "state": "triage", "at": NOW - 10})
    assert assign(log) == []  # 10 s after the first failure: wait 30 s
    assert len(assign(log, now=NOW + 30)) == 1
