"""pm-handoff: schema, acceptance and per-state validation (design: "Run result contract")."""

from dataclasses import replace
from typing import Any

import pytest

from concert.core.events import MalformedEvent, UnsupportedSchema
from concert.core.handoff import (
    Accepted,
    Assignment,
    BlockFacts,
    Handoff,
    Ignored,
    Invalid,
    InvalidResultAction,
    RunFacts,
    RunState,
    accept_handoff,
    consolidate_reviews,
    invalid_result_action,
    parse_handoff,
)
from concert.core.transitions import Trigger

SPEC_HASH = "4be77c0"
NEW_SPEC_HASH = "9a1d2e3"
HEAD = "aaa1111"
COMMIT = "9f2c1ab"


def raw_handoff(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": 1,
        "state": "implementing",
        "run_id": "r-0193",
        "runner": "vm-1",
        "exit": "done",
        "commit_sha": COMMIT,
        "artifact_rev": SPEC_HASH,
        "produced": ["backend/exports/api.py"],
        "decisions": ["Paginate exports server-side"],
        "rejected": [],
        "doubts": [],
        "lessons_applied": ["L0012"],
        "lessons_proposed": [],
        "plan_deviations": [],
        "skills_used": [],
        "depends_on": [],
        "run_commits": [COMMIT],
    }
    return base | overrides


def handoff(**overrides: Any) -> Handoff:
    return parse_handoff(raw_handoff(**overrides))


ASSIGNMENTS = {"r-0193": Assignment(run_id="r-0193", runner="vm-1")}
GOOD_BLOCK = BlockFacts(author="vm-1", edited=False, posted_before_lease_expiry=True)
GOOD_FACTS = RunFacts(
    run_start_head=HEAD,
    approved_spec_rev=SPEC_HASH,
    commit_exists_on_remote=True,
    tree_hash_at_commit=SPEC_HASH,
    paths_at_commit=frozenset({"backend/exports/api.py"}),
)


def decide(
    h: Handoff,
    block: BlockFacts = GOOD_BLOCK,
    facts: RunFacts = GOOD_FACTS,
    assignments: dict[str, Assignment] = ASSIGNMENTS,
) -> Accepted | Ignored | Invalid:
    return accept_handoff(h, assignments, block, facts)


# --- schema -----------------------------------------------------------------------------


def test_a_full_handoff_parses() -> None:
    h = handoff(plan_deviations=["Split the view"], reviewer=None)
    assert h.state is RunState.IMPLEMENTING
    assert h.run_id == "r-0193"
    assert h.exit == "done"
    assert h.produced == ("backend/exports/api.py",)
    assert h.plan_deviations == ("Split the view",)
    assert h.run_commits == (COMMIT,)


def test_optional_lists_default_to_empty() -> None:
    minimal = {
        "schema": 1,
        "state": "triage",
        "run_id": "r-1",
        "runner": "vm-1",
        "exit": "spec",
        "commit_sha": None,
        "artifact_rev": None,
    }
    h = parse_handoff(minimal)
    assert h.produced == () and h.depends_on == () and h.run_commits == ()


def test_an_unknown_newer_schema_is_refused() -> None:
    with pytest.raises(UnsupportedSchema):
        parse_handoff(raw_handoff(schema=2))


@pytest.mark.parametrize("field", ["state", "run_id", "runner", "exit"])
def test_a_missing_required_field_is_malformed(field: str) -> None:
    raw = raw_handoff()
    del raw[field]
    with pytest.raises(MalformedEvent):
        parse_handoff(raw)


def test_an_unknown_state_or_a_bad_list_is_malformed() -> None:
    with pytest.raises(MalformedEvent):
        parse_handoff(raw_handoff(state="lunch"))
    with pytest.raises(MalformedEvent):
        parse_handoff(raw_handoff(produced="not-a-list"))


# --- trust rules: late or untrusted blocks are ignored ------------------------------------


