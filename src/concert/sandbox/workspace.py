"""Turning a repository tree into a directory an agent can work in, and back into changes.

The agent is untrusted: whatever it leaves in the directory is read back only as plain text files
inside the directory. Symlinks (which could point at host files), binary files, oversized files
and path tricks are refused outright, so a compromised agent cannot smuggle host data or
non-reviewable bytes into a commit. The agent's scratch directory and `.git` are never changes.
"""

import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

SCRATCH = ".concert-run"  # the agent's private area: its result file lives here
_IGNORED_TOP = frozenset({SCRATCH, ".git"})

MAX_FILE_BYTES = 1_000_000
MAX_FILES = 5_000


class UnsafeWorkspace(Exception):
    pass


def result_path(root: Path) -> Path:
    return root / SCRATCH / "result.json"


def materialize(tree: Mapping[str, str], root: Path) -> None:
    for name, content in tree.items():
        relative = _safe_relative(name)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def snapshot(
    root: Path, max_file_bytes: int = MAX_FILE_BYTES, max_files: int = MAX_FILES
) -> dict[str, str]:
    found: dict[str, str] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        is_top = here == root
        for name in list(dirs):
            path = here / name
            if path.is_symlink():
                raise UnsafeWorkspace(f"symlink: {path.relative_to(root).as_posix()}")
            if is_top and name in _IGNORED_TOP:
                dirs.remove(name)
            elif name == ".git":
                raise UnsafeWorkspace(f"nested git directory: {path.relative_to(root).as_posix()}")
        for name in files:
            path = here / name
            relative = path.relative_to(root).as_posix()
            kind = path.lstat().st_mode
            if stat.S_ISLNK(kind):
                raise UnsafeWorkspace(f"symlink: {relative}")
            if is_top and name in _IGNORED_TOP:
                continue
            found[relative] = _read_regular(path, relative, max_file_bytes)
            if len(found) > max_files:
                raise UnsafeWorkspace("too many files")
    return found


def _read_regular(path: Path, relative: str, max_file_bytes: int) -> str:
    """Read a regular file and nothing else. A FIFO, socket or device would block a read forever
    (an agent can `mkfifo`), so the file is opened without blocking or following links and its
    type is checked on the open descriptor, which also closes the race with a swap after lstat."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        raise UnsafeWorkspace(f"not a regular file: {relative}") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeWorkspace(f"not a regular file: {relative}")
        if info.st_size > max_file_bytes:
            raise UnsafeWorkspace(f"file too large: {relative}")
        data = handle.read(max_file_bytes + 1)
    if len(data) > max_file_bytes:
        raise UnsafeWorkspace(f"file too large: {relative}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsafeWorkspace(f"not text: {relative}") from None


def changes_since(before: Mapping[str, str], root: Path) -> dict[str, str | None]:
    """What the agent changed: new and modified files map to their content, deleted ones to None."""
    after = snapshot(root)
    changes: dict[str, str | None] = {p: c for p, c in after.items() if before.get(p) != c}
    changes.update({p: None for p in before if p not in after})
    return changes


def _safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise UnsafeWorkspace(f"unsafe path: {name!r}")
    if path.parts[0] in _IGNORED_TOP:
        raise UnsafeWorkspace(f"reserved path: {name!r}")
    return path
