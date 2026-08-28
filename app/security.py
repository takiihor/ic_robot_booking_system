"""Same-origin protection for state-changing form posts — SPEC 20.

The app has no login and no cookies, so there is no session token to bind a
CSRF nonce to. What a browser *can* be trusted to send is the Origin (or
Referer) header, so we simply require that any POST came from this same site.
That blocks a malicious page on another host from silently posting approvals,
which is the realistic risk on a shared LAN.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

log = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-site POSTs. Requests without an Origin/Referer are allowed
    through, because curl and other non-browser clients never send one and the
    attack this guards against requires a browser."""

    async def dispatch(self, request, call_next):
        if request.method not in SAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source:
                host = request.headers.get("host", "")
                source_host = urlparse(source).netloc
                if source_host and source_host != host:
                    log.warning(
                        "Blocked cross-site %s %s from %s",
                        request.method,
                        request.url.path,
                        source_host,
                    )
                    return PlainTextResponse(
                        "Cross-site form submissions are not allowed.", status_code=403
                    )
        return await call_next(request)
