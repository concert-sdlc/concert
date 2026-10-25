"""RestGitHub against real GitHub: one test per behaviour the runner relies on.

Each test names the fake behaviour it checks (tests/fakes/test_github_fake.py). A failure here
means the fake is wrong about GitHub, or RestGitHub is wrong about the API: fix the fake and the
client together, then re-run the unit suite.
"""

import base64
import hashlib

import pytest

from concert.core.ci import CheckRun
from concert.github.api import BinaryBlob, Conflict, GitHubError, NotFound, StaleHead
from tests.live.conftest import Live, wait_for

AUTHOR = "concert-live"


def _base(live: Live) -> str:
    return live.settings.base_branch


# --- credentials ---------------------------------------------------------------------------------


def test_both_apps_authenticate_and_are_bots(live: Live) -> None:
    for client in (live.pm, live.worker):
        issue = client.create_issue(f"{live.prefix} whoami", "")
        assert issue.author.is_bot
        assert issue.author.user_id > 0
    assert live.pm.create_issue(f"{live.prefix} ids", "").author.user_id != (
        live.worker.create_issue(f"{live.prefix} ids", "").author.user_id
    )


# --- issues and comments -------------------------------------------------------------------------


def test_issue_comment_lifecycle(live: Live) -> None:
    gh = live.pm
    issue = gh.create_issue(f"{live.prefix} comments", "body")
    assert issue.state == "open"
    assert issue.number in gh.list_open_issues()

    comment = gh.add_comment(issue.number, "/approve spec 1234567")
    assert comment.edited_at is None
    assert comment.author.is_bot

    edited = gh.edit_comment(comment.id, "/approve spec 7654321")
    assert edited.body.endswith("7654321")
    assert edited.created_at == comment.created_at
    assert edited.edited_at is not None, "STATUS.md: edits are detected by updated_at != created_at"

    second = gh.add_comment(issue.number, "next")
    assert [c.id for c in gh.list_comments(issue.number, after_id=comment.id)] == [second.id]

    gh.delete_comment(comment.id)
    assert comment.id not in [c.id for c in gh.list_comments(issue.number)]

    assert gh.close_issue(issue.number).state == "closed"
    assert issue.number not in gh.list_open_issues()
    assert gh.reopen_issue(issue.number).state == "open"


def test_open_issues_never_include_pull_requests(live: Live) -> None:
    branch = f"{live.prefix}/no-prs"
    live.worker.create_branch(branch, _base(live))
    live.worker.commit(branch, "x", {f"{live.prefix}/a.txt": "a"}, AUTHOR, AUTHOR)
    pr = live.worker.create_pr(branch, _base(live), f"{live.prefix} not an issue")
    assert pr.number not in live.pm.list_open_issues()


def test_a_missing_issue_is_not_found(live: Live) -> None:
    with pytest.raises(NotFound):
        live.pm.get_issue(2_000_000_000)


# --- the Projects v2 Status field ----------------------------------------------------------------


def test_status_round_trips_on_the_board(live: Live) -> None:
    if live.settings.board is None:
        pytest.skip("no CONCERT_BOARD_OWNER / CONCERT_BOARD_NUMBER")
    issue = live.pm.create_issue(f"{live.prefix} board", "")
    assert live.pm.get_status(issue.number) is None
    live.pm.set_status(issue.number, "inbox")
    assert live.pm.get_status(issue.number) == "inbox"
    live.pm.set_status(issue.number, "triage")  # the item already exists: updated, not duplicated
    assert live.pm.get_status(issue.number) == "triage"


def test_an_unknown_status_option_is_refused(live: Live) -> None:
    if live.settings.board is None:
        pytest.skip("no board configured")
    issue = live.pm.create_issue(f"{live.prefix} bad status", "")
    with pytest.raises(GitHubError, match="no 'not-a-status' option"):
        live.pm.set_status(issue.number, "not-a-status")


# --- git ------------------------------------------------------------------------------------------


