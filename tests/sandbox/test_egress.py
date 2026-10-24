"""The egress policy: an allowlist of hosts, HTTPS only, public addresses only."""

import pytest

from concert.sandbox.egress import Allowlist, Decision, check_connect

PUBLIC = ["93.184.216.34"]


def resolve_public(_host: str) -> list[str]:
    return PUBLIC


ALLOW = Allowlist(["pypi.org", "*.pythonhosted.org", "api.anthropic.com"])


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("pypi.org", True),
        ("PyPI.org", True),
        ("pypi.org.", True),  # a trailing dot is the same name
        ("files.pythonhosted.org", True),
        ("a.b.pythonhosted.org", True),
        ("pythonhosted.org", False),  # the wildcard covers subdomains only
        ("evilpypi.org", False),
        ("pypi.org.evil.com", False),
        ("notpythonhosted.org", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_allowlist_matching(host: str, expected: bool) -> None:
    assert ALLOW.allows(host) is expected


def test_an_allowed_host_on_443_resolving_to_public_addresses_is_allowed() -> None:
    assert check_connect("pypi.org", 443, ALLOW, resolve_public) == Decision(True, "allowed")


def test_other_ports_are_refused() -> None:
    decision = check_connect("pypi.org", 80, ALLOW, resolve_public)
    assert not decision.allowed and "port" in decision.reason


def test_a_host_outside_the_allowlist_is_refused() -> None:
    decision = check_connect("example.com", 443, ALLOW, resolve_public)
    assert not decision.allowed and "allowlist" in decision.reason


@pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "[2001:4860:4860::8888]", "127.0.0.1"])
def test_ip_literals_are_refused_even_when_listed(ip: str) -> None:
    allow = Allowlist([ip.strip("[]")])
    decision = check_connect(ip, 443, allow, resolve_public)
    assert not decision.allowed and "literal" in decision.reason


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "100.64.0.1",  # carrier-grade NAT
        "ff0e::1",  # global multicast
    ],
)
def test_a_name_that_resolves_to_a_private_address_is_refused(address: str) -> None:
    decision = check_connect("pypi.org", 443, ALLOW, lambda _h: ["93.184.216.34", address])
    assert not decision.allowed and "private" in decision.reason


def test_a_name_that_does_not_resolve_is_refused() -> None:
    assert not check_connect("pypi.org", 443, ALLOW, lambda _h: []).allowed


def test_a_resolver_failure_is_a_refusal_not_a_crash() -> None:
    def broken(_host: str) -> list[str]:
        raise OSError("dns down")

    decision = check_connect("pypi.org", 443, ALLOW, broken)
    assert not decision.allowed and "resolve" in decision.reason
