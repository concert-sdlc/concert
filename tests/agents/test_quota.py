"""A usage or rate limit is a pause, not a failure: the run keeps its lease and is not charged."""

import json

import pytest

from concert.agents.claude_code import ClaudeCodeAgent
from concert.runner.worker import AgentFailure, QuotaPause
from concert.sandbox.executor import ExecResult
from tests.agents.test_claude_code import task


class Limited:
    def __init__(self, exit_code: int, stdout: str, stderr: str = "") -> None:
        self.result = ExecResult(exit_code, stdout, stderr)

    def execute(self, *_args: object) -> ExecResult:
        return self.result


def run(executor: Limited) -> None:
    ClaudeCodeAgent(executor, model="m", timeout=60, clock=lambda: 1_000).run(task())  # type: ignore[arg-type]


def test_the_subscription_limit_message_carries_the_reset_time() -> None:
    with pytest.raises(QuotaPause) as caught:
        run(Limited(1, "Claude AI usage limit reached|1900000000"))
    assert caught.value.until == 1_900_000_000


def test_a_json_error_result_is_read_too() -> None:
    body = json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1900000123"})
    with pytest.raises(QuotaPause) as caught:
        run(Limited(1, body))
    assert caught.value.until == 1_900_000_123


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 429 rate_limit_error: Number of request tokens has exceeded your limit",
        "Too many requests, please slow down",
        "Your credit balance is too low to access the Anthropic API",
        "5-hour limit reached",
    ],
)
def test_other_limits_pause_without_a_known_reset(text: str) -> None:
    with pytest.raises(QuotaPause) as caught:
        run(Limited(1, "", text))
    assert caught.value.until is None


def test_the_number_429_alone_is_not_a_limit() -> None:
    with pytest.raises(AgentFailure) as caught:
        run(Limited(1, "", "wrote 4290 bytes to line 429 of x.py, then crashed"))
    assert not isinstance(caught.value, QuotaPause)
    with pytest.raises(QuotaPause):
        run(Limited(1, "", "API Error: 429 overloaded"))


def test_an_ordinary_failure_is_still_a_failure() -> None:
    with pytest.raises(AgentFailure) as caught:
        run(Limited(2, "", "segmentation fault"))
    assert not isinstance(caught.value, QuotaPause)


def test_a_reset_time_in_the_past_is_ignored() -> None:
    with pytest.raises(QuotaPause) as caught:
        run(Limited(1, "usage limit reached|500"))
    assert caught.value.until is None


def test_a_limit_phrase_in_the_agents_own_output_is_not_a_limit() -> None:
    """The agent (or an issue body steering it) can print anything: a success reply that merely
    mentions a limit, and phrases buried in the middle of long output, are ordinary failures."""
    chatty = "\n".join(["usage limit reached|1900000000", *["compiling..."] * 10])
    with pytest.raises(AgentFailure) as caught:
        run(Limited(1, chatty))
    assert not isinstance(caught.value, QuotaPause)
    reply = json.dumps({"is_error": False, "result": "I hit the rate limit of my tests"})
    with pytest.raises(AgentFailure) as caught:
        run(Limited(1, reply))
    assert not isinstance(caught.value, QuotaPause)


def test_an_error_reply_of_the_cli_is_read_from_its_result_field() -> None:
    reply = json.dumps({"is_error": True, "result": "API Error: 429 rate_limit_error"})
    with pytest.raises(QuotaPause):
        run(Limited(1, reply))
