"""Issue classification, checked against the diff and never taken on trust.

Pure. A `docs` issue gets lighter gates (no automated UAT), so it keeps them only while every
changed path is documentation and none is a file agents read at run start or the process
defines (the protected paths). Anything else is reclassified to a code issue.
"""

from collections.abc import Collection, Sequence

from concert.core.paths import matches_any

DEFAULT_DOCS_PATHS = ("docs/**", "*.md")


def docs_violations(
    changed: Sequence[str], docs_paths: Collection[str], protected: Collection[str]
) -> list[str]:
    """The changed paths that stop an issue from being a docs issue (empty: it qualifies)."""
    return [
        path
        for path in changed
        if not matches_any(path, docs_paths) or matches_any(path, protected)
    ]
