"""Path globs for protected and security-sensitive files, and tree diffs.

`*` stays inside one directory, `**` crosses directories, and `**/` also matches no directory
at all (`**/auth/**` matches `auth/x.py`). Everything else is literal.
"""

import re
from collections.abc import Collection, Mapping
from functools import cache


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
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns: Collection[str]) -> bool:
    return any(_compile(pattern).match(path) for pattern in patterns)


def changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Paths added, removed or modified between two trees (path -> content or blob id)."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
