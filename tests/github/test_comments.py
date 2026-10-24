"""Rendering and extracting pm-event, pm-handoff and command comments."""

import os
from typing import Any

import pytest

from concert.core.commands import Command, Role
from concert.core.events import Event, append_event
from concert.github.api import Identity
from concert.github.comments import (
    assignments_from,
    extract_commands,
    extract_events,
    extract_handoffs,
    lease_expiry_comment_ids,
    render_event_comment,
    render_handoff_comment,
)
from tests.fakes.github import FakeGitHub

PM = Identity(user_id=900, login="concert-pm[bot]", is_bot=True)
VM1 = Identity(user_id=346900002, login="concert-vm1[bot]", is_bot=True)
VM2 = Identity(user_id=346900003, login="concert-vm2[bot]", is_bot=True)
ALICE = Identity(user_id=1, login="alice", is_bot=False)
MALLORY = Identity(user_id=666, login="mallory", is_bot=False)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


def chain(*specs: tuple[str, dict[str, Any]], epoch: int = 1) -> list[Event]:
    events: list[Event] = []
    for kind, data in specs:
        events.append(append_event(events, epoch=epoch, kind=kind, data=data))
    return events


# --- pm-events ---------------------------------------------------------------------------------


def test_an_event_comment_round_trips(fake: FakeGitHub) -> None:
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    events = chain(
        ("created", {"issue_type": "code"}),
        ("transition", {"from": "inbox", "to": "triage", "trigger": "dispatch"}),
    )
    pm.add_comment(issue, render_event_comment(events))
    result = extract_events(pm.list_comments(issue), pm=PM)
    assert [r.event for r in result.records] == [e.to_dict() for e in events]
    assert result.ignored == [] and result.problems == []


def test_events_coalesced_in_one_comment_keep_their_order_across_comments(fake: FakeGitHub) -> None:
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    events = chain(
        ("created", {"issue_type": "code"}),
        ("transition", {"from": "inbox", "to": "triage", "trigger": "dispatch"}),
        ("counter_reset", {"state": "implementing"}),
    )
    pm.add_comment(issue, render_event_comment(events[:2]))
    pm.add_comment(issue, render_event_comment(events[2:]))
    result = extract_events(pm.list_comments(issue), pm=PM)
    assert [r.event["seq"] for r in result.records] == [1, 2, 3]
    assert [r.comment_id for r in result.records] == [1, 1, 2]


def test_a_comment_by_anyone_but_the_pm_app_is_ignored_even_if_it_looks_like_an_event(
    fake: FakeGitHub,
) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    forged = render_event_comment(chain(("created", {"issue_type": "code"})))
    fake.client(MALLORY).add_comment(issue, forged)
    result = extract_events(fake.client(PM).list_comments(issue), pm=PM)
    assert result.records == []
    assert [i.reason for i in result.ignored] == ["not authored by the PM App"]


def test_an_edited_pm_comment_does_not_count(fake: FakeGitHub) -> None:
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    comment = pm.add_comment(
        issue, render_event_comment(chain(("created", {"issue_type": "code"})))
    )
    fake.tick(5)
    pm.edit_comment(comment.id, render_event_comment(chain(("created", {"issue_type": "revert"}))))
    result = extract_events(pm.list_comments(issue), pm=PM)
    assert result.records == []
    assert [i.reason for i in result.ignored] == ["edited"]


def test_a_pm_comment_with_unreadable_yaml_is_a_problem_not_silently_skipped(
    fake: FakeGitHub,
) -> None:
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    pm.add_comment(issue, "```pm-event\n: : not yaml [\n```")
    result = extract_events(pm.list_comments(issue), pm=PM)
    assert result.records == []
    assert len(result.problems) == 1 and "comment 1" in result.problems[0]


def test_ordinary_pm_comments_without_event_blocks_are_not_a_problem(fake: FakeGitHub) -> None:
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    pm.add_comment(issue, "Triage summary: this looks like a feature.")
    result = extract_events(pm.list_comments(issue), pm=PM)
    assert result.records == [] and result.problems == [] and result.ignored == []


