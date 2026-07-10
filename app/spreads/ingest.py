"""Low-latency market-data ingestion from moomoo OpenD.

moomoo's OpenAPI SDK is synchronous and callback-based: real-time pushes
arrive on handler callbacks fired from an SDK-owned thread, and chain
discovery/subscribe calls block. This module bridges both onto the asyncio
loop the same way ``execution.py`` bridges order placement:

* Push callbacks (``StockQuoteHandlerBase`` for greeks/IV/last price,
  ``OrderBookHandlerBase`` for top-of-book bid/ask) marshal onto the event
  loop via ``loop.call_soon_threadsafe`` and enqueue onto a bounded
  ``asyncio.Queue``. The SDK's own thread never touches the chain store
  directly, and a full queue drops the OLDEST tick rather than blocking the
  push thread (backpressure policy, same as a WS reader would need).
* A consumer coroutine drains the queue and updates the ``ChainStore``.
* A separate discovery task polls the option chain for the configured DTE
  window on ``contract_discovery_seconds`` — contracts roll on/off that
  window as expiries pass and OpenD has no "new contract" push — and
  subscribes anything newly seen in throttled batches. The same cycle
  refreshes the underlying's spot price.

Quote pushes carry no per-field exchange timestamp for options, so each
order-book tick is stamped with wall-clock arrival time (``time.time_ns()``);
given the push transport is itself the real-time entitled feed, arrival time
is a faithful proxy for the order-timing guardrail in ``execution.py``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from .chain import ChainStore
from .config import SpreadsConfig
from .models import OptionContract

log = logging.getLogger("daytrader.spreads.ingest")


class MoomooOptionsStream:
    """Owns the OpenD quote connection and the push -> ChainStore pipeline."""

    def __init__(self, cfg: SpreadsConfig, chain: ChainStore) -> None:
        self.cfg = cfg
        self.chain = chain
        self.queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(
            maxsize=cfg.tick_queue_size
        )
        self.dropped_ticks = 0
        self._subscribed: set[str] = set()
        self._ctx = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # Connection + handler registration
    # ------------------------------------------------------------------ #
    def _connect(self):
        """Blocking: open the quote context and register push handlers."""
        from moomoo import OpenQuoteContext  # type: ignore[import-not-found]

        ctx = OpenQuoteContext(host=self.cfg.moomoo_opend_host, port=self.cfg.moomoo_opend_port)
        ctx.set_handler(_make_quote_handler(self))
        ctx.set_handler(_make_orderbook_handler(self))
        return ctx

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while True:
            try:
                self._ctx = await asyncio.to_thread(self._connect)
                log.info("moomoo quote context connected")
                backoff = 1.0
                # The context keeps pushing on its own thread; this
                # coroutine just has to outlive it until cancelled or a
                # keepalive probe finds the socket dead.
                await asyncio.to_thread(self._wait_until_closed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("moomoo quote context dropped (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if self._ctx is not None:
                    try:
                        self._ctx.close()
                    except Exception:
                        pass
                    self._ctx = None
                    self._subscribed.clear()

    def _wait_until_closed(self) -> None:
        """Blocks the worker thread; a keepalive failure raises to trigger
        the reconnect loop instead of leaving a silently dead connection."""
        from moomoo import RET_OK  # type: ignore[import-not-found]

        while True:
            time.sleep(5)
            ret, _ = self._ctx.get_global_state()
            if ret != RET_OK:
                raise ConnectionError("moomoo quote context keepalive failed")

    # ------------------------------------------------------------------ #
    # Subscription (called from the discovery task)
    # ------------------------------------------------------------------ #
    async def subscribe_contracts(self, contracts: list[OptionContract]) -> None:
        if self._ctx is None:
            return
        new = [c.moomoo_code for c in contracts if c.moomoo_code not in self._subscribed]
        if not new:
            return
        await asyncio.to_thread(self._subscribe_batches, new)

    def _subscribe_batches(self, codes: list[str]) -> None:
        from moomoo import RET_OK, SubType  # type: ignore[import-not-found]

        batch_size = max(1, self.cfg.subscribe_batch_size)
        for i in range(0, len(codes), batch_size):
            batch = codes[i : i + batch_size]
            ret, msg = self._ctx.subscribe(batch, [SubType.QUOTE, SubType.ORDER_BOOK])
            if ret != RET_OK:
                log.warning("subscribe failed for %d codes: %s", len(batch), msg)
                continue
            self._subscribed.update(batch)
            if i + batch_size < len(codes):
                time.sleep(self.cfg.subscribe_batch_pause_seconds)

    # ------------------------------------------------------------------ #
    # Thread-safe enqueue (called from SDK callback threads)
    # ------------------------------------------------------------------ #
    def _enqueue_threadsafe(self, kind: str, payload: object) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._enqueue, kind, payload)

    def _enqueue(self, kind: str, payload: object) -> None:
        try:
            self.queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            # Drop-oldest: a stale greeks/NBBO tick is worthless once a
            # fresher one exists, so make room rather than stall the SDK's
            # push thread on a slow consumer.
            try:
                self.queue.get_nowait()
                self.dropped_ticks += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait((kind, payload))
            except asyncio.QueueFull:
                self.dropped_ticks += 1

    # ------------------------------------------------------------------ #
    # Consumer: queue -> chain store
    # ------------------------------------------------------------------ #
    async def run_consumer(self) -> None:
        while True:
            kind, payload = await self.queue.get()
            try:
                if kind == "quote":
                    self._apply_quote_frame(payload)
                elif kind == "orderbook":
                    self._apply_orderbook(payload)
            except Exception:
                log.exception("bad %s tick: %r", kind, payload)
            self.queue.task_done()

    def _apply_quote_frame(self, frame) -> None:
        for row in frame.to_dict("records"):
            try:
                contract = OptionContract.from_moomoo_code(row["code"])
            except ValueError:
                continue  # underlying-stock quote or unrecognised symbol
            iv = row.get("implied_volatility")
            self.chain.update_greeks(
                contract,
                delta=float(row.get("delta", float("nan"))),
                gamma=float(row.get("gamma", float("nan"))),
                # moomoo reports IV as a percentage (e.g. 20.5 == 20.5%).
                iv=float(iv) / 100.0 if iv is not None else float("nan"),
            )

    def _apply_orderbook(self, payload: dict) -> None:
        try:
            contract = OptionContract.from_moomoo_code(payload["code"])
        except ValueError:
            return
        bids, asks = payload.get("Bid") or [], payload.get("Ask") or []
        if not bids or not asks:
            return
        self.chain.update_quote(
            contract,
            bid=float(bids[0][0]),
            ask=float(asks[0][0]),
            ts_ns=time.time_ns(),
        )


def _make_quote_handler(stream: MoomooOptionsStream):
    from moomoo import RET_OK, StockQuoteHandlerBase  # type: ignore[import-not-found]

    class _Handler(StockQuoteHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret_code, content = super().on_recv_rsp(rsp_pb)
            if ret_code != RET_OK:
                log.warning("quote push error: %s", content)
                return ret_code, content
            stream._enqueue_threadsafe("quote", content)
            return RET_OK, content

    return _Handler()


def _make_orderbook_handler(stream: MoomooOptionsStream):
    from moomoo import RET_OK, OrderBookHandlerBase  # type: ignore[import-not-found]

    class _Handler(OrderBookHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret_code, content = super().on_recv_rsp(rsp_pb)
            if ret_code != RET_OK:
                log.warning("order book push error: %s", content)
                return ret_code, content
            stream._enqueue_threadsafe("orderbook", content)
            return RET_OK, content

    return _Handler()


class ContractDiscovery:
    """Periodic chain scan: finds contracts in the DTE window, subscribes
    new ones, and refreshes the underlying's spot price."""

    def __init__(self, cfg: SpreadsConfig, chain: ChainStore) -> None:
        self.cfg = cfg
        self.chain = chain
        self.spot: float = 0.0
        self.last_refresh: float = 0.0
        # A given expiry's strike ladder is static intraday — cache it after
        # the first successful fetch so steady-state discovery only pays for
        # get_option_chain calls on expiries newly entering the DTE window,
        # keeping well under the broker's request-rate quota.
        self._expiry_contracts: dict[str, list[OptionContract]] = {}

    async def run(self, stream: MoomooOptionsStream) -> None:
        while True:
            try:
                if stream._ctx is not None:
                    contracts = await asyncio.to_thread(self._discover_once, stream._ctx)
                    self.last_refresh = time.time()
                    await stream.subscribe_contracts(contracts)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("contract discovery cycle failed")
            await asyncio.sleep(self.cfg.contract_discovery_seconds)

    def _discover_once(self, ctx) -> list[OptionContract]:
        from moomoo import RET_OK, OptionCondType, OptionType  # type: ignore[import-not-found]

        underlying_code = f"US.{self.cfg.underlying}"
        ret, snap = ctx.get_market_snapshot([underlying_code])
        if ret == RET_OK and len(snap) > 0:
            self.spot = float(snap["last_price"].iloc[0])

        ret, exp = ctx.get_option_expiration_date(code=underlying_code)
        if ret != RET_OK:
            log.warning("get_option_expiration_date failed: %s", exp)
            return list(self._all_cached())

        today = date.today()
        in_window = [
            strike_time
            for strike_time in exp["strike_time"]
            if self.cfg.min_dte <= (date.fromisoformat(strike_time) - today).days <= self.cfg.max_dte
        ]
        # Contracts that roll out of the window are dropped from the cache
        # so a long-running session doesn't keep re-subscribing/holding
        # state for expiries that are no longer relevant.
        self._expiry_contracts = {
            st: v for st, v in self._expiry_contracts.items() if st in in_window
        }

        to_fetch = [st for st in in_window if st not in self._expiry_contracts]
        queries = 0
        for strike_time in to_fetch:
            if queries >= self.cfg.max_chain_queries_per_cycle:
                log.debug(
                    "chain query cap reached (%d/cycle); %d expiries remain for next cycle",
                    self.cfg.max_chain_queries_per_cycle, len(to_fetch) - queries,
                )
                break
            ret, chain_df = ctx.get_option_chain(
                code=underlying_code,
                start=strike_time,
                end=strike_time,
                option_type=OptionType.ALL,
                option_cond_type=OptionCondType.ALL,
            )
            queries += 1
            if ret != RET_OK:
                log.warning("get_option_chain failed for %s: %s", strike_time, chain_df)
                continue  # left uncached: retried next cycle
            contracts = []
            for code in chain_df["code"]:
                try:
                    contracts.append(OptionContract.from_moomoo_code(code))
                except ValueError:
                    continue
            self._expiry_contracts[strike_time] = contracts
            if queries < len(to_fetch):
                time.sleep(self.cfg.chain_query_pause_seconds)

        return list(self._all_cached())

    def _all_cached(self) -> list[OptionContract]:
        return [c for contracts in self._expiry_contracts.values() for c in contracts]
