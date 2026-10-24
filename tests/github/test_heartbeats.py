"""Heartbeat refs: concert-heartbeats/<runner> lists the runs a runner is executing."""

from concert.github.api import GitHubApi, Identity
from concert.github.heartbeats import heartbeat_ref, read_heartbeat, write_heartbeat
from tests.fakes.github import FakeGitHub

VM1 = Identity(user_id=346900002, login="concert-vm1[bot]", is_bot=True)


def client() -> GitHubApi:
    fake = FakeGitHub()
    fake.seed_branch(heartbeat_ref("vm-1"), {})
    return fake.client(VM1)


def test_a_heartbeat_round_trips() -> None:
    gh = client()
    write_heartbeat(gh, "vm-1", ["r-1", "r-2"], now=500)
    beat = read_heartbeat(gh, "vm-1")
    assert beat is not None
    assert (beat.runner, beat.at, beat.run_ids) == ("vm-1", 500, frozenset({"r-1", "r-2"}))


def test_the_latest_heartbeat_wins() -> None:
    gh = client()
    write_heartbeat(gh, "vm-1", ["r-1"], now=100)
    write_heartbeat(gh, "vm-1", [], now=160)
    beat = read_heartbeat(gh, "vm-1")
    assert beat is not None and beat.at == 160 and beat.run_ids == frozenset()


def test_a_runner_with_no_heartbeat_ref_or_file_has_no_heartbeat() -> None:
    gh = client()
    assert read_heartbeat(gh, "vm-1") is None
    assert read_heartbeat(gh, "nobody") is None


def test_an_unreadable_heartbeat_counts_as_no_heartbeat() -> None:
    gh = client()
    for junk in ("{not json", '{"at": "now", "run_ids": 3}', "[1, 2]"):
        gh.commit(
            heartbeat_ref("vm-1"), "junk", {"heartbeat.json": junk}, author="x", committer="x"
        )
        assert read_heartbeat(gh, "vm-1") is None
