"""The spec hash: a content hash of everything under docs/specs/<issue>/ (design: spec_revision)."""

from concert.core.spec_hash import spec_dir, spec_tree_hash

BASE = {
    "docs/specs/7/spec.md": "# spec",
    "docs/specs/7/plan.md": "# plan",
    "docs/specs/7/uat.md": "# uat",
    "backend/api.py": "print('x')",
}


def test_the_hash_ignores_files_outside_the_spec_dir() -> None:
    other = BASE | {"backend/api.py": "print('changed')", "README": "new"}
    assert spec_tree_hash(other, 7) == spec_tree_hash(BASE, 7)


def test_the_hash_changes_when_a_spec_file_changes_is_added_or_removed() -> None:
    original = spec_tree_hash(BASE, 7)
    assert spec_tree_hash(BASE | {"docs/specs/7/spec.md": "# spec v2"}, 7) != original
    assert spec_tree_hash(BASE | {"docs/specs/7/extra.md": "x"}, 7) != original
    smaller = {k: v for k, v in BASE.items() if k != "docs/specs/7/uat.md"}
    assert spec_tree_hash(smaller, 7) != original


def test_the_hash_does_not_depend_on_insertion_order() -> None:
    reordered = dict(reversed(list(BASE.items())))
    assert spec_tree_hash(reordered, 7) == spec_tree_hash(BASE, 7)


def test_a_path_boundary_is_part_of_the_hash() -> None:
    """Moving content between file names must not collide."""
    a = {"docs/specs/7/ab": "c"}
    b = {"docs/specs/7/a": "bc"}
    assert spec_tree_hash(a, 7) != spec_tree_hash(b, 7)


def test_other_issues_have_their_own_directories() -> None:
    tree = BASE | {"docs/specs/8/spec.md": "# other"}
    assert spec_tree_hash(tree, 7) == spec_tree_hash(BASE, 7)
    assert spec_dir(7) == "docs/specs/7/"
    assert spec_tree_hash({}, 7) == spec_tree_hash({"x": "y"}, 7)  # an empty spec dir is stable


def test_the_hash_is_a_40_character_hex_digest() -> None:
    digest = spec_tree_hash(BASE, 7)
    assert len(digest) == 40 and all(c in "0123456789abcdef" for c in digest)
