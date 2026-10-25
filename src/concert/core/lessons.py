"""The lessons ledger, read side (Milestone 1): parse `docs/lessons/L*.md` and pick the few an
agent should see at the start of a run.

Pure. A lesson is a Markdown file with YAML frontmatter; only `status: accepted` lessons are
shown. Selection is deterministic (best confidence first, then most recently validated, then id)
and capped, so a long ledger never floods an agent's context. Lessons are text every agent reads,
which makes them a prompt-injection channel: they come only from files an admin merged
(`docs/lessons/**` is a protected path), and they reach the prompt fenced as data.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, cast

import yaml

from concert.core.paths import matches_any

LESSON_DIR = "docs/lessons/"
DEFAULT_CAP = 10
MAX_BODY = 4_000

_ID = re.compile(r"^L\d{4}$")
_TYPES = frozenset({"pitfall", "practice", "tooling", "process"})
_STATUSES = frozenset({"proposed", "accepted", "graduated", "retired"})
_SCOPES = frozenset({"project", "portable"})
_TAGS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    "package.json": "node",
    "pyproject.toml": "python",
    "Dockerfile": "docker",
}


@dataclass(frozen=True)
class Lesson:
    id: str
    type: str
    applies_to: tuple[str, ...]
    confidence: float
    status: str
    scope: str
    last_validated: str
    body: str


def parse_lesson(path: str, text: str) -> Lesson | str:
    """The lesson, or why the file is not one (the caller logs it and carries on)."""
    if not text.startswith("---"):
        return f"{path}: no frontmatter"
    _, _, rest = text.partition("---")
    front, found, body = rest.partition("\n---")
    if not found:
        return f"{path}: unterminated frontmatter"
    try:
        loaded: Any = yaml.safe_load(front)
    except yaml.YAMLError as error:
        return f"{path}: unreadable frontmatter ({error.__class__.__name__})"
    if not isinstance(loaded, dict):
        return f"{path}: frontmatter must be a mapping"
    data = cast("dict[str, Any]", loaded)
    identifier = str(data.get("id", ""))
    if not _ID.match(identifier):
        return f"{path}: id must look like L0001"
    if not path.rsplit("/", 1)[-1].startswith(identifier):
        return f"{path}: the file name must start with the id {identifier}"
    for key, allowed in (("type", _TYPES), ("status", _STATUSES), ("scope", _SCOPES)):
        if data.get(key) not in allowed:
            return f"{path}: {key} must be one of {', '.join(sorted(allowed))}"
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return f"{path}: confidence must be a number"
    if not 0 <= confidence <= 1:
        return f"{path}: confidence must be between 0 and 1"
    applies: Any = data.get("applies_to", [])
    if not isinstance(applies, list) or not all(
        isinstance(a, str) for a in cast("list[Any]", applies)
    ):
        return f"{path}: applies_to must be a list of strings"
    return Lesson(
        id=identifier,
        type=str(data["type"]),
        applies_to=tuple(cast("list[str]", applies)),
        confidence=float(confidence),
        status=str(data["status"]),
        scope=str(data["scope"]),
        last_validated=str(data.get("last_validated", data.get("created", ""))),
        body=body.strip()[:MAX_BODY],
    )


def load_lessons(tree: Mapping[str, str]) -> tuple[list[Lesson], list[str]]:
    """Every lesson file in a repository tree, and the problems found in the ones that failed."""
    lessons: list[Lesson] = []
    problems: list[str] = []
    for path in sorted(p for p in tree if p.startswith(LESSON_DIR) and p.endswith(".md")):
        parsed = parse_lesson(path, tree[path])
        if isinstance(parsed, Lesson):
            lessons.append(parsed)
        else:
            problems.append(parsed)
    return lessons, problems


def stack_tags(paths: Collection[str]) -> frozenset[str]:
    tags: set[str] = set()
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if name in _TAGS:
            tags.add(_TAGS[name])
        elif "." in name:
            tag = _TAGS.get("." + name.rsplit(".", 1)[-1])
            if tag:
                tags.add(tag)
    return frozenset(tags)


def select_lessons(
    lessons: Collection[Lesson],
    state: str,
    paths: Collection[str],
    cap: int = DEFAULT_CAP,
) -> list[Lesson]:
    """Accepted lessons whose `applies_to` names this state, a stack tag of the repository, or a
    glob matching one of its files (a lesson that names nothing applies everywhere)."""
    tags = stack_tags(paths)

    def applies(lesson: Lesson) -> bool:
        if not lesson.applies_to:
            return True
        return any(
            entry == state or entry in tags or any(matches_any(p, [entry]) for p in paths)
            for entry in lesson.applies_to
        )

    chosen = [lesson for lesson in lessons if lesson.status == "accepted" and applies(lesson)]
    chosen.sort(key=lambda x: (-x.confidence, _newest_first(x.last_validated), x.id))
    return chosen[:cap]


def _newest_first(date: str) -> tuple[int, ...]:
    """Sort key putting later ISO dates first."""
    digits = re.sub(r"\D", "", date)
    return tuple(-ord(c) for c in digits.ljust(8, "0"))
