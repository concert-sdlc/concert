"""The docs issue type: no automated UAT, and only while the diff really is documentation."""

from dataclasses import replace

import pytest

from concert.core.classify import docs_violations
from concert.core.commands import (
    Actor,
    CommentFacts,
    IssueContext,
    Role,
    parse_command,
    validate_command,
)
from concert.core.derive import IssueState
from concert.core.evidence import Evidence, Ready, evaluate_gate
from concert.core.transitions import InvalidTransition, IssueType, Status, Trigger, next_status
from concert.runner.worker import AgentResult
from tests.runner.helpers import World
from tests.runner.test_evidence import POLICY
from tests.runner.test_journey import Project

DOCS = ("docs/**", "*.md")
PROTECTED = ("AGENTS.md", "docs/decisions/**")


def docs_project(changes: dict[str, str | None]) -> Project:
    project = Project()
    project.agent.results["implementing"] = AgentResult(exit="done", changes=changes)
    return project


def to_review(project: Project) -> None:
    project.step()
    project.human("/dispatch docs")
    project.run_until(Status.SPEC_APPROVAL)
    project.human(f"/approve spec {project.world.state(project.issue).spec_rev}")
    project.run_until(Status.IN_REVIEW)


def test_dispatch_docs_types_the_issue_and_starts_triage() -> None:
    project = Project()
    project.step()
    project.human("/dispatch docs")
    project.step()
    state = project.world.state(project.issue)
    assert state.issue_type is IssueType.DOCS and state.status is Status.TRIAGE


def test_a_docs_issue_skips_automated_uat_and_lands() -> None:
    project = docs_project({"docs/guide.md": "# guide"})
    to_review(project)
    project.run_until(Status.HUMAN_UAT)  # review was clean: no UAT state in between
    world, issue = project.world, project.issue
    assert world.state(issue).issue_type is IssueType.DOCS
    assert not any(e.kind == "uat" for e in world.state(issue).evidence)
    head = world.state(issue).head_sha or ""
    world.fake.add_check_run(head, "tests", "success", "w1")
    project.step()
    project.human(f"/approve uat {head}")
    project.run_until(Status.ON_STAGING)


def test_a_docs_issue_that_changes_code_is_reclassified_and_gets_full_gates() -> None:
    project = docs_project({"docs/guide.md": "# guide", "backend/export.py": "x = 1"})
    to_review(project)
    project.step()
    assert project.world.state(project.issue).issue_type is IssueType.CODE
    logged = [
        line["event"]
        for line in project.world.mirror.read(project.issue)
        if line.get("line") == "event" and line["event"]["kind"] == "reclassified"
    ]
    assert len(logged) == 1 and "backend/export.py" in logged[0]["data"]["reason"]
    project.run_until(Status.UAT)  # a code issue goes through automated UAT


def test_editing_a_file_agents_read_reclassifies_too() -> None:
    project = docs_project({"AGENTS.md": "new rules"})
    project.world.config = replace(project.world.config, protected_paths=("AGENTS.md",))
    to_review(project)
    project.step()
    assert project.world.state(project.issue).issue_type is IssueType.CODE


def test_the_classification_is_decided_on_the_paths_alone() -> None:
    assert docs_violations(["docs/a.md", "README.md", "docs/img/x.txt"], DOCS, PROTECTED) == []
    assert docs_violations(["docs/a.md", "src/x.py"], DOCS, PROTECTED) == ["src/x.py"]
    assert docs_violations(["AGENTS.md", "docs/decisions/0001.md"], DOCS, PROTECTED) == [
        "AGENTS.md",
        "docs/decisions/0001.md",
    ]


def test_the_docs_table_goes_from_review_straight_to_the_human_gate() -> None:
    assert next_status(Status.IN_REVIEW, Trigger.CLEAN, IssueType.DOCS) is Status.HUMAN_UAT
    with pytest.raises(InvalidTransition):
        next_status(Status.UAT, Trigger.PASSED, IssueType.DOCS)
    assert next_status(Status.IN_REVIEW, Trigger.CLEAN, IssueType.CODE) is Status.UAT


def test_the_gate_does_not_ask_a_docs_issue_for_automated_uat() -> None:
    evidence = [
        Evidence("ci", "tests", "h", True, POLICY.policy_rev, "w1"),
        Evidence("review", "code", "h", True),
        Evidence("review", "security", "h", True),
        Evidence("approval", "uat", "h", True, actor_id=min(POLICY.approvers)),
    ]
    docs = replace(POLICY, issue_type=IssueType.DOCS)
    assert isinstance(evaluate_gate("h", evidence, docs), Ready)
    assert not isinstance(evaluate_gate("h", evidence, POLICY), Ready)  # code needs UAT


def test_only_an_approver_can_dispatch_docs() -> None:
    command = parse_command("/dispatch docs")
    assert command is not None and (command.verb, command.kind) == ("dispatch", "docs")
    context = IssueContext(
        Status.INBOX, IssueType.CODE, None, None, None, None, frozenset(), frozenset()
    )
    approver = Actor(1, "a", False, Role.APPROVER)
    assert (
        validate_command(command, approver, CommentFacts(False), context).__class__.__name__
        == "Valid"
    )
    contributor = Actor(2, "c", False, Role.CONTRIBUTOR)
    assert (
        validate_command(command, contributor, CommentFacts(False), context).__class__.__name__
        == "Rejected"
    )


def test_only_a_docs_issue_can_be_reclassified() -> None:
    from concert.core.derive import derive
    from concert.core.events import Event, append_event

    events: list[Event] = []
    for kind, data in (
        ("created", {"issue_type": "code"}),
        ("reclassified", {"from": "docs", "to": "code", "reason": "x"}),
    ):
        events.append(append_event(events, epoch=1, kind=kind, data=data))
    state = derive([e.to_dict() for e in events])
    assert state.status is Status.BLOCKED and "only a docs issue" in (state.blocked_reason or "")
    assert IssueState().issue_type is IssueType.CODE and World is not None
