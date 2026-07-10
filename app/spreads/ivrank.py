"""Implied-volatility rank over a rolling historical window.

IV Rank = 100 * (IV_now - IV_min) / (IV_max - IV_min) over the window.
Observations are ATM IV samples persisted to a small JSON file so the
window survives restarts — an intraday-only sample would make every
morning look like a 50th-percentile day.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("daytrader.spreads.ivrank")

# Persist at most one sample per this many seconds to keep the file tiny.
_SAMPLE_SPACING_S = 300.0


class IVRankTracker:
    def __init__(self, history_path: str, window_days: int) -> None:
        self._path = Path(history_path)
        self._window_s = window_days * 86400.0
        self._samples: list[tuple[float, float]] = []  # (epoch_s, iv)
        self._last_saved = 0.0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._samples = [(float(t), float(v)) for t, v in raw]
            log.info("loaded %d IV samples from %s", len(self._samples), self._path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not load IV history (%s); starting fresh", exc)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._samples))
        except Exception as exc:
            log.warning("could not persist IV history: %s", exc)

    def observe(self, iv: float, now: float | None = None) -> None:
        """Record an ATM IV observation (ignores non-positive/NaN marks)."""
        if not iv or iv != iv or iv <= 0:
            return
        now = now if now is not None else time.time()
        cutoff = now - self._window_s
        self._samples = [(t, v) for t, v in self._samples if t >= cutoff]
        if not self._samples or now - self._samples[-1][0] >= _SAMPLE_SPACING_S:
            self._samples.append((now, iv))
            if now - self._last_saved >= _SAMPLE_SPACING_S:
                self._save()
                self._last_saved = now

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def reload(self) -> None:
        """Re-read the history file (after an external seeding run)."""
        self._samples = []
        self._load()

    def rank(self, current_iv: float) -> float | None:
        """IV rank 0-100, or None until the window has enough texture."""
        if not current_iv or current_iv != current_iv:
            return None
        values = [v for _, v in self._samples] + [current_iv]
        if len(values) < 12:  # ~1 hour of samples minimum
            return None
        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            return 50.0
        return 100.0 * (current_iv - lo) / (hi - lo)
