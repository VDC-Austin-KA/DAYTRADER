"""HTTP Basic auth for the dashboard.

This app can place real orders against a real margin account, and the
Cloudflare tunnel puts it on a public URL. A quick-tunnel hostname is random
but it is not a secret: it travels through DNS and TLS logs, and anything
reachable is eventually reached. So when a tunnel is in play, a password is
mandatory rather than optional -- ``start.bat`` refuses to open the tunnel
without one.

Local-only use (127.0.0.1, no tunnel) can run unauthenticated: set no
password and the middleware disables itself.
"""
from __future__ import annotations

import base64
import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from .config import settings

log = logging.getLogger("daytrader.auth")

# Health stays open so the tunnel/Railway healthcheck works without creds.
_OPEN_PATHS = {"/api/health"}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind a shared username/password."""

    def __init__(self, app, username: str, password: str) -> None:
        super().__init__(app)
        self._expected = base64.b64encode(
            f"{username}:{password}".encode()
        ).decode()

    async def dispatch(self, request, call_next):
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        # compare_digest to keep the check constant-time; a timing oracle on a
        # trading dashboard is a small hole but a free one to close.
        if scheme.lower() == "basic" and hmac.compare_digest(token, self._expected):
            return await call_next(request)

        return Response(
            status_code=401,
            content="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="DAYTRADER"'},
        )


def install(app) -> None:
    """Attach auth if a password is configured; warn loudly if not."""
    password = settings.dashboard_password
    if not password:
        log.warning(
            "DASHBOARD_PASSWORD is not set - the dashboard is UNAUTHENTICATED. "
            "Safe on localhost only. Never expose this over a tunnel."
        )
        return
    app.add_middleware(
        BasicAuthMiddleware,
        username=settings.dashboard_user,
        password=password,
    )
    log.info("Dashboard auth enabled for user %r", settings.dashboard_user)