def test_branch_commit_read_back(live: Live) -> None:
    gh = live.worker
    branch = f"{live.prefix}/git"
    base_sha = gh.create_branch(branch, _base(live))
    assert gh.get_ref(branch) == base_sha
    assert gh.get_ref(f"{live.prefix}/missing") is None

    sha = gh.commit(
        branch, "add two files", {"d/a.txt": "alpha", "d/b.txt": "beta"}, "alice", "concert-bot"
    )
    assert gh.get_ref(branch) == sha
    assert gh.commit_parents(sha) == (base_sha,)
    assert gh.read_file(branch, "d/a.txt") == "alpha"
    assert gh.read_file(branch, "d/nope.txt") is None

    delete = gh.commit(branch, "drop one", {"d/a.txt": None, "d/b.txt": "beta2"}, "alice", "bot")
    tree = gh.read_tree(delete)
    assert "d/a.txt" not in tree
    assert tree["d/b.txt"] == "beta2"

    commits = gh.list_commits(_base(live), branch)
    assert [c.sha for c in commits] == [sha, delete]
    assert commits[0].parent == base_sha
    assert commits[0].author == "alice"
    assert commits[0].committer == "concert-bot"


def test_ref_moves_and_deletes(live: Live) -> None:
    gh = live.worker
    branch = f"{live.prefix}/refs"
    first = gh.create_branch(branch, _base(live))
    second = gh.commit(branch, "c", {f"{live.prefix}/r.txt": "r"}, AUTHOR, AUTHOR)
    with pytest.raises(Conflict):  # not a fast-forward without force
        gh.update_ref(branch, first)
    gh.update_ref(branch, first, force=True)
    assert gh.get_ref(branch) == first
    gh.update_ref(branch, second)
    gh.delete_ref(branch)
    assert gh.get_ref(branch) is None


def test_orphan_branch_has_no_parents(live: Live) -> None:
    sha = live.pm.create_orphan_branch(
        f"{live.prefix}/orphan", {"events.jsonl": "{}\n"}, "start", "concert-pm"
    )
    assert live.pm.commit_parents(sha) == ()
    assert live.pm.read_file(f"{live.prefix}/orphan", "events.jsonl") == "{}\n"


# --- pull requests and checks ---------------------------------------------------------------------


def test_pull_request_flow_and_merge_fencing(live: Live) -> None:
    worker, pm = live.worker, live.pm
    branch = f"{live.prefix}/pr"
    worker.create_branch(branch, _base(live))
    head = worker.commit(branch, "change", {f"{live.prefix}/pr.txt": "1"}, AUTHOR, AUTHOR)

    pr = worker.create_pr(branch, _base(live), f"{live.prefix} pr")
    assert pr.draft
    found = pm.find_pr(branch)
    assert found is not None
    assert found.number == pr.number
    assert pm.find_pr(f"{live.prefix}/no-such-head") is None

    assert not pm.mark_ready(pr.number).draft
    assert not pm.mark_ready(pr.number).draft  # repeating is accepted (a replayed intent is safe)
    assert pm.mark_draft(pr.number).draft
    assert pm.mark_draft(pr.number).draft
    pm.mark_ready(pr.number)
    assert pm.list_pr_reviews(pr.number) == []

    newer = worker.commit(branch, "more", {f"{live.prefix}/pr.txt": "2"}, AUTHOR, AUTHOR)
    with pytest.raises(StaleHead):  # the PR head moved after the PM looked
        pm.merge_pr(pr.number, head)

    merge_sha = pm.merge_pr(pr.number, newer)
    merged = pm.get_pr(pr.number)
    assert merged.state == "merged"
    assert merged.merge_sha == merge_sha

    revert = pm.revert_pr(pr.number)
    found_revert = pm.find_revert(pr.number)  # GitHub names it revert-<n>-<head branch>
    assert found_revert is not None and found_revert.number == revert.number
    assert revert.state == "open"
    assert not revert.draft
    assert revert.base == _base(live)


