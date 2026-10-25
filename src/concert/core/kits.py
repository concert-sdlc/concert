"""Gate kits: what a human reads before approving.

Pure rendering. Code attaches the deterministic facts (changed files, evidence, which extra
approvals are needed); any LLM-written summary is labelled advisory and quoted line by line, so
text from an untrusted source can never form a command line or break out of the comment's
structure. Each kit ends with the exact commands to copy, and carries a marker the PM uses to
find a kit it already posted.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from concert.core.evidence import Evidence

_SUMMARY_LIMIT = 3_000


@dataclass(frozen=True)
class KitFacts:
    issue: int
    changed_files: Sequence[str]
    security_sensitive: bool = False  # the change also needs `/approve security`
    protected_files: Sequence[str] = ()  # these need an admin's approving PR review


def kit_marker(kind: str, revision: str) -> str:
    return f"<!-- pm-kit:{kind}:{revision} -->"


def has_marker(body: str, marker: str) -> bool:
    """A comment carries a marker only as its last line, where code puts it. Text quoted from
    an agent can never end up there (and is escaped besides)."""
    lines = body.rstrip().splitlines()
    return bool(lines) and lines[-1].strip() == marker


def render_spec_kit(facts: KitFacts, spec_hash: str, summary: str) -> str:
    return "\n".join(
        [
            f"## Spec ready for approval: #{facts.issue}",
            "",
            *_files(facts),
            *_advisory(summary),
            "### Decide",
            "",
            "```",
            f"/approve spec {spec_hash}",
            "/reject spec <reason>",
            "```",
            "",
            kit_marker("spec", spec_hash),
        ]
    )


def render_workflow_kit(
    issue: int, parent: int | None, diffs: Sequence[tuple[str, str]], digest: str
) -> str:
    """The proposed workflow files as diffs against staging, and the one command that approves
    exactly them. Agent-written YAML is shown fenced and escaped, never as markup."""
    origin = f"the run of #{parent}" if parent is not None else "an agent run"
    lines = [
        f"## Workflow change for your approval: #{issue}",
        "",
        f"Requested by {origin}. Nothing is pushed until an admin approves this exact content.",
        "",
        "### Proposed changes",
        "",
    ]
    for path, diff in diffs:
        lines += [f"**{_code(path)}**", "", "```diff", _escape(diff).rstrip("\n"), "```", ""]
    lines += [
        "### Decide",
        "",
        "```",
        f"/approve workflow {digest}",
        "```",
        "",
        "Approving opens a pull request with these files. Merge it yourself: the PM never merges a workflow.",
        "",
        kit_marker("workflow", digest),
    ]
    return "\n".join(lines)


def render_uat_kit(facts: KitFacts, head: str, evidence: Sequence[Evidence], summary: str) -> str:
    approvals = [f"/approve uat {head}"]
    if facts.security_sensitive:
        approvals.append(f"/approve security {head}")
    lines = [
        f"## Ready for your acceptance test: #{facts.issue}",
        "",
        f"Commit under test: `{head}`",
        "",
        *_files(facts),
        "### Evidence",
        "",
        *_evidence_lines(head, evidence),
        "",
        *_advisory(summary),
    ]
    if facts.protected_files:
        lines += [
            "### Admin review required",
            "",
            (
                "This change touches protected files. An admin must approve the pull request "
                "on GitHub (a review on this exact commit) before it can merge:"
            ),
            "",
            *(f"- {_code(path)}" for path in facts.protected_files),
            "",
        ]
    lines += [
        "### Decide",
        "",
        "```",
        *approvals,
        "/reject uat <reason>",
        "```",
        "",
        kit_marker("uat", head),
    ]
    return "\n".join(lines)


def render_revert_kit(facts: KitFacts, head: str, reverts: int, ci_passed: bool) -> str:
    result = "passed" if ci_passed else "did not pass"
    return "\n".join(
        [
            f"## Revert of #{reverts} ready: #{facts.issue}",
            "",
            f"The checks on the revert pull request {result}. Commit: `{head}`",
            "",
            *_files(facts),
            "### Decide (admin)",
            "",
            "```",
            f"/approve revert {head}",
            "/reject revert <reason>",
            "```",
            "",
            kit_marker("revert", head),
        ]
    )


def _code(text: str) -> str:
    """A code span for text we do not control (a file or check name): no backticks, no line
    breaks or control characters, no markup."""
    cleaned = "".join(f"\\x{ord(c):02x}" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in text)
    cleaned = cleaned.replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")
    return f"`{cleaned}`"


def _files(facts: KitFacts) -> list[str]:
    if not facts.changed_files:
        return []
    return ["### Changed files", "", *(f"- {_code(path)}" for path in facts.changed_files), ""]


def _evidence_lines(head: str, evidence: Sequence[Evidence]) -> list[str]:
    latest: dict[tuple[str, str | None], Evidence] = {}
    for item in evidence:
        if item.head_sha == head:
            latest[(item.kind, item.item)] = item
    if not latest:
        return ["- nothing recorded for this commit yet"]
    return [
        f"- {kind}{f' {_code(name)}' if name else ''}: {'passed' if item.passed else 'failed'}"
        for (kind, name), item in sorted(latest.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
    ]


def _advisory(summary: str) -> list[str]:
    text = summary.strip()
    if not text:
        return []
    note = ""
    if len(text) > _SUMMARY_LIMIT:
        text, note = text[:_SUMMARY_LIMIT], "> (summary truncated)"
    quoted = [f"> {line}".rstrip() for line in _escape(text).splitlines()]
    return [
        "### Summary (advisory: written by an agent, not verified)",
        "",
        *quoted,
        *([note] if note else []),
        "",
    ]


def question_marker(key: str) -> str:
    return f"<!-- pm-question:{key} -->"


def render_question(key: str, doubts: Sequence[str]) -> str:
    """The agent's questions, quoted (they are agent text), and how to answer."""
    asked = [f"> {line}".rstrip() for doubt in doubts for line in _single_line(doubt).splitlines()]
    return "\n".join(
        [
            "## A question before this can go on",
            "",
            *(asked or ["> The agent needs a decision from a person."]),
            "",
            "Reply in a comment, or use the explicit form:",
            "",
            "```",
            "/answer <your answer>",
            "```",
            "",
            question_marker(key),
        ]
    )


def _escape(text: str) -> str:
    """Agent text goes into comments as plain quoted text: no fences, no markup, no comments."""
    return text.replace("```", "'''").replace("<", "&lt;").replace(">", "&gt;")


def safe_line(text: str) -> str:
    """Untrusted text as one inert line: no fences or markup, no pings, no line breaks."""
    return _escape(text).replace("@", "@\u200b").replace("\n", " ").strip()


def _single_line(text: str) -> str:
    return _escape(text).strip()


def blocker_marker(key: str) -> str:
    return f"<!-- pm-blocked:{key} -->"


def render_blocker(reason: str, targets: Sequence[str], key: str) -> str:
    """Why the issue stopped, and the commands that can move it on."""
    shown = safe_line(reason)
    options = [f"/unblock {target}" for target in targets]
    return "\n".join(
        [
            "## This issue is blocked",
            "",
            f"> {shown}",
            "",
            *(
                [
                    "An approver can send it on from here, choosing where it restarts:",
                    "",
                    "```",
                    *options,
                    "```",
                    "",
                ]
                if options
                else ["An approver or admin has to look at this before it can go on.", ""]
            ),
            blocker_marker(key),
        ]
    )
