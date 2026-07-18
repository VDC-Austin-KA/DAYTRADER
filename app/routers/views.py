"""HTML page routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """The dashboard -- or, on a relay deployment, a bounce to the real one.

    The Railway instance has no route to OpenD (that runs on the trading
    PC, on localhost), so serving the dashboard there produces a page whose
    every data call hangs. That looks broken rather than misconfigured.

    So an instance with no gateway configured redirects to the tunnel the
    home launcher published, making the Railway URL itself the permanent
    bookmark rather than a dead lookalike.
    """
    from ..data import moomoo_data
    from ..models import TunnelPointer

    if not moomoo_data.configured():
        row = db.query(TunnelPointer).first()
        if row and row.url:
            return RedirectResponse(row.url, status_code=302)
        return HTMLResponse(
            "<body style='font-family:system-ui;background:#0b0f17;color:#e6edf6;"
            "padding:40px;line-height:1.6'>"
            "<h2>Relay standing by</h2>"
            "<p>This deployment has no market-data gateway of its own &mdash; it "
            "forwards to the dashboard running beside moomoo OpenD on the trading "
            "PC.</p><p>No tunnel has been published yet. Start the launcher on that "
            "machine, then reload.</p></body>",
            status_code=503,
        )

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "app_name": settings.app_name,
         "watchlist": settings.default_watchlist},
    )
