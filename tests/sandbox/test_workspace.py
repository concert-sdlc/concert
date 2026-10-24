"""Materialising a tree for an agent and reading back only what it changed."""

import os
from pathlib import Path

import pytest

from concert.sandbox.workspace import (
    UnsafeWorkspace,
    changes_since,
    materialize,
    result_path,
    snapshot,
)

TREE = {"README.md": "hello\n", "src/app.py": "print(1)\n", "docs/specs/1/spec.md": "# s\n"}


def test_materialize_writes_every_file(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    assert (tmp_path / "src/app.py").read_text() == "print(1)\n"
    assert snapshot(tmp_path) == TREE


def test_changes_report_added_modified_and_deleted_files(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    (tmp_path / "src/app.py").write_text("print(2)\n")
    (tmp_path / "new.txt").write_text("n\n")
    (tmp_path / "README.md").unlink()
    assert changes_since(TREE, tmp_path) == {
        "src/app.py": "print(2)\n",
        "new.txt": "n\n",
        "README.md": None,
    }


def test_no_changes_means_an_empty_mapping(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    assert changes_since(TREE, tmp_path) == {}


def test_the_agents_scratch_directory_is_never_part_of_the_changes(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    result_path(tmp_path).parent.mkdir(parents=True)
    result_path(tmp_path).write_text("{}")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("x")
    assert changes_since(TREE, tmp_path) == {}


def test_a_tree_path_that_escapes_the_directory_is_refused(tmp_path: Path) -> None:
    for bad in ("../evil", "/abs/evil", "a/../../evil", ""):
        with pytest.raises(UnsafeWorkspace):
            materialize({bad: "x"}, tmp_path)


def test_a_symlink_created_by_the_agent_is_refused(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("top secret")
    os.symlink(outside, tmp_path / "leak")
    with pytest.raises(UnsafeWorkspace, match="symlink"):
        snapshot(tmp_path)


def test_a_symlinked_directory_is_refused_too(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    os.symlink(tmp_path.parent, tmp_path / "up")
    with pytest.raises(UnsafeWorkspace, match="symlink"):
        snapshot(tmp_path)


def test_binary_files_are_refused_rather_than_silently_dropped(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(UnsafeWorkspace, match="not text"):
        snapshot(tmp_path)


def test_oversized_output_is_refused(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    (tmp_path / "big.txt").write_text("x" * 2_000)
    with pytest.raises(UnsafeWorkspace, match="too large"):
        snapshot(tmp_path, max_file_bytes=1_000)
    (tmp_path / "big.txt").unlink()
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("y")
    with pytest.raises(UnsafeWorkspace, match="too many"):
        snapshot(tmp_path, max_files=6)


def test_a_nested_git_directory_is_refused(tmp_path: Path) -> None:
    materialize(TREE, tmp_path)
    (tmp_path / "vendor/.git").mkdir(parents=True)
    (tmp_path / "vendor/.git/config").write_text("x")
    with pytest.raises(UnsafeWorkspace, match="nested git"):
        snapshot(tmp_path)


def test_the_reserved_top_level_names_cannot_be_materialized(tmp_path: Path) -> None:
    for bad in (".git/config", ".concert-run/result.json"):
        with pytest.raises(UnsafeWorkspace, match="reserved"):
            materialize({bad: "x"}, tmp_path)
