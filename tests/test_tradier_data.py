"""Tradier minute-bar loader tests -- offline, no network.

The loader is the only path that makes a backtest runnable without a moomoo
gateway, so its parsing has to be robust to the shapes the sandbox actually
returns: a good series, an empty series, and a non-JSON error body.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.backtest import tradier_data as td


class _Resp:
    def __init__(self, status=200, payload=None, text_body=None):
        self.status_code = status
        self._payload = payload
        self._text = text_body

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def get(self, *a, **kw):
        return self._resp


_GOOD = {"series": {"data": [
    {"time": "2026-07-20T09:30:00", "open": 1.0, "high": 2.0, "low": 0.5,
     "close": 1.5, "volume": 100},
    {"time": "2026-07-20T09:31:00", "open": 1.5, "high": 2.5, "low": 1.0,
     "close": 2.0, "volume": 200},
]}}


def test_parses_a_good_series_into_ohlcv():
    df = td.fetch_day("SPY", dt.date(2026, 7, 20), session=_Session(_Resp(payload=_GOOD)))
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 2
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df["Close"].iloc[-1] == 2.0
    assert df.index.is_monotonic_increasing


def test_empty_series_returns_empty_frame_not_an_error():
    """Weekends/holidays come back with a null series; that is not a failure."""
    df = td.fetch_day("SPY", dt.date(2026, 7, 19),
                      session=_Session(_Resp(payload={"series": None})))
    assert df.empty


def test_http_error_returns_empty_frame():
    """Dates beyond the ~10-day window return 400; must degrade, not raise."""
    df = td.fetch_day("SPY", dt.date(2020, 1, 2),
                      session=_Session(_Resp(status=400)))
    assert df.empty


def test_non_json_body_returns_empty_frame():
    df = td.fetch_day("SPY", dt.date(2026, 7, 20),
                      session=_Session(_Resp(text_body="<html>rate limited")))
    assert df.empty
