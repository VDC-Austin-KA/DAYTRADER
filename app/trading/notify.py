"""Trade notifications: every fill surfaces, nothing happens silently.

An autonomous daemon placing real orders must never do so invisibly. Every
entry and exit is recorded here the moment it happens; the dashboard polls
and raises a desktop notification (and a toast) so a trade cannot occur
without the operator seeing it -- including on a phone, if the dashboard is
open there.

Kept in memory deliberately: this is an alerting channel, not the ledger.
The Trade table remains the source of truth for what was traded.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("daytrader.notify")

_MAX = 200
_lock = threading.Lock()
_events: deque = deque(maxlen=_MAX)
_seq = 0


def record(kind: str, title: str, detail: str = "", level: str = "info",
           **extra) -> dict:
    """Record a trade event. ``kind``: entry | exit | blocked | error."""
    global _seq
    with _lock:
        _seq += 1
        evt = {
            "id": _seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind, "title": title, "detail": detail,
            "level": level, **extra,
        }
        _events.append(evt)
    log.warning("NOTIFY [%s] %s -- %s", kind, title, detail)
    return evt


def since(after_id: int = 0, limit: int = 50) -> list[dict]:
    """Events newer than ``after_id``, oldest first."""
    with _lock:
        return [e for e in _events if e["id"] > after_id][-limit:]


def latest_id() -> int:
    with _lock:
        return _seq


def clear() -> None:
    """Test helper."""
    global _seq
    with _lock:
        _events.clear()
        _seq = 0
