"""GitHub App authentication: a signed JWT, exchanged for a short-lived installation token."""

import base64
import json
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from concert.github.api import Forbidden
from concert.github.app_auth import AppAuth, make_jwt
from concert.github.http import Response


def keypair() -> tuple[str, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_the_jwt_is_rs256_signed_with_the_app_id_and_a_short_lifetime() -> None:
    pem, public = keypair()
    token = make_jwt(pem, app_id=910002, now=1_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(b64decode(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(b64decode(payload))
    assert claims["iss"] == "910002"
    assert claims["iat"] == 1_000_000 - 60  # backdated for clock skew
    assert claims["exp"] - claims["iat"] <= 600  # GitHub allows at most ten minutes
    public.verify(
        b64decode(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


class Exchange:
    def __init__(self, *tokens: tuple[str, str]) -> None:
        self.tokens = list(tokens)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        self.calls.append((method, url, headers))
        token, expires = self.tokens.pop(0)
        return Response(201, {}, json.dumps({"token": token, "expires_at": expires}).encode())


def auth(exchange: Exchange, clock: Callable[[], float]) -> AppAuth:
    pem, _public = keypair()
    return AppAuth(exchange, pem, app_id=910002, installation_id=52000102, clock=clock)


def test_the_installation_token_is_fetched_with_the_jwt() -> None:
    exchange = Exchange(("ghs_one", "2026-10-24T13:00:00Z"))
    assert auth(exchange, lambda: 1_792_000_000).token() == "ghs_one"
    method, url, headers = exchange.calls[0]
    assert (method, url) == (
        "POST",
        "https://api.github.com/app/installations/52000102/access_tokens",
    )
    assert headers["Authorization"].startswith("Bearer ey")


def test_a_token_is_reused_until_shortly_before_it_expires() -> None:
    # 2026-10-24T13:00:00Z is 1792846800
    exchange = Exchange(("ghs_one", "2026-10-24T13:00:00Z"), ("ghs_two", "2026-10-24T14:00:00Z"))
    now = [1_792_846_800 - 3600]
    a = auth(exchange, lambda: now[0])
    assert a.token() == "ghs_one" and a.token() == "ghs_one"
    assert len(exchange.calls) == 1
    now[0] = 1_792_846_800 - 200  # inside the five-minute safety margin
    assert a.token() == "ghs_two" and len(exchange.calls) == 2


def test_a_refused_exchange_is_an_error() -> None:
    class Refusing:
        def send(self, *_a: object) -> Response:
            return Response(401, {}, b'{"message": "Bad credentials"}')

    pem, _public = keypair()
    broken = AppAuth(Refusing(), pem, app_id=1, installation_id=2, clock=lambda: 0)  # type: ignore[arg-type]
    with pytest.raises(Forbidden, match="Bad credentials"):
        broken.token()
