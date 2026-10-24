"""Retry budgets (design: "Retry budgets (exact semantics)")."""

from concert.core.counters import AgentState, Cause, Outcome, RetryCounters, charge_target


def test_review_uat_and_spec_drift_charge_implementing() -> None:
    for cause in (Cause.CHANGES_NEEDED, Cause.UAT_FAILED, Cause.REJECT_UAT, Cause.SPEC_DRIFT):
        assert charge_target(cause) is AgentState.IMPLEMENTING


def test_reject_spec_charges_spec() -> None:
    assert charge_target(Cause.REJECT_SPEC) is AgentState.SPEC


def test_budget_four_allows_four_reentries_and_blocks_the_fifth() -> None:
    counters = RetryCounters(budget=4)
    for _ in range(4):
        counters, outcome = counters.charge(Cause.CHANGES_NEEDED)
        assert outcome is Outcome.CHARGED
    # five runs of implementing have now started; the would-be 5th re-entry blocks
    counters, outcome = counters.charge(Cause.CHANGES_NEEDED)
    assert outcome is Outcome.BLOCKED
    assert counters.used(AgentState.IMPLEMENTING) == 4


def test_counters_are_per_state() -> None:
    counters = RetryCounters(budget=1)
    counters, _ = counters.charge(Cause.CHANGES_NEEDED)
    counters, outcome = counters.charge(Cause.REJECT_SPEC)
    assert outcome is Outcome.CHARGED
    assert counters.used(AgentState.SPEC) == 1
    assert counters.used(AgentState.IMPLEMENTING) == 1


def test_reset_clears_only_the_named_state() -> None:
    counters = RetryCounters(budget=2)
    counters, _ = counters.charge(Cause.CHANGES_NEEDED)
    counters, _ = counters.charge(Cause.REJECT_SPEC)
    counters = counters.reset(AgentState.IMPLEMENTING)
    assert counters.used(AgentState.IMPLEMENTING) == 0
    assert counters.used(AgentState.SPEC) == 1


def test_spec_correction_prompted_by_review_costs_two_reentries() -> None:
    counters = RetryCounters(budget=4)
    counters, _ = counters.charge(Cause.CHANGES_NEEDED)  # conformance finding
    counters, _ = counters.charge(Cause.SPEC_DRIFT)  # the drift that answers it
    # approval of the corrected spec is not a further charge: nothing to call
    assert counters.used(AgentState.IMPLEMENTING) == 2
