"""Low-latency market-data ingestion from Polygon.io.

Two feeds converge on the :class:`~.chain.ChainStore`:

* **WebSocket OPRA quotes** — ``wss://socket.polygon.io/options``. The
  reader coroutine does nothing but push raw frames onto a bounded
  ``asyncio.Queue``; a separate consumer parses them and updates the chain.
  When the queue is full the OLDEST tick is dropped (for NBBO only the
  latest quote matters), so a slow consumer can never stall the socket and
  back TCP up into Polygon's server.

* **REST option-chain snapshots** — Polygon serves greeks and implied vol
  through ``/v3/snapshot/options/{underlying}``, not the stream, so a
  background task refreshes them every few seconds (greeks drift far more
  slowly than the NBBO) and also feeds the underlying spot price and the
  IV-rank tracker. The blocking ``requests`` calls run in a worker thread
  via ``asyncio.to_thread`` to keep the loop free.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date

import requests
import websockets

from .chain import ChainStore
from .config import SpreadsConfig
from .models import OptionContract

log = logging.getLogger("daytrader.spreads.ingest")


class PolygonOptionsStream:
    """Streams OPRA NBBO quotes into the chain store."""

    def __init__(self, cfg: SpreadsConfig, chain: ChainStore) -> None:
        self.cfg = cfg
        self.chain = chain
        self.queue: asyncio.Queue[list[dict]] = asyncio.Queue(maxsize=cfg.tick_queue_size)
        self.dropped_ticks = 0
        self._subscribed: set[str] = set()
        # Live connection object (ClientConnection on websockets>=13, the
        # legacy protocol on 12); None whenever we're between reconnects.
        self._ws = None

    # ------------------------------------------------------------------ #
    # Reader: socket -> queue (never blocks on downstream work)
    # ------------------------------------------------------------------ #
    async def run_reader(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.cfg.polygon_ws_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(
                        {"action": "auth", "params": self.cfg.polygon_api_key}
                    ))
                    if self._subscribed:
                        await self._send_subscribe(sorted(self._subscribed))
                    backoff = 1.0
                    async for frame in ws:
                        events = json.loads(frame)
                        if not isinstance(events, list):
                            events = [events]
                        self._enqueue(events)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("polygon stream dropped (%s); reconnecting in %.0fs", exc, backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _enqueue(self, events: list[dict]) -> None:
        try:
            self.queue.put_nowait(events)
        except asyncio.QueueFull:
            # Drop-oldest: stale NBBO is worthless, the incoming one is not.
            try:
                self.queue.get_nowait()
                self.dropped_ticks += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(events)
            except asyncio.QueueFull:
                self.dropped_ticks += 1

    async def _send_subscribe(self, channels: list[str]) -> None:
        if self._ws is not None and channels:
            await self._ws.send(json.dumps(
                {"action": "subscribe", "params": ",".join(channels)}
            ))

    async def subscribe_contracts(self, contracts: list[OptionContract]) -> None:
        """Subscribe the NBBO channel for each contract (idempotent)."""
        new = [
            c for t in contracts
            if (c := f"Q.{t.polygon_ticker}") not in self._subscribed
        ]
        if not new:
            return
        self._subscribed.update(new)
        try:
            await self._send_subscribe(new)
        except Exception as exc:  # reconnect logic re-subscribes everything
            log.warning("subscribe failed (%s); will retry on reconnect", exc)

    # ------------------------------------------------------------------ #
    # Consumer: queue -> chain store
    # ------------------------------------------------------------------ #
    async def run_consumer(self) -> None:
        while True:
            events = await self.queue.get()
            for ev in events:
                try:
                    self._apply(ev)
                except Exception:
                    log.exception("bad tick: %r", ev)
            self.queue.task_done()

    def _apply(self, ev: dict) -> None:
        etype = ev.get("ev")
        if etype == "Q":
            contract = OptionContract.from_occ(ev["sym"])
            ts = int(ev.get("t") or 0)
            # Polygon stamps options quotes in ms; normalise to ns and fall
            # back to arrival time if the field is ever absent.
            ts_ns = ts * 1_000_000 if 0 < ts < 10**15 else (ts or time.time_ns())
            self.chain.update_quote(
                contract,
                bid=float(ev.get("bp") or 0.0),
                ask=float(ev.get("ap") or 0.0),
                ts_ns=ts_ns,
            )
        elif etype == "status":
            log.info("polygon: %s %s", ev.get("status"), ev.get("message", ""))


class SnapshotRefresher:
    """Periodic greeks/IV/spot refresh from Polygon's snapshot endpoint."""

    def __init__(self, cfg: SpreadsConfig, chain: ChainStore) -> None:
        self.cfg = cfg
        self.chain = chain
        self.spot: float = 0.0
        self.last_refresh: float = 0.0
        self._session = requests.Session()

    async def run(self, stream: PolygonOptionsStream) -> None:
        while True:
            try:
                contracts = await asyncio.to_thread(self._refresh_once)
                self.last_refresh = time.time()
                # Everything the snapshot knows about in our DTE window gets
                # a live NBBO subscription.
                await stream.subscribe_contracts(contracts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("snapshot refresh failed: %s", exc)
            await asyncio.sleep(self.cfg.greeks_refresh_seconds)

    def _refresh_once(self) -> list[OptionContract]:
        url = (
            f"{self.cfg.polygon_rest_url}/v3/snapshot/options/"
            f"{self.cfg.underlying}?limit=250&apiKey={self.cfg.polygon_api_key}"
        )
        today = date.today()
        found: list[OptionContract] = []
        while url:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("results", []):
                contract = self._ingest_snapshot_item(item, today)
                if contract is not None:
                    found.append(contract)
            url = payload.get("next_url")
            if url:
                url += f"&apiKey={self.cfg.polygon_api_key}"
        return found

    def _ingest_snapshot_item(self, item: dict, today: date) -> OptionContract | None:
        details = item.get("details") or {}
        ticker = details.get("ticker")
        if not ticker:
            return None
        try:
            contract = OptionContract.from_occ(ticker)
        except ValueError:
            return None
        if not (self.cfg.min_dte <= contract.dte(today) <= self.cfg.max_dte):
            return None

        ua = (item.get("underlying_asset") or {}).get("price")
        if ua:
            self.spot = float(ua)

        greeks = item.get("greeks") or {}
        iv = item.get("implied_volatility")
        if greeks or iv is not None:
            self.chain.update_greeks(
                contract,
                delta=float(greeks.get("delta", float("nan"))),
                gamma=float(greeks.get("gamma", float("nan"))),
                iv=float(iv) if iv is not None else float("nan"),
            )
        # Seed the book from the snapshot quote so scanning can start before
        # the first WS tick lands; the stream overwrites it immediately after.
        quote = item.get("last_quote") or {}
        q = self.chain.quote(contract)
        if q is None and quote.get("bid") is not None:
            self.chain.update_quote(
                contract,
                bid=float(quote.get("bid") or 0.0),
                ask=float(quote.get("ask") or 0.0),
                ts_ns=0,  # marked stale on purpose: never tradeable as-is
            )
        return contract
