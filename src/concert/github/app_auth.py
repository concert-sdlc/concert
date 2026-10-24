"""GitHub App authentication.

An App proves who it is with a short-lived JWT signed by its private key, and trades it for an
installation token (valid for an hour) that the REST client sends. The token is cached and
renewed shortly before it expires; the private key never leaves this module.
"""

import base64
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from concert.github.http import API, GitHubHttp, Transport

_SKEW = 60  # the JWT is backdated a minute: clocks differ
_LIFETIME = 540  # GitHub allows ten minutes at most
_RENEW_MARGIN = 300  # renew an installation token five minutes before it expires


def make_jwt(private_key_pem: str, app_id: int, now: int) -> str:
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - _SKEW, "exp": now - _SKEW + _LIFETIME, "iss": str(app_id)}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = cast("rsa.RSAPrivateKey", key).sign(
        f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{header}.{payload}.{_b64(signature)}"


class AppAuth:
    def __init__(
        self,
        transport: Transport,
        private_key_pem: str,
        app_id: int,
        installation_id: int,
        clock: Callable[[], float],
    ) -> None:
        self._transport = transport
        self._key = private_key_pem
        self._app_id = app_id
        self._installation = installation_id
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def token(self) -> str:
        now = self._clock()
        if self._token is None or now >= self._expires - _RENEW_MARGIN:
            self._renew(int(now))
        assert self._token is not None
        return self._token

    def _renew(self, now: int) -> None:
        jwt = make_jwt(self._key, self._app_id, now)
        http = GitHubHttp(self._transport, token=lambda: jwt, api=API, max_attempts=2)
        reply: Any = http.json("POST", f"/app/installations/{self._installation}/access_tokens")
        data = cast("dict[str, Any]", reply)
        self._token = str(data["token"])
        self._expires = datetime.fromisoformat(str(data["expires_at"])).timestamp()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
