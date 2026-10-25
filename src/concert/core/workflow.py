"""Workflow change requests: what an agent may ask for, how the request is hashed and shown.

Pure. Workflow files run with the repository's secrets as soon as a branch is pushed, so an agent
never pushes one. It may *propose* new contents in its handoff; the PM files a `workflow` issue
holding exactly those files, an admin approves their hash, and only then does the PM push them.
"""

import difflib
import hashlib
import json
from collections.abc import Mapping

from concert.core.paths import WORKFLOW_PATHS, matches_any

MAX_FILES = 5
MAX_BYTES = 40_000  # all files together: they travel inside one comment


def diff_hash(files: Mapping[str, str]) -> str:
    """Identifies the proposed contents, whatever order the files are listed in."""
    canonical = json.dumps(sorted(files.items()), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def problem_with(files: Mapping[str, str]) -> str | None:
    """Why this request cannot be filed (None: it can)."""
    if not files:
        return "the request names no files"
    if len(files) > MAX_FILES:
        return f"a request changes at most {MAX_FILES} workflow files"
    for path, content in files.items():
        if not matches_any(path, WORKFLOW_PATHS) or not path.endswith((".yml", ".yaml")):
            return f"{path!r} is not a workflow file"
        if ".." in path.split("/"):
            return f"{path!r} leaves the workflows directory"
        if not content.strip():
            return f"{path!r} would be empty (deleting a workflow is an admin's own change)"
    if sum(len(c.encode()) for c in files.values()) > MAX_BYTES:
        return f"the files together exceed {MAX_BYTES} bytes"
    return None


def render_diff(path: str, current: str | None, proposed: str) -> str:
    """A unified diff of one file against what staging has now (all added when it is new)."""
    old = (current or "").splitlines(keepends=True)
    new = proposed.splitlines(keepends=True)
    lines = difflib.unified_diff(
        old, new, fromfile=f"a/{path}" if current is not None else "/dev/null", tofile=f"b/{path}"
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
