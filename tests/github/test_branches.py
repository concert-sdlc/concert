"""Service branches are created on first use and left alone afterwards."""

from concert.github.api import Identity
from concert.github.branches import ensure_orphan_branch
from tests.fakes.github import FakeGitHub

PM = Identity(user_id=900, login="pm[bot]", is_bot=True)


def test_a_missing_branch_is_created_once() -> None:
    fake = FakeGitHub()
    gh = fake.client(PM)
    ensure_orphan_branch(gh, "concert-events", "concert")
    first = gh.get_ref("concert-events")
    assert first is not None
    ensure_orphan_branch(gh, "concert-events", "concert")
    assert gh.get_ref("concert-events") == first


def test_losing_a_creation_race_is_fine() -> None:
    fake = FakeGitHub()
    gh = fake.client(PM)

    class Racing:
        def __init__(self) -> None:
            self.calls = 0

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(gh, name)

        def get_ref(self, name: str) -> str | None:
            self.calls += 1
            return None if self.calls == 1 else gh.get_ref(name)

    gh.create_orphan_branch("concert-events", {}, "other runner", "x")
    ensure_orphan_branch(Racing(), "concert-events", "concert")  # type: ignore[arg-type]
