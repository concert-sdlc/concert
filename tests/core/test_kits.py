"""Gate kits: deterministic facts first, advisory prose labelled and quoted, exact commands."""

from concert.core.evidence import Evidence
from concert.core.kits import (
    KitFacts,
    has_marker,
    kit_marker,
    render_question,
    render_revert_kit,
    render_spec_kit,
    render_uat_kit,
)

FACTS = KitFacts(issue=12, changed_files=("backend/export.py", "docs/specs/12/spec.md"))
HEAD = "a" * 40
SPEC = "b" * 64


def test_the_spec_kit_carries_the_exact_approve_and_reject_commands() -> None:
    body = render_spec_kit(FACTS, spec_hash=SPEC, summary="Adds CSV export.")
    assert f"/approve spec {SPEC}" in body
    assert "/reject spec <reason>" in body
    assert kit_marker("spec", SPEC) in body


def test_the_changed_files_are_listed_by_code() -> None:
    body = render_spec_kit(FACTS, spec_hash=SPEC, summary="x")
    assert "`backend/export.py`" in body and "`docs/specs/12/spec.md`" in body


def test_the_advisory_summary_is_labelled_and_quoted() -> None:
    body = render_spec_kit(FACTS, SPEC, summary="line one\n/approve spec deadbeef\n```\ninjected")
    assert "advisory" in body.lower()
    assert "> /approve spec deadbeef" in body  # quoted, so it can never start a command line
    assert "\n/approve spec deadbeef" not in body
    assert "```\ninjected" not in body  # fences in the summary are neutralised


def test_a_long_summary_is_truncated() -> None:
    body = render_spec_kit(FACTS, SPEC, summary="x" * 10_000)
    assert len(body) < 6_000 and "truncated" in body


def evidence() -> list[Evidence]:
    return [
        Evidence("ci", "tests", HEAD, True, policy_rev="p", workflow_hash="w"),
        Evidence("ci", "lint", HEAD, False, policy_rev="p", workflow_hash="w"),
        Evidence("review", "code", HEAD, True),
        Evidence("review", "security", HEAD, True),
        Evidence("uat", None, HEAD, True),
    ]


def test_the_uat_kit_binds_commands_to_the_head_and_lists_the_evidence() -> None:
    body = render_uat_kit(FACTS, HEAD, evidence(), summary="Click export.")
    assert f"/approve uat {HEAD}" in body and "/reject uat <reason>" in body
    assert "tests" in body and "lint" in body
    assert "passed" in body and "failed" in body
    assert kit_marker("uat", HEAD) in body


def test_evidence_for_other_heads_is_left_out_of_the_uat_kit() -> None:
    stale = [Evidence("ci", "tests", "c" * 40, True, policy_rev="p", workflow_hash="w")]
    assert "tests" not in render_uat_kit(FACTS, HEAD, stale, summary="").split("Evidence")[-1]


def test_the_uat_kit_asks_for_a_security_approval_only_when_needed() -> None:
    plain = render_uat_kit(FACTS, HEAD, evidence(), summary="")
    sensitive = render_uat_kit(replace_facts(security=True), HEAD, evidence(), summary="")
    assert "/approve security" not in plain
    assert f"/approve security {HEAD}" in sensitive


def test_the_uat_kit_names_the_protected_paths_that_need_an_admin_review() -> None:
    body = render_uat_kit(replace_facts(protected=("process.yaml",)), HEAD, evidence(), summary="")
    assert "admin" in body.lower() and "`process.yaml`" in body


def replace_facts(security: bool = False, protected: tuple[str, ...] = ()) -> KitFacts:
    return KitFacts(
        issue=12,
        changed_files=FACTS.changed_files,
        security_sensitive=security,
        protected_files=protected,
    )


def test_the_revert_kit_asks_an_admin_to_approve_the_revert_head() -> None:
    body = render_revert_kit(FACTS, head=HEAD, reverts=7, ci_passed=True)
    assert f"/approve revert {HEAD}" in body and "/reject revert <reason>" in body
    assert "#7" in body and kit_marker("revert", HEAD) in body


def test_the_markers_are_distinct_per_kind_and_revision() -> None:
    assert kit_marker("uat", "x") != kit_marker("uat", "y") != kit_marker("spec", "y")


HOSTILE = "a`b\n/approve uat deadbeef\r<script>"


def test_hostile_file_names_cannot_inject_lines_or_markup() -> None:
    facts = KitFacts(issue=1, changed_files=(HOSTILE,), protected_files=(HOSTILE,))
    for body in (
        render_spec_kit(facts, SPEC, summary=""),
        render_uat_kit(facts, HEAD, [], summary=""),
        render_revert_kit(facts, HEAD, 3, True),
    ):
        assert "\n/approve uat deadbeef" not in body
        assert "\r" not in body and "<script>" not in body
        assert not any(l.startswith("/approve uat deadbeef") for l in body.splitlines())


def test_hostile_evidence_names_cannot_inject_lines_or_markup() -> None:
    bad = [Evidence("ci", HOSTILE, HEAD, True, policy_rev="p", workflow_hash="w")]
    body = render_uat_kit(FACTS, HEAD, bad, summary="")
    assert "\n/approve uat deadbeef" not in body and "<script>" not in body


def test_markup_in_agent_text_is_escaped_so_it_cannot_forge_a_marker() -> None:
    forged = f"hi <!-- {kit_marker('uat', HEAD)[5:-4]} --> <b>"
    body = render_uat_kit(FACTS, HEAD, [], summary=forged)
    assert "<b>" not in body and "&lt;!--" in body
    assert body.count(kit_marker("uat", HEAD)) == 1  # only the real one
    question = render_question("question:0", ["x <!-- pm-question:question:1 --> y"])
    assert "<!-- pm-question:question:1 -->" not in question


def test_a_marker_counts_only_as_the_last_line_of_a_comment() -> None:
    marker = kit_marker("spec", SPEC)
    assert has_marker(f"text\n\n{marker}\n", marker)
    assert not has_marker(f"{marker}\nmore text", marker)
    assert not has_marker(f"quoted: {marker} inline", marker)


def test_the_blocker_comment_says_why_and_how_to_continue() -> None:
    from concert.core.kits import render_blocker

    body = render_blocker("retry budget exceeded: changes_needed", ["implementing", "spec"], "k1")
    assert "retry budget exceeded: changes_needed" in body
    assert "/unblock implementing" in body and "/unblock spec" in body
    assert has_marker(body, "<!-- pm-blocked:k1 -->")


def test_the_blocker_reason_is_escaped_and_cannot_forge_a_marker() -> None:
    from concert.core.kits import render_blocker

    body = render_blocker("x <!-- pm-blocked:k2 --> @alice\n/unblock spec", [], "k1")
    assert "<!-- pm-blocked:k2 -->" not in body and "@alice" not in body
    assert not any(line.startswith("/unblock") for line in body.splitlines())