def test_yaml_is_loaded_safely_and_never_executes_anything(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONCERT_PWNED", raising=False)
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    payload = "!!python/object/apply:builtins.exec [\"import os; os.environ['CONCERT_PWNED']='1'\"]"
    pm.add_comment(issue, f"```pm-event\n{payload}\n```")
    result = extract_events(pm.list_comments(issue), pm=PM)
    assert result.records == [] and len(result.problems) == 1
    assert "CONCERT_PWNED" not in os.environ  # the tag was refused, not executed


# --- assignments and lease expiry ---------------------------------------------------------------


def test_assignments_and_lease_expiries_come_from_trusted_events(fake: FakeGitHub) -> None:
    pm = fake.client(PM)
    issue = pm.create_issue("a", "").number
    events = chain(
        ("created", {"issue_type": "code"}),
        ("assignment", {"run_id": "r-1", "runner": "vm-1"}),
        ("lease_expired", {"run_id": "r-1"}),
    )
    pm.add_comment(issue, render_event_comment(events[:2]))
    pm.add_comment(issue, render_event_comment(events[2:]))
    result = extract_events(pm.list_comments(issue), pm=PM)
    assignments = assignments_from(result.records)
    assert assignments["r-1"].runner == "vm-1"
    assert lease_expiry_comment_ids(result.records) == {"r-1": 2}


# --- pm-handoff ------------------------------------------------------------------------------------

RUNNER_IDS = {"vm-1": VM1.user_id, "vm-2": VM2.user_id}


def handoff_raw(run_id: str = "r-1", runner: str = "vm-1") -> dict[str, Any]:
    return {
        "schema": 1,
        "state": "triage",
        "run_id": run_id,
        "runner": runner,
        "exit": "spec",
        "commit_sha": None,
        "artifact_rev": None,
    }


def test_a_handoff_comment_yields_the_handoff_and_facts_about_its_block(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    fake.client(VM1).add_comment(issue, render_handoff_comment(handoff_raw()))
    result = extract_handoffs(fake.client(PM).list_comments(issue), RUNNER_IDS, lease_expiry={})
    assert result.problems == []
    [item] = result.items
    assert item.handoff.run_id == "r-1"
    assert item.block.author == "vm-1"
    assert item.block.edited is False
    assert item.block.posted_before_lease_expiry is True


def test_a_handoff_posted_after_the_lease_expired_is_flagged_late(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    fake.client(VM1).add_comment(issue, render_handoff_comment(handoff_raw()))
    result = extract_handoffs(
        fake.client(PM).list_comments(issue), RUNNER_IDS, lease_expiry={"r-1": 1}
    )
    assert result.items[0].block.posted_before_lease_expiry is False


def test_the_author_is_resolved_by_user_id_not_by_name(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    claims_to_be_vm1 = handoff_raw(runner="vm-1")
    fake.client(VM2).add_comment(issue, render_handoff_comment(claims_to_be_vm1))
    fake.client(MALLORY).add_comment(issue, render_handoff_comment(claims_to_be_vm1))
    result = extract_handoffs(fake.client(PM).list_comments(issue), RUNNER_IDS, lease_expiry={})
    assert [i.block.author for i in result.items] == ["vm-2", "user:666"]


def test_an_edited_handoff_is_flagged_edited(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    vm1 = fake.client(VM1)
    comment = vm1.add_comment(issue, render_handoff_comment(handoff_raw()))
    fake.tick(3)
    vm1.edit_comment(comment.id, render_handoff_comment(handoff_raw()))
    result = extract_handoffs(fake.client(PM).list_comments(issue), RUNNER_IDS, lease_expiry={})
    assert result.items[0].block.edited is True


def test_a_handoff_whose_marker_does_not_match_its_run_is_a_problem(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    body = render_handoff_comment(handoff_raw(run_id="r-1")).replace("pm-run:r-1", "pm-run:r-9")
    fake.client(VM1).add_comment(issue, body)
    result = extract_handoffs(fake.client(PM).list_comments(issue), RUNNER_IDS, lease_expiry={})
    assert result.items == [] and len(result.problems) == 1


def test_a_handoff_with_an_unknown_schema_is_a_problem(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    fake.client(VM1).add_comment(issue, render_handoff_comment(handoff_raw() | {"schema": 9}))
    result = extract_handoffs(fake.client(PM).list_comments(issue), RUNNER_IDS, lease_expiry={})
    assert result.items == [] and "runner too old" in result.problems[0]


# --- commands ---------------------------------------------------------------------------------------


def test_commands_are_extracted_with_the_author_role_by_user_id(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    fake.client(ALICE).add_comment(issue, "/dispatch")
    fake.client(MALLORY).add_comment(issue, "/dispatch")
    fake.client(ALICE).add_comment(issue, "thanks, looks good")
    roles = {ALICE.user_id: Role.APPROVER}
    found = extract_commands(fake.client(PM).list_comments(issue), roles)
    assert [(c.comment_id, c.command, c.actor.role) for c in found] == [
        (1, Command("dispatch", None, ""), Role.APPROVER),
        (2, Command("dispatch", None, ""), Role.NONE),
    ]


def test_a_user_with_an_approvers_login_but_another_id_has_no_role(fake: FakeGitHub) -> None:
    impostor = Identity(
        user_id=777, login="alice", is_bot=False
    )  # a renamed or re-registered login
    issue = fake.client(PM).create_issue("a", "").number
    fake.client(impostor).add_comment(issue, "/dispatch")
    [found] = extract_commands(fake.client(PM).list_comments(issue), {ALICE.user_id: Role.APPROVER})
    assert found.actor.role is Role.NONE


def test_a_command_edit_is_visible_to_validation(fake: FakeGitHub) -> None:
    issue = fake.client(PM).create_issue("a", "").number
    alice = fake.client(ALICE)
    comment = alice.add_comment(issue, "/dispatch")
    fake.tick(1)
    alice.edit_comment(comment.id, "/dispatch")
    [found] = extract_commands(fake.client(PM).list_comments(issue), {ALICE.user_id: Role.APPROVER})
    assert found.facts.edited is True
