"""Egress policy for sandboxed agents: which hosts a CONNECT tunnel may reach.

Pure decisions; the proxy that enforces them lives next to it. Only HTTPS (port 443) to listed
host names is allowed. IP literals are refused (they would bypass the name list), and so is any
name that resolves to a non-public address, which keeps an allowed name from being turned into a
way to reach the host's network or the cloud metadata service.
"""

import ipaddress
from collections.abc import Callable, Iterable
from dataclasses import dataclass

HTTPS_PORT = 443


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class Allowlist:
    """Exact names, and `*.example.com` for any subdomain (not the bare domain)."""

    def __init__(self, hosts: Iterable[str]) -> None:
        names = [_normalize(h) for h in hosts]
        self._exact = frozenset(n for n in names if not n.startswith("*."))
        self._suffixes = tuple(n[1:] for n in names if n.startswith("*."))  # ".example.com"

    def allows(self, host: str) -> bool:
        name = _normalize(host)
        if not name:
            return False
        return name in self._exact or any(
            name.endswith(s) and len(name) > len(s) for s in self._suffixes
        )


def check_connect(
    host: str, port: int, allowlist: Allowlist, resolve: Callable[[str], list[str]]
) -> Decision:
    if port != HTTPS_PORT:
        return Decision(False, f"port {port} is not allowed")
    if _is_ip_literal(host):
        return Decision(False, "ip literal")
    if not allowlist.allows(host):
        return Decision(False, f"{host} is not on the allowlist")
    try:
        addresses = resolve(_normalize(host))
    except OSError:
        return Decision(False, f"could not resolve {host}")
    if not addresses:
        return Decision(False, f"could not resolve {host}")
    for address in addresses:
        if not _is_public(address):
            return Decision(False, f"{host} resolves to a private address")
    return Decision(True, "allowed")


def _normalize(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast
