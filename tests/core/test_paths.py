"""Glob matching for protected and security-sensitive paths."""

import pytest

from concert.core.paths import changed_paths, matches_any

CASES = [
    ("backend/**", "backend/export.py", True),
    ("backend/**", "backend/deep/er/x.py", True),
    ("backend/**", "frontend/x.py", False),
    ("**/auth/**", "auth/login.py", True),
    ("**/auth/**", "src/app/auth/login.py", True),
    ("**/auth/**", "src/oauth/login.py", False),
    ("*.md", "README.md", True),
    ("*.md", "docs/guide.md", False),
    ("docs/**", "docs/guide.md", True),
    ("process.yaml", "process.yaml", True),
    ("process.yaml", "sub/process.yaml", False),
    ("jest.config.*", "jest.config.js", True),
    ("jest.config.*", "jest.config.ts", True),
    ("**/settings*", "app/settings.py", True),
    ("**/settings*", "settings.py", True),
    (".github/**", ".github/workflows/ci.yml", True),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("a.b", "axb", False),
    ("backend/**", "backend/odd\nname.py", True),
    ("**/auth/**", "src/au\nth/../auth/x.py", True),
    ("*.md", "new\nline.md", True),
]


@pytest.mark.parametrize(("pattern", "path", "expected"), CASES)
def test_glob(pattern: str, path: str, expected: bool) -> None:
    assert matches_any(path, [pattern]) is expected


def test_any_of_several_patterns_matches() -> None:
    assert matches_any("x.md", ["*.py", "*.md"])
    assert not matches_any("x.txt", ["*.py", "*.md"])
    assert not matches_any("x.txt", [])


def test_changed_paths_lists_added_removed_and_modified_files_sorted() -> None:
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "changed", "d": "4"}
    assert changed_paths(before, after) == ["b", "c", "d"]