def test_a_good_implementing_handoff_is_accepted() -> None:
    assert decide(handoff()) == Accepted(trigger=Trigger.DONE, spec_drift=False)


def test_a_block_for_a_run_with_no_assignment_is_ignored() -> None:
    assert isinstance(decide(handoff(), assignments={}), Ignored)


def test_a_block_by_another_runner_is_ignored() -> None:
    other = replace(GOOD_BLOCK, author="vm-2")
    assert isinstance(decide(handoff(), block=other), Ignored)


def test_an_edited_block_is_ignored() -> None:
    assert isinstance(decide(handoff(), block=replace(GOOD_BLOCK, edited=True)), Ignored)


def test_a_block_posted_after_the_lease_expired_is_ignored() -> None:
    late = replace(GOOD_BLOCK, posted_before_lease_expiry=False)
    assert isinstance(decide(handoff(), block=late), Ignored)


# --- per-state validation -----------------------------------------------------------------


def test_an_exit_not_listed_for_the_state_is_invalid() -> None:
    assert isinstance(decide(handoff(exit="clean")), Invalid)


def test_a_committing_state_needs_a_commit_that_exists_on_the_remote() -> None:
    assert isinstance(decide(handoff(commit_sha=None)), Invalid)
    missing = replace(GOOD_FACTS, commit_exists_on_remote=False)
    assert isinstance(decide(handoff(), facts=missing), Invalid)


def test_a_non_committing_state_must_report_the_head_at_run_start() -> None:
    uat = handoff(state="uat", exit="passed", commit_sha=HEAD)
    assert decide(uat) == Accepted(trigger=Trigger.PASSED, spec_drift=False)
    moved = handoff(state="uat", exit="passed", commit_sha=COMMIT)
    assert isinstance(decide(moved), Invalid)


def test_triage_reports_no_commit_and_no_artifact() -> None:
    ok = handoff(state="triage", exit="spec", commit_sha=None, artifact_rev=None, produced=[])
    assert decide(ok) == Accepted(trigger=Trigger.SPEC, spec_drift=False)
    assert isinstance(decide(replace(ok, commit_sha=HEAD)), Invalid)
    assert isinstance(decide(replace(ok, artifact_rev=SPEC_HASH)), Invalid)


def test_a_spec_run_reports_the_new_spec_hash_that_matches_the_commit_tree() -> None:
    facts = replace(GOOD_FACTS, tree_hash_at_commit=NEW_SPEC_HASH)
    ok = handoff(state="spec", exit="ready_for_approval", artifact_rev=NEW_SPEC_HASH)
    assert decide(ok, facts=facts) == Accepted(trigger=Trigger.READY_FOR_APPROVAL, spec_drift=False)
    lying = handoff(state="spec", exit="ready_for_approval", artifact_rev="deadbee")
    assert isinstance(decide(lying, facts=facts), Invalid)


def test_later_states_must_work_from_the_approved_spec() -> None:
    assert isinstance(decide(handoff(artifact_rev="deadbee")), Invalid)


def test_a_changed_spec_tree_overrides_the_declared_exit_with_spec_drift() -> None:
    drifted = replace(GOOD_FACTS, tree_hash_at_commit=NEW_SPEC_HASH)
    result = decide(handoff(exit="done"), facts=drifted)
    assert result == Accepted(trigger=Trigger.SPEC_DRIFT, spec_drift=True)


def test_declared_produced_paths_must_exist_at_the_commit() -> None:
    gone = replace(GOOD_FACTS, paths_at_commit=frozenset())
    assert isinstance(decide(handoff(), facts=gone), Invalid)


