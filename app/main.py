"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth
from .config import settings
from .database import init_db
from .routers import api, tunnel, views
from .scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daytrader")


_capture_stop = threading.Event()
_capture_thread: threading.Thread | None = None


def _start_capture() -> None:
    """Run the microstructure recorder alongside the dashboard.

    Started here rather than as a separate command so that launching the
    app -- however it is launched -- is enough. Daemon thread with an
    explicit stop event, and any failure inside it is contained: a capture
    crash must never take the dashboard down.
    """
    global _capture_thread
    if not settings.capture_enabled:
        log.info("microstructure capture disabled (CAPTURE_ENABLED=false)")
        return
    if not settings.moomoo_opend_host:
        log.info("capture skipped: no MOOMOO_OPEND_HOST configured")
        return
    from . import capture

    _capture_thread = threading.Thread(
        target=capture.run, args=(_capture_stop,),
        name="capture", daemon=True,
    )
    _capture_thread.start()
    log.info("microstructure capture started -> %s/", capture.OUT_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    _start_capture()
    log.info("%s started", settings.app_name)
    yield
    _capture_stop.set()
    if _capture_thread is not None:
        # Brief join so the final parquet flush lands before exit.
        _capture_thread.join(timeout=10)
    shutdown_scheduler()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

auth.install(app)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api.router)
app.include_router(tunnel.router)
app.include_router(views.router)
