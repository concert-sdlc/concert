"""The adopt command line: arguments, ids, exit statuses. The decisions are tested in tests/adopt."""

import io
from typing import Any

import pytest

from concert.adopt.adopt import AdoptStop, RepoFacts
from concert.cli import adopting, main
from concert.github.http import Response
from tests.adopt.test_adopt import FACTS, TREE
from tests.adopt.test_init import ADMIN, repo

USERS: dict[str, dict[str, Any]] = {
    "/users/alice": {"login": "alice", "id": 583231},
    "/users/concert-pm%5Bbot%5D": {"login": "concert-pm[bot]", "id": 346900000},
    "/users/concert-worker%5Bbot%5D": {"login": "concert-worker[bot]", "id": 346900002},
}


class Users:
    """Answers only the user lookups; everything else goes to the fake repository."""

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        import json

        path = url.removeprefix("https://api.github.com")
        if path in USERS:
            return Response(200, {}, json.dumps(USERS[path]).encode())
        return Response(404, {}, b'{"message": "Not Found"}')


ARGS = [
    "adopt",
    "--repo",
    "acme/app",
    "--admin",
    "alice",
    "--pm-app",
    "concert-pm",
    "--worker-app",
    "concert-worker",
    "--runner",
    "vm-1",
    "--ci",
    "tests=make test",
]


def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake, gh = repo()

    def client(*_args: object, **_kwargs: object) -> object:
        return gh

    def facts(*_args: object, **_kwargs: object) -> RepoFacts:
        return FACTS

    monkeypatch.setattr(adopting, "RestGitHub", client)
    monkeypatch.setattr(adopting, "_facts", facts)
    gh.read_tree = lambda _ref: TREE  # type: ignore[method-assign]
    return fake


def run(extra: list[str] | None = None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [*ARGS, *(extra or [])],
        env={"GITHUB_TOKEN": "t"} if env is None else env,
        transport=Users(),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_the_first_run_waits_for_approval_and_says_what_to_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire(monkeypatch)
    code, out, _err = run()
    assert code == adopting.WAITING
    assert "/approve adopt " in out and "run this command again" in out


def test_after_the_admins_approval_the_prs_are_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = wire(monkeypatch)
    run()
    steering = next(iter(fake.issues))
    digest = fake.comments_text(steering).split("/approve adopt ")[1].split()[0]
    fake.client(ADMIN).add_comment(steering, f"/approve adopt {digest}")
    code, out, _err = run()
    assert code == 0 and "opened PR #" in out and "protect the branches" in out
    assert len(fake.prs) == 2


def test_a_missing_token_or_check_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch)
    monkeypatch.setattr(adopting, "_gh_token", lambda: "")
    code, _out, err = run(env={})
    assert code == 2 and "no token" in err
    out_err = io.StringIO()
    bad = [a for a in ARGS if a not in ("--ci", "tests=make test")]
    assert main(bad, env={"GITHUB_TOKEN": "t"}, transport=Users(), stderr=out_err) == 2
    assert "at least one check" in out_err.getvalue()


def test_unknown_people_and_apps_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch)
    code, _out, err = run(["--admin", "nobody"])
    assert code == 2 and "no GitHub user 'nobody'" in err
    code, _out, err = run(["--pm-app", "ghost"])
    assert code == 2


def test_check_arguments_must_look_like_name_equals_command() -> None:
    assert adopting._checks(["a=b c"]) == {"a": "b c"}  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(AdoptStop):
        adopting._checks(["nonsense"])  # pyright: ignore[reportPrivateUsage]