def test_check_runs_carry_the_workflow_hash(live: Live) -> None:
    worker = live.worker
    branch = f"{live.prefix}/ci"
    worker.create_branch(branch, _base(live))
    head = worker.commit(branch, "trigger ci", {f"{live.prefix}/ci.txt": "x"}, AUTHOR, AUTHOR)
    pr = worker.create_pr(branch, _base(live), f"{live.prefix} ci")
    live.pm.mark_ready(pr.number)  # the generated workflow runs on ready_for_review, not on drafts

    runs = wait_for(lambda: [r for r in worker.list_check_runs(head)] or None, seconds=120)
    if runs is None:
        pytest.skip("no check runs: the throwaway repo needs concert-ci.yml on the base branch")
    assert all(r.head_sha == head for r in runs)
    assert all(len(r.workflow_hash) == 64 for r in runs), "every run maps to a workflow file"

    def finished() -> list[CheckRun] | None:
        listed = list(worker.list_check_runs(head))
        return listed if listed and all(r.conclusion for r in listed) else None

    done = wait_for(finished)
    assert done is not None, "check runs never finished"
    # the hash is of the workflow file as it was at that commit, not as it is on the base branch
    text = worker.read_file(head, ".github/workflows/concert-ci.yml")
    if text is not None:
        assert hashlib.sha256(text.encode()).hexdigest() in {r.workflow_hash for r in done}


def test_binary_files_and_executable_modes_survive_a_commit(live: Live) -> None:
    gh = live.worker
    owner, repo = live.settings.owner, live.settings.repo
    branch = f"{live.prefix}/modes"
    gh.create_branch(branch, _base(live))
    base_tree = gh.http.json("GET", f"/repos/{owner}/{repo}/git/commits/{gh.get_ref(branch)}")[
        "tree"
    ]["sha"]
    png = gh.http.json(
        "POST",
        f"/repos/{owner}/{repo}/git/blobs",
        {"content": base64.b64encode(b"\xff\xfe\x00\x01").decode(), "encoding": "base64"},
    )["sha"]
    tree = gh.http.json(
        "POST",
        f"/repos/{owner}/{repo}/git/trees",
        {
            "base_tree": base_tree,
            "tree": [
                {
                    "path": f"{live.prefix}/run.sh",
                    "mode": "100755",
                    "type": "blob",
                    "content": "#!/bin/sh\necho 1\n",
                },
                {"path": f"{live.prefix}/logo.png", "mode": "100644", "type": "blob", "sha": png},
            ],
        },
    )["sha"]
    commit = gh.http.json(
        "POST",
        f"/repos/{owner}/{repo}/git/commits",
        {"message": "seed", "tree": tree, "parents": [gh.get_ref(branch)]},
    )["sha"]
    gh.update_ref(branch, commit)

    seen = gh.read_tree(branch)
    blob = seen[f"{live.prefix}/logo.png"]
    assert isinstance(blob, BinaryBlob) and blob.sha == png  # read, not a crash

    edited = gh.commit(
        branch,
        "edit the script",
        {f"{live.prefix}/run.sh": "#!/bin/sh\necho 2\n", f"{live.prefix}/copy.png": blob},
        AUTHOR,
        AUTHOR,
    )
    modes = {
        e["path"]: (e["mode"], e["sha"])
        for e in gh.http.json(
            "GET", f"/repos/{owner}/{repo}/git/trees/{edited}", None, {"recursive": 1}
        )["tree"]
    }
    assert modes[f"{live.prefix}/run.sh"][0] == "100755"  # still executable
    assert modes[f"{live.prefix}/copy.png"][1] == png  # the same blob, bytes never decoded


def test_a_file_over_one_megabyte_is_read_not_reported_empty(live: Live) -> None:
    gh = live.worker
    branch = f"{live.prefix}/big"
    gh.create_branch(branch, _base(live))
    text = "event line\n" * 120_000  # about 1.3 MB
    gh.commit(branch, "big", {f"{live.prefix}/big.jsonl": text}, AUTHOR, AUTHOR)
    assert gh.read_file(branch, f"{live.prefix}/big.jsonl") == text
