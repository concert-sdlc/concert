"""The real HTTP transport (urllib), the only code that touches the network for GitHub."""

import urllib.error
import urllib.request
from typing import cast

from concert.github.http import Response


class UrllibTransport:
    def __init__(self, timeout: float = 30, require_https: bool = False) -> None:
        self._timeout = timeout
        self._require_https = require_https

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        if self._require_https and not url.startswith("https://"):
            raise ValueError(f"refusing to send credentials over a non-https url: {url}")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as reply:
                return Response(reply.status, dict(reply.headers), cast("bytes", reply.read()))
        except urllib.error.HTTPError as error:
            return Response(error.code, dict(error.headers), error.read())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ConnectionError(f"could not reach {url}: {error}") from error
