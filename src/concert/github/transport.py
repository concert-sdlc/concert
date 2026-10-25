"""The real HTTP transport (urllib), the only code that touches the network for GitHub."""

import urllib.error
import urllib.request
from typing import cast

from concert.github.http import Response


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to wherever it
    points and turn a redirected write into a GET. The 3xx is returned as the response."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class UrllibTransport:
    def __init__(self, timeout: float = 30, require_https: bool = False) -> None:
        self._timeout = timeout
        self._require_https = require_https

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        if self._require_https and not url.startswith("https://"):
            raise ValueError(f"refusing to send credentials over a non-https url: {url}")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _OPENER.open(request, timeout=self._timeout) as reply:
                return Response(reply.status, dict(reply.headers), cast("bytes", reply.read()))
        except urllib.error.HTTPError as error:
            return Response(error.code, dict(error.headers), error.read())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ConnectionError(f"could not reach {url}: {error}") from error
