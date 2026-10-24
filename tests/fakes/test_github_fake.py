"""The in-memory fake GitHub: the behavior the runner's e2e tests rely on."""

import pytest

from concert.github.api import Conflict, Forbidden, Identity, NotFound, StaleHead
from tests.fakes.github import FakeGitHub

ALICE = Identity(user_id=1, login="alice", is_bot=False)
BOB = Identity(user_id=2, login="bob", is_bot=False)
PM = Identity(user_id=900, login="concert-pm[bot]", is_bot=True)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


# --- issues and comments ----------------------------------------------------------------------


def test_an_issue_records_its_author_and_starts_open(fake: FakeGitHub) -> None:
    issue = fake.client(ALICE).create_issue("Export CSV", "Please add it")
    assert issue.number == 1
    assert issue.author == ALICE
    assert issue.state == "open"


def test_issue_and_pr_numbers_share_one_sequence(fake: FakeGitHub) -> None:
    gh = fake.client(ALICE)
    fake.seed_branch("main", {"README": "x"})
    gh.create_issue("a", "")
    gh.create_branch("feature", "main")
    pr = gh.create_pr(head="feature", base="main", title="p")
    assert pr.number == 2


def test_issues_can_be_closed_and_reopened(fake: FakeGitHub) -> None:
    gh = fake.client(ALICE)
    issue = gh.create_issue("a", "")
    assert gh.close_issue(issue.number).state == "closed"
    assert gh.reopen_issue(issue.number).state == "open"


def test_a_missing_issue_is_not_found(fake: FakeGitHub) -> None:
    with pytest.raises(NotFound):
        fake.client(ALICE).get_issue(99)


def test_a_comment_records_author_id_and_time(fake: FakeGitHub) -> None:
    gh = fake.client(ALICE)
    issue = gh.create_issue("a", "")
    fake.tick(30)
    comment = gh.add_comment(issue.number, "/dispatch")
    assert comment.author == ALICE
    assert comment.created_at == 30
    assert comment.edited_at is None


def test_comment_ids_increase_across_issues(fake: FakeGitHub) -> None:
    gh = fake.client(ALICE)
    one, two = gh.create_issue("a", ""), gh.create_issue("b", "")
    first = gh.add_comment(one.number, "x")
    second = gh.add_comment(two.number, "y")
    assert second.id > first.id


def test_editing_a_comment_is_visible_and_keeps_the_creation_time(fake: FakeGitHub) -> None:
    gh = fake.client(ALICE)
    issue = gh.create_issue("a", "")
    comment = gh.add_comment(issue.number, "/approve spec 1234567")
    fake.tick(10)
    edited = gh.edit_comment(comment.id, "/approve spec 7654321")
    assert edited.body == "/approve spec 7654321"
    assert edited.edited_at == 10
    assert edited.created_at == comment.created_at


def test_only_the_author_can_edit_or_delete_a_comment(fake: FakeGitHub) -> None:
    issue = fake.client(ALICE).create_issue("a", "")
    comment = fake.client(ALICE).add_comment(issue.number, "x")
    with pytest.raises(Forbidden):
        fake.client(BOB).edit_comment(comment.id, "hijacked")
    with pytest.raises(Forbidden):
        fake.client(BOB).delete_comment(comment.id)


