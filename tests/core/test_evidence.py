"""The merge gate: every piece of evidence must be bound to the head SHA (design: Evidence ledger)."""

from dataclasses import replace

from concert.core.evidence import Evidence, GatePolicy, Ready, Waiting, evaluate_gate
from concert.core.transitions import IssueType

HEAD = "h2"
OLD = "h1"
ADMIN, APPROVER, REMOVED = 1, 2, 9

POLICY = GatePolicy(
    policy_rev="p1",
    dod_items=frozenset({"tests", "lint"}),
    allowed_workflows=frozenset({"w1"}),
    approvers=frozenset({APPROVER}),
    admins=frozenset({ADMIN}),
    security_sensitive=False,
    protected_changed=False,
    issue_type=IssueType.CODE,
)


def ci(
    item: str, sha: str = HEAD, passed: bool = True, workflow: str = "w1", policy: str = "p1"
) -> Evidence:
    return Evidence("ci", item, sha, passed, policy_rev=policy, workflow_hash=workflow)


def review(who: str, sha: str = HEAD, passed: bool = True) -> Evidence:
    return Evidence("review", who, sha, passed)


def uat(sha: str = HEAD, passed: bool = True) -> Evidence:
    return Evidence("uat", None, sha, passed)


def approval(kind: str, actor: int, sha: str = HEAD) -> Evidence:
    return Evidence("approval", kind, sha, True, actor_id=actor)


def green(sha: str = HEAD) -> list[Evidence]:
    return [
        ci("tests", sha),
        ci("lint", sha),
        review("code", sha),
        review("security", sha),
        uat(sha),
        approval("uat", APPROVER, sha),
    ]


def waiting(verdict: object) -> Waiting:
    assert isinstance(verdict, Waiting), verdict
    return verdict


def test_everything_green_at_the_head_is_ready() -> None:
    assert evaluate_gate(HEAD, green(), POLICY) == Ready()


def test_a_missing_dod_item_waits() -> None:
    evidence = [e for e in green() if e.item != "lint"]
    assert waiting(evaluate_gate(HEAD, evidence, POLICY)).missing == ("ci:lint",)


def test_a_failing_dod_item_is_failing_not_missing() -> None:
    evidence = [*green(), ci("tests", passed=False)]  # the latest result wins
    verdict = waiting(evaluate_gate(HEAD, evidence, POLICY))
    assert verdict.failing == ("ci:tests",) and verdict.missing == ()


def test_a_later_pass_replaces_an_earlier_failure() -> None:
    evidence = [ci("tests", passed=False), *green()]
    assert evaluate_gate(HEAD, evidence, POLICY) == Ready()


def test_evidence_bound_to_an_older_head_is_stale_and_counts_as_missing() -> None:
    evidence = [ci("tests", OLD), *[e for e in green() if e.item != "tests"]]
    verdict = waiting(evaluate_gate(HEAD, evidence, POLICY))
    assert verdict.stale == ("ci:tests",) and verdict.missing == ("ci:tests",)


def test_ci_from_a_workflow_that_is_not_allowed_is_stale() -> None:
    evidence = [ci("tests", workflow="w-old"), *[e for e in green() if e.item != "tests"]]
    assert waiting(evaluate_gate(HEAD, evidence, POLICY)).stale == ("ci:tests",)


def test_ci_run_under_an_older_policy_is_stale() -> None:
    evidence = [ci("lint", policy="p0"), *[e for e in green() if e.item != "lint"]]
    assert waiting(evaluate_gate(HEAD, evidence, POLICY)).stale == ("ci:lint",)


def test_both_reviewers_and_the_automated_uat_must_have_passed() -> None:
    assert waiting(
        evaluate_gate(HEAD, [e for e in green() if e.item != "security"], POLICY)
    ).missing == ("review:security",)
    assert waiting(
        evaluate_gate(HEAD, [e for e in green() if e.kind != "uat"], POLICY)
    ).missing == ("uat",)
    failed = [*green(), review("code", passed=False)]
    assert waiting(evaluate_gate(HEAD, failed, POLICY)).failing == ("review:code",)


def test_the_human_uat_approval_is_required_and_bound_to_the_head() -> None:
    no_approval = [e for e in green() if e.kind != "approval"]
    assert waiting(evaluate_gate(HEAD, no_approval, POLICY)).missing == ("approval:uat",)
    stale_approval = [*no_approval, approval("uat", APPROVER, OLD)]
    assert waiting(evaluate_gate(HEAD, stale_approval, POLICY)).stale == ("approval:uat",)


def test_an_approval_by_someone_no_longer_an_approver_is_void() -> None:
    evidence = [*[e for e in green() if e.kind != "approval"], approval("uat", REMOVED)]
    verdict = waiting(evaluate_gate(HEAD, evidence, POLICY))
    assert verdict.missing == ("approval:uat",)


def test_an_admin_can_give_the_uat_approval() -> None:
    evidence = [*[e for e in green() if e.kind != "approval"], approval("uat", ADMIN)]
    assert evaluate_gate(HEAD, evidence, POLICY) == Ready()


def test_a_security_sensitive_change_needs_the_security_approval() -> None:
    policy = replace(POLICY, security_sensitive=True)
    assert waiting(evaluate_gate(HEAD, green(), policy)).missing == ("approval:security",)
    assert evaluate_gate(HEAD, [*green(), approval("security", APPROVER)], policy) == Ready()


def test_a_protected_path_change_needs_an_admins_pr_review() -> None:
    policy = replace(POLICY, protected_changed=True)
    assert waiting(evaluate_gate(HEAD, green(), policy)).missing == ("admin_review",)
    by_approver = [*green(), Evidence("admin_review", None, HEAD, True, actor_id=APPROVER)]
    assert waiting(evaluate_gate(HEAD, by_approver, policy)).missing == ("admin_review",)
    by_admin = [*green(), Evidence("admin_review", None, HEAD, True, actor_id=ADMIN)]
    assert evaluate_gate(HEAD, by_admin, policy) == Ready()


def test_a_revert_needs_the_admins_approval_and_its_tests_but_no_review_or_uat() -> None:
    policy = replace(
        POLICY, issue_type=IssueType.REVERT, dod_items=frozenset({"tests"}), require_reviews=False
    )
    evidence = [ci("tests"), approval("revert", ADMIN)]
    assert evaluate_gate(HEAD, evidence, policy) == Ready()
    by_approver = [ci("tests"), approval("revert", APPROVER)]
    assert waiting(evaluate_gate(HEAD, by_approver, policy)).missing == ("approval:revert",)


def test_everything_that_is_wrong_is_reported_at_once() -> None:
    verdict = waiting(evaluate_gate(HEAD, [ci("tests", OLD), uat(passed=False)], POLICY))
    assert set(verdict.missing) >= {
        "ci:tests",
        "ci:lint",
        "review:code",
        "review:security",
        "approval:uat",
    }
    assert verdict.failing == ("uat",)
