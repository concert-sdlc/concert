"""The daily digest: rendered from the metrics, posted once per UTC day."""

from concert.core.metrics import DAY, compute
from concert.runner.digest import post_digest_once_a_day, render_digest, utc_day
from tests.core.test_metrics import MON, NOW, Log
from tests.runner.helpers import PM, World
from tests.runner.test_service import rig, tick


def test_the_digest_reports_the_figures_and_what_needs_attention() -> None:
    from concert.core.metrics import observe

    logs = [Log().landed(MON, MON + 3 * 3600, "changes_needed"), Log().landed(MON, MON + 3600)]
    figures = compute([observe(i, log.timed, "on_staging") for i, log in enumerate(logs, 1)], NOW)
    text = render_digest(figures, ["#7 is blocked: budget"])
    assert "Daily digest, " + utc_day(NOW) in text
    assert "landed on staging: 2" in text and "first pass: 1/2 (50%)" in text
    assert "re-entries: review 1" in text
    assert "lead time median 2.0 h" in text
    assert "### Needs attention" in text and "#7 is blocked: budget" in text


def test_an_empty_project_still_renders() -> None:
    text = render_digest(compute([], NOW))
    assert "landed on staging: 0" in text and "n/a" in text


def test_a_digest_is_posted_once_per_day() -> None:
    world = World()
    steering = world.new_issue()
    assert post_digest_once_a_day(world.pm, steering, PM, NOW, "first")
    assert not post_digest_once_a_day(world.pm, steering, PM, NOW + 3600, "again")
    assert post_digest_once_a_day(world.pm, steering, PM, NOW + DAY, "next day")
    bodies = [c.body for c in world.pm.list_comments(steering)]
    assert len(bodies) == 2 and "first" in bodies[0] and "next day" in bodies[1]


def test_a_digest_from_someone_else_does_not_count_as_ours() -> None:
    from tests.runner.helpers import ALICE

    world = World()
    steering = world.new_issue()
    world.say(ALICE, steering, f"not the PM\n<!-- pm-digest:{utc_day(NOW)} -->")
    assert post_digest_once_a_day(world.pm, steering, PM, NOW, "ours")


def test_the_service_posts_a_digest_in_steering_each_day() -> None:
    r = rig(steering=True)
    steering = r.service.steering or 0
    tick(r)
    tick(r)
    digests = [c for c in r.world.pm.list_comments(steering) if "pm-digest" in c.body]
    assert len(digests) == 1 and "Daily digest" in digests[0].body
    r.world.fake.tick(DAY)
    tick(r)
    assert len([c for c in r.world.pm.list_comments(steering) if "pm-digest" in c.body]) == 2


def test_the_service_keeps_a_finished_issue_in_the_figures() -> None:
    r = rig(steering=True)
    tick(r)
    assert r.issue in r.service._history  # pyright: ignore[reportPrivateUsage]
    status, timeline = r.service._history[r.issue]  # pyright: ignore[reportPrivateUsage]
    assert status == "inbox" and timeline and timeline[0].event.kind == "created"
