"""Stable front door for an ephemeral Cloudflare tunnel.

Quick tunnels mint a new random hostname on every launch, which makes a
phone bookmark useless. This gives one permanent address:

    phone -> https://<your-app>.up.railway.app/go  ->  302  ->  current tunnel

The home launcher POSTs its fresh URL to /api/tunnel at startup. That is a
single HTTP call taking milliseconds -- as opposed to routing the update
through a git commit, which would mean a rebuild and redeploy (minutes of
downtime) plus a junk commit every single morning.

SECURITY
--------
/api/tunnel rewrites where the bookmark sends you, so an unauthenticated
version would let anyone repoint it at a page that imitates this dashboard
and harvests the password. It therefore requires a shared secret, and
refuses to run at all unless TUNNEL_UPDATE_SECRET is set. Submitted URLs
must be https and on trycloudflare.com or a configured custom domain, so a
leaked secret still cannot aim the bookmark at an arbitrary site.

/go is intentionally public: it is a redirect to a host that demands Basic
auth of its own, so it reveals only a hostname, and requiring credentials
here would mean typing them twice.
"""
from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import TunnelPointer

log = logging.getLogger("daytrader.tunnel")

router = APIRouter(tags=["tunnel"])

# Only hosts we are willing to be redirected to.
_ALLOWED_HOST = re.compile(
    r"^https://[a-z0-9-]+\.(trycloudflare\.com|" +
    (re.escape(settings.tunnel_custom_domain) if settings.tunnel_custom_domain
     else r"(?!)") + r")/?$",
    re.IGNORECASE,
)


class TunnelUpdate(BaseModel):
    url: str
    secret: str


def _pointer(db: Session) -> TunnelPointer:
    row = db.query(TunnelPointer).first()
    if row is None:
        row = TunnelPointer(id=1, url="")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.post("/api/tunnel")
def update_tunnel(req: TunnelUpdate, db: Session = Depends(get_db)):
    """Publish the current tunnel URL. Called by the home launcher."""
    expected = settings.tunnel_update_secret
    if not expected:
        raise HTTPException(
            503,
            "TUNNEL_UPDATE_SECRET is not configured on this deployment; "
            "refusing to accept unauthenticated redirect updates.",
        )
    if not hmac.compare_digest(req.secret, expected):
        log.warning("rejected tunnel update with a bad secret")
        raise HTTPException(403, "Bad secret.")

    url = req.url.strip().rstrip("/")
    if not _ALLOWED_HOST.match(url + "/"):
        raise HTTPException(400, f"Refusing to redirect to {url!r}: host not allowed.")

    row = _pointer(db)
    row.url = url
    row.updated_at = datetime.utcnow()
    db.commit()
    log.info("tunnel pointer updated -> %s", url)
    return {"message": "Tunnel URL updated.", "url": url}


@router.get("/api/tunnel")
def read_tunnel(db: Session = Depends(get_db)):
    row = _pointer(db)
    return {
        "url": row.url,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/go")
def go(request: Request, db: Session = Depends(get_db)):
    """Bookmark this. Redirects to wherever the home dashboard is today."""
    row = _pointer(db)
    if not row.url:
        return HTMLResponse(
            "<h2>Dashboard is not running</h2>"
            "<p>No tunnel has been published yet today. Start the launcher on "
            "the trading PC and reload this page.</p>",
            status_code=503,
        )
    age = (datetime.utcnow() - row.updated_at).total_seconds() if row.updated_at else 0
    if age > 24 * 3600:
        # A stale pointer usually means the PC never came up. Say so rather
        # than bouncing the user to a dead hostname.
        return HTMLResponse(
            f"<h2>Link is stale</h2><p>Last published "
            f"{age / 3600:.0f} hours ago ({row.url}). The trading PC may be "
            "offline. Reload once the launcher has run.</p>",
            status_code=503,
        )
    return RedirectResponse(row.url, status_code=302)
