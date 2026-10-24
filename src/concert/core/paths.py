"""Path globs for protected and security-sensitive files, and tree diffs.

`*` stays inside one directory, `**` crosses directories, and `**/` also matches no directory
at all (`**/auth/**` matches `auth/x.py`). Everything else is literal.
"""

import re
from collections.abc import Collection, Mapping
from functools import cache
from typing import cast

# GitHub runs a workflow from a pushed branch with the repository's secrets, so agents never push
# one: the worker drops such edits and the PM refuses a commit that carries one.
WORKFLOW_PATHS = (".github/workflows/**",)


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)  # file names may contain newlines


def matches_any(path: str, patterns: Collection[str]) -> bool:
    return any(_compile(pattern).match(path) for pattern in patterns)


def changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Paths added, removed or modified between two trees. Trees that know their blob ids
    (the real client's lazy trees) are compared by id, without loading any file."""
    left = cast("Mapping[str, str]", getattr(before, "blob_ids", before))
    right = cast("Mapping[str, str]", getattr(after, "blob_ids", after))
    if hasattr(before, "blob_ids") != hasattr(after, "blob_ids"):
        left, right = before, after  # ids and contents are not comparable
    return sorted(p for p in set(left) | set(right) if left.get(p) != right.get(p))