@pytest.mark.parametrize(
    ("state", "exit_name", "trigger"),
    [
        ("triage", "needs_input", Trigger.NEEDS_INPUT),
        ("triage", "duplicate", Trigger.DUPLICATE),
        ("spec", "needs_input", Trigger.NEEDS_INPUT),
        ("implementing", "needs_input", Trigger.NEEDS_INPUT),
        ("in_review", "clean", Trigger.CLEAN),
        ("in_review", "changes_needed", Trigger.CHANGES_NEEDED),
        ("uat", "failed", Trigger.FAILED),
        ("integrating", "ready_to_merge", Trigger.READY_TO_MERGE),
        ("integrating", "rebased", Trigger.REBASED),
        ("verifying_staging", "passed", Trigger.PASSED),
    ],
)
def test_exits_map_to_transition_triggers(state: str, exit_name: str, trigger: Trigger) -> None:
    committing = state in ("integrating",)
    h = handoff(
        state=state,
        exit=exit_name,
        commit_sha=COMMIT if committing else (None if state == "triage" else HEAD),
        artifact_rev=None if state == "triage" else SPEC_HASH,
        produced=[],
    )
    result = decide(h)
    assert isinstance(result, Accepted), result
    assert result.trigger is trigger


@pytest.mark.parametrize(
    ("state", "exit_name"),
    [
        ("triage", "blocked_by"),
        ("implementing", "needs_prerequisite"),
        ("verifying_staging", "failed"),
    ],
)
def test_exits_decided_by_other_rules_are_accepted_without_a_trigger(
    state: str, exit_name: str
) -> None:
    h = handoff(
        state=state,
        exit=exit_name,
        commit_sha=None if state == "triage" else (HEAD if state != "implementing" else COMMIT),
        artifact_rev=None if state == "triage" else SPEC_HASH,
        produced=[],
    )
    result = decide(h)
    assert isinstance(result, Accepted), result
    assert result.trigger is None


# --- invalid results: one retry, then blocked ---------------------------------------------


def test_an_invalid_result_gets_one_infra_retry_then_blocks() -> None:
    assert invalid_result_action(previous_invalid=0) is InvalidResultAction.RETRY
    assert invalid_result_action(previous_invalid=1) is InvalidResultAction.BLOCK


# --- reviewers ----------------------------------------------------------------------------


def test_needs_prerequisite_wins_over_changes_needed_over_clean() -> None:
    assert consolidate_reviews(["clean", "clean"]) == "clean"
    assert consolidate_reviews(["clean", "changes_needed"]) == "changes_needed"
    assert consolidate_reviews(["changes_needed", "needs_prerequisite"]) == "needs_prerequisite"


def test_consolidating_no_reviews_is_an_error() -> None:
    with pytest.raises(ValueError):
        consolidate_reviews([])


# --- rebases -----------------------------------------------------------------------------


def test_rebases_are_recorded_as_an_old_to_new_mapping() -> None:
    h = handoff(rebases={"c1": "c1r", "c2": "c2r"})
    assert dict(h.rebases) == {"c1": "c1r", "c2": "c2r"}
    assert handoff().rebases == {}


@pytest.mark.parametrize("bad", ["c1", ["c1"], {"c1": 2}, {1: "c1r"}])
def test_a_malformed_rebase_mapping_is_rejected(bad: object) -> None:
    with pytest.raises(MalformedEvent):
        parse_handoff(raw_handoff(rebases=bad))


def test_a_review_or_uat_report_from_an_older_head_is_ignored() -> None:
    """After a rebase the issue is reviewed again; a report from a run that started on the old
    head must not stand in for the new round."""
    review = handoff(state="in_review", exit="clean", reviewer="code", commit_sha=HEAD)
    current = replace(GOOD_FACTS, current_head=HEAD)
    assert decide(review, facts=current) == Accepted(trigger=Trigger.CLEAN, spec_drift=False)
    assert isinstance(decide(review, facts=replace(GOOD_FACTS, current_head=COMMIT)), Ignored)
    uat = handoff(state="uat", exit="passed", commit_sha=HEAD)
    assert isinstance(decide(uat, facts=replace(GOOD_FACTS, current_head=COMMIT)), Ignored)