def test_a_deleted_comment_disappears_from_the_listing(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    issue = gh.create_issue("a", "")
    first = gh.add_comment(issue.number, "one")
    gh.add_comment(issue.number, "two")
    gh.delete_comment(first.id)
    assert [c.body for c in gh.list_comments(issue.number)] == ["two"]


def test_comments_list_in_order_and_after_a_cursor(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    issue = gh.create_issue("a", "")
    ids = [gh.add_comment(issue.number, str(n)).id for n in range(3)]
    assert [c.id for c in gh.list_comments(issue.number)] == ids
    assert [c.id for c in gh.list_comments(issue.number, after_id=ids[0])] == ids[1:]


def test_the_projects_status_field_is_set_and_read_per_issue(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    issue = gh.create_issue("a", "")
    assert gh.get_status(issue.number) is None
    gh.set_status(issue.number, "triage")
    assert gh.get_status(issue.number) == "triage"


# --- git: commits, refs, ranges ---------------------------------------------------------------


def test_commits_advance_a_branch_and_record_author_and_committer(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    base = fake.seed_branch("main", {"README": "x"})
    gh.create_branch("pm/1", "main")
    sha = gh.commit(
        "pm/1", "add spec", {"docs/specs/1/spec.md": "# spec"}, author="agent", committer="agent"
    )
    assert gh.get_ref("pm/1") == sha
    assert gh.get_ref("main") == base
    meta = gh.list_commits(base="main", head="pm/1")
    assert [(c.sha, c.parent, c.author, c.committer) for c in meta] == [
        (sha, base, "agent", "agent")
    ]


def test_the_tree_at_a_commit_reflects_all_earlier_changes(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    fake.seed_branch("main", {"README": "x"})
    gh.create_branch("pm/1", "main")
    gh.commit("pm/1", "one", {"a.py": "1"}, author="agent", committer="agent")
    sha = gh.commit("pm/1", "two", {"b.py": "2", "a.py": None}, author="agent", committer="agent")
    assert gh.read_tree(sha) == {"README": "x", "b.py": "2"}


def test_range_listing_returns_the_commits_oldest_first(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    fake.seed_branch("main", {"README": "x"})
    gh.create_branch("pm/1", "main")
    first = gh.commit("pm/1", "one", {"a": "1"}, author="agent", committer="agent")
    second = gh.commit("pm/1", "two", {"b": "2"}, author="agent", committer="agent")
    assert [c.sha for c in gh.list_commits(base="main", head="pm/1")] == [first, second]


def test_a_non_fast_forward_ref_update_needs_force(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    base = fake.seed_branch("main", {"README": "x"})
    gh.create_branch("pm/1", "main")
    one = gh.commit("pm/1", "one", {"a": "1"}, author="agent", committer="agent")
    gh.create_branch("pm/2", "main")
    other = gh.commit("pm/2", "other", {"z": "9"}, author="agent", committer="agent")
    with pytest.raises(Conflict):
        gh.update_ref("pm/1", other)
    gh.update_ref("pm/1", other, force=True)
    assert gh.get_ref("pm/1") == other
    gh.update_ref("pm/1", other)  # same sha: fine
    assert one != other and base != other


def test_refs_can_be_deleted_and_a_missing_ref_reads_as_none(fake: FakeGitHub) -> None:
    gh = fake.client(PM)
    fake.seed_branch("main", {"README": "x"})
    gh.create_branch("pm/1", "main")
    gh.delete_ref("pm/1")
    assert gh.get_ref("pm/1") is None
    with pytest.raises(NotFound):
        gh.delete_ref("pm/1")


# --- pull requests ------------------------------------------------------------------------------


def make_pr(fake: FakeGitHub) -> tuple[int, str]:
    gh = fake.client(PM)
    fake.seed_branch("staging", {"README": "x"})
    gh.create_branch("pm/1", "staging")
    head = gh.commit("pm/1", "work", {"a.py": "1"}, author="agent", committer="agent")
    return gh.create_pr(head="pm/1", base="staging", title="work").number, head


def test_a_pr_starts_as_a_draft_and_must_be_ready_to_merge(fake: FakeGitHub) -> None:
    number, head = make_pr(fake)
    gh = fake.client(PM)
    assert gh.get_pr(number).draft
    with pytest.raises(Conflict):
        gh.merge_pr(number, expected_head=head)
    gh.mark_ready(number)
    assert not gh.get_pr(number).draft


def test_merging_with_a_stale_expected_head_is_refused(fake: FakeGitHub) -> None:
    number, _head = make_pr(fake)
    gh = fake.client(PM)
    gh.mark_ready(number)
    with pytest.raises(StaleHead):
        gh.merge_pr(number, expected_head="0" * 40)
    assert gh.get_pr(number).state == "open"


def test_a_merge_creates_a_merge_commit_on_the_base_and_closes_the_pr(fake: FakeGitHub) -> None:
    number, head = make_pr(fake)
    gh = fake.client(PM)
    before = gh.get_ref("staging")
    gh.mark_ready(number)
    merge_sha = gh.merge_pr(number, expected_head=head)
    assert gh.get_ref("staging") == merge_sha
    assert gh.get_pr(number).state == "merged"
    assert gh.read_tree(merge_sha)["a.py"] == "1"
    assert gh.commit_parents(merge_sha) == (before, head)


def test_a_head_that_moved_after_the_pr_was_opened_changes_the_expected_head(
    fake: FakeGitHub,
) -> None:
    number, head = make_pr(fake)
    gh = fake.client(PM)
    gh.mark_ready(number)
    newer = gh.commit("pm/1", "more", {"b.py": "2"}, author="agent", committer="agent")
    with pytest.raises(StaleHead):
        gh.merge_pr(number, expected_head=head)
    assert gh.merge_pr(number, expected_head=newer)
