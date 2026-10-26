"""
Alpaca market-data websocket stream — primary feed for live RollingWindow deques.

REST shadow reconciliation is handled asynchronously by DualBufferDataCoordinator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import threading
from typing import Any

import structlog

from src.ingestor.assets import crypto_data_symbol, infer_asset_class
from src.ingestor.exchange_clock import get_exchange_clock_registry
from src.ingestor.feed_stream_health import get_feed_stream_health_registry
from src.ingestor.feed_ingest_guard import (
    get_feed_sequence_guard,
    get_ingest_circuit_breaker,
)
from src.ingestor.dual_buffer_manager import get_dual_buffer_coordinator
from src.ingestor.level1_depth_cache import get_level1_depth_cache
from src.engine.microstructure_guard import get_microstructure_guard
from src.models import Bar
from src.persistence import db as persistence

log = structlog.get_logger()

BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
# Bound every websocket-teardown await so a hung library stop can never block the
# supervisor forever (close-race requirement c). Small enough that a stock+crypto
# teardown stays under the stop() thread-join budget on a normal shutdown.
WS_CLOSE_TIMEOUT_SECONDS = 3.0


def _install_uvloop_policy() -> None:
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass


class AlpacaMarketDataStreamClock:
    """Background Alpaca websocket consumer with supervisor reconnect loop."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._stock_symbols: tuple[str, ...] = ()
        self._crypto_symbols: tuple[str, ...] = ()
        self._symbol_timeframes: dict[str, str] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def configure_symbols(
        self,
        legs: list[tuple[str, str, str]],
    ) -> None:
        stock: list[str] = []
        crypto: list[str] = []
        timeframes: dict[str, str] = {}
        for symbol, timeframe, asset_class in legs:
            normalized = symbol.upper()
            timeframes[normalized] = timeframe
            if asset_class == "crypto":
                crypto.append(crypto_data_symbol(symbol))
            else:
                stock.append(normalized)
        self._symbol_timeframes = timeframes
        self._stock_symbols = tuple(dict.fromkeys(stock))
        self._crypto_symbols = tuple(dict.fromkeys(crypto))

    def start(self) -> None:
        if self.is_running:
            return
        if not self._stock_symbols and not self._crypto_symbols:
            log.info("market_data_stream_clock_skipped", reason="no_symbols")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervisor_loop,
            name="alpaca-market-data-stream-clock",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "market_data_stream_clock_started",
            stock_symbols=list(self._stock_symbols),
            crypto_symbols=list(self._crypto_symbols),
        )

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        if not self.is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(timeout_seconds, 0.1))
        log.info("market_data_stream_clock_stopped")

    def _supervisor_loop(self) -> None:
        _install_uvloop_policy()
        health = get_feed_stream_health_registry()
        backoff = BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                health.note_reconnect_attempt()
                self._loop.run_until_complete(self._serve())
                backoff = BACKOFF_INITIAL_SECONDS
                if self._stop_event.is_set():
                    break
                health.note_disconnect()
                persistence.log_system_event(
                    "FEED_STREAM_DISCONNECTED",
                    "market data stream ended without stop signal",
                    severity="critical",
                    metadata={"phase": "unexpected_end"},
                )
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                health.note_disconnect()
                persistence.log_system_event(
                    "FEED_STREAM_DISCONNECTED",
                    str(exc),
                    severity="critical",
                    metadata={
                        "backoff_seconds": backoff,
                        "feed_mode": health.mode.value,
                    },
                )
                log.warning(
                    "feed_stream_disconnected",
                    error=str(exc),
                    backoff_seconds=backoff,
                    feed_mode=health.mode.value,
                )
                if health.is_degraded_feed():
                    log.critical(
                        "feed_stream_degraded",
                        disconnected_seconds=health.snapshot().disconnected_seconds,
                    )
            finally:
                if self._loop is not None:
                    self._loop.close()
                    self._loop = None
            if self._stop_event.is_set():
                break
            if health.is_degraded_feed():
                log.critical(
                    "feed_stream_degraded_mode_active",
                    disconnected_seconds=health.snapshot().disconnected_seconds,
                )
            # (d) bounded exponential backoff WITH JITTER: wait a random 50-100% of the
            # current backoff so repeated reconnects cannot self-synchronize into a storm.
            # The exponential SCHEDULE is unchanged (jitter is applied only to the wait).
            self._stop_event.wait(timeout=backoff * (0.5 + random.random() * 0.5))
            backoff = min(backoff * 2.0, BACKOFF_MAX_SECONDS)

    async def _serve(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        if self._stock_symbols:
            tasks.append(asyncio.create_task(self._run_stock_stream()))
        if self._crypto_symbols:
            tasks.append(asyncio.create_task(self._run_crypto_stream()))
        synthetic_blip_seconds = float(
            os.getenv("SOAK_SYNTHETIC_WS_BLIP_SECONDS", "0").strip() or "0"
        )
        if synthetic_blip_seconds > 0.0:
            tasks.append(
                asyncio.create_task(
                    self._run_synthetic_disconnect_loop(synthetic_blip_seconds)
                )
            )
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        finally:
            # CLOSE-BEFORE-RECONNECT at supervisor scope. asyncio.gather does NOT cancel
            # siblings when one task raises, so a single stream ending would otherwise leave
            # the other stream's task (and its open Alpaca socket) orphaned when the loop is
            # torn down for the reconnect -> that lingering socket is exactly what trips
            # Alpaca's per-account connection cap. Cancel every still-running task and await
            # them so each one's finally (-> _close_stream) releases its socket first.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_synthetic_disconnect_loop(self, interval_seconds: float) -> None:
        interval = max(float(interval_seconds), 5.0)
        while not self._stop_event.is_set():
            await asyncio.sleep(interval)
            if self._stop_event.is_set():
                return
            log.warning(
                "synthetic_feed_blip",
                interval_seconds=interval,
            )
            raise RuntimeError("synthetic_soak_ws_blip")

    async def _run_stock_stream(self) -> None:
        from alpaca.data.live import StockDataStream

        from src.ingestor.market_data_feed import soak_market_data_feed

        # N1: the websocket feed MUST be explicit and identical to the REST shadow feed. The
        # alpaca-py default is IEX; running the stream on IEX while the shadow fetches SIP would
        # DIVERGENT-LATCH every leg (active-vs-shadow tape mismatch) and poison the fill/price
        # reference (F4). One source of truth, never the default.
        stream = StockDataStream(self._api_key, self._secret_key, feed=soak_market_data_feed())
        registry = get_exchange_clock_registry()
        health = get_feed_stream_health_registry()
        sequence_guard = get_feed_sequence_guard()
        circuit_breaker = get_ingest_circuit_breaker()

        async def on_quote(quote: Any) -> None:
            symbol = str(getattr(quote, "symbol", "") or "").upper()
            timestamp = getattr(quote, "timestamp", None)
            if timestamp is None:
                return
            bid_price = float(getattr(quote, "bid_price", 0.0) or 0.0)
            ask_price = float(getattr(quote, "ask_price", 0.0) or 0.0)
            bid_size = float(getattr(quote, "bid_size", 0.0) or 0.0)
            ask_size = float(getattr(quote, "ask_size", 0.0) or 0.0)
            if bid_price <= 0.0 and ask_price <= 0.0:
                return
            conditions = getattr(quote, "conditions", None)
            tape = getattr(quote, "tape", None)
            get_level1_depth_cache().update(
                symbol,
                bid_price=bid_price,
                ask_price=ask_price,
                bid_size=bid_size,
                ask_size=ask_size,
                timestamp=timestamp,
                source="stream",
            )
            get_microstructure_guard().note_quote(
                symbol,
                bid_price=bid_price,
                ask_price=ask_price,
                timestamp=timestamp,
                conditions=conditions,
                tape=tape,
            )

        async def on_trade(trade: Any) -> None:
            symbol = str(getattr(trade, "symbol", "") or "").upper()
            timestamp = getattr(trade, "timestamp", None)
            if timestamp is None:
                return
            price = float(getattr(trade, "price", 0.0) or 0.0)
            if price <= 0.0:
                return
            get_microstructure_guard().note_trade(
                symbol,
                price=price,
                timestamp=timestamp,
                conditions=getattr(trade, "conditions", None),
                tape=getattr(trade, "tape", None),
            )

        async def on_trading_status(status: Any) -> None:
            symbol = str(getattr(status, "symbol", "") or "").upper()
            timestamp = getattr(status, "timestamp", None)
            if timestamp is None:
                return
            get_microstructure_guard().note_trading_status(
                symbol,
                status_code=str(getattr(status, "status_code", "") or ""),
                status_message=str(getattr(status, "status_message", "") or ""),
                reason_code=str(getattr(status, "reason_code", "") or ""),
                reason_message=str(getattr(status, "reason_message", "") or ""),
                timestamp=timestamp,
                tape=getattr(status, "tape", None),
                limit_up_price=getattr(status, "limit_up_price", None),
                limit_down_price=getattr(status, "limit_down_price", None),
            )

        async def on_bar(bar: Any) -> None:
            symbol = str(getattr(bar, "symbol", "") or "").upper()
            timestamp = getattr(bar, "timestamp", None)
            if timestamp is None:
                return
            timeframe = self._symbol_timeframes.get(symbol, "15Min")
            vendor_sequence_id = getattr(bar, "id", None)
            if vendor_sequence_id is not None:
                try:
                    vendor_sequence_id = int(vendor_sequence_id)
                except (TypeError, ValueError):
                    vendor_sequence_id = None
            acceptance = sequence_guard.evaluate(
                symbol,
                timeframe,
                timestamp,
                vendor_sequence_id=vendor_sequence_id,
            )
            if not acceptance.accepted:
                persistence.log_system_event(
                    "FEED_SEQUENCE_MISALIGNMENT",
                    acceptance.reason,
                    severity="warning",
                    metadata={"symbol": symbol, "timeframe": timeframe},
                )
                return
            circuit_breaker.note_bar_arrival(timestamp, symbol, timeframe)
            registry.note_stream_bar(
                symbol,
                timeframe,
                bar_timestamp=timestamp,
                asset_class=infer_asset_class(symbol),
            )
            health.note_stream_bar("stock")
            get_microstructure_guard().note_stream_bar(symbol, bar_timestamp=timestamp)
            get_dual_buffer_coordinator().ingest_stream_bar(
                Bar(
                    timestamp=timestamp,
                    open=float(getattr(bar, "open", 0.0) or 0.0),
                    high=float(getattr(bar, "high", 0.0) or 0.0),
                    low=float(getattr(bar, "low", 0.0) or 0.0),
                    close=float(getattr(bar, "close", 0.0) or 0.0),
                    volume=float(getattr(bar, "volume", 0.0) or 0.0),
                    symbol=symbol,
                ),
                timeframe,
            )

        stream.subscribe_quotes(on_quote, *self._stock_symbols)
        stream.subscribe_trades(on_trade, *self._stock_symbols)
        stream.subscribe_trading_statuses(on_trading_status, *self._stock_symbols)
        stream.subscribe_bars(on_bar, *self._stock_symbols)
        await self._run_until_stopped(stream)

    async def _run_crypto_stream(self) -> None:
        from alpaca.data.live import CryptoDataStream

        stream = CryptoDataStream(self._api_key, self._secret_key)
        registry = get_exchange_clock_registry()
        health = get_feed_stream_health_registry()
        sequence_guard = get_feed_sequence_guard()
        circuit_breaker = get_ingest_circuit_breaker()

        async def on_bar(bar: Any) -> None:
            symbol = str(getattr(bar, "symbol", "") or "").upper()
            timestamp = getattr(bar, "timestamp", None)
            if timestamp is None:
                return
            timeframe = self._symbol_timeframes.get(symbol, "1Hour")
            vendor_sequence_id = getattr(bar, "id", None)
            if vendor_sequence_id is not None:
                try:
                    vendor_sequence_id = int(vendor_sequence_id)
                except (TypeError, ValueError):
                    vendor_sequence_id = None
            acceptance = sequence_guard.evaluate(
                symbol,
                timeframe,
                timestamp,
                vendor_sequence_id=vendor_sequence_id,
            )
            if not acceptance.accepted:
                persistence.log_system_event(
                    "FEED_SEQUENCE_MISALIGNMENT",
                    acceptance.reason,
                    severity="warning",
                    metadata={"symbol": symbol, "timeframe": timeframe},
                )
                return
            circuit_breaker.note_bar_arrival(timestamp, symbol, timeframe)
            registry.note_stream_bar(
                symbol,
                timeframe,
                bar_timestamp=timestamp,
                asset_class="crypto",
            )
            health.note_stream_bar("crypto")
            get_dual_buffer_coordinator().ingest_stream_bar(
                Bar(
                    timestamp=timestamp,
                    open=float(getattr(bar, "open", 0.0) or 0.0),
                    high=float(getattr(bar, "high", 0.0) or 0.0),
                    low=float(getattr(bar, "low", 0.0) or 0.0),
                    close=float(getattr(bar, "close", 0.0) or 0.0),
                    volume=float(getattr(bar, "volume", 0.0) or 0.0),
                    symbol=symbol,
                ),
                timeframe,
            )

        stream.subscribe_bars(on_bar, *self._crypto_symbols)
        await self._run_until_stopped(stream)

    async def _run_until_stopped(self, stream: Any) -> None:
        run_task = asyncio.create_task(stream._run_forever())
        try:
            while not self._stop_event.is_set():
                if run_task.done():
                    exc = run_task.exception()
                    if exc is not None:
                        raise exc
                    raise ConnectionError("market data websocket stream ended")
                await asyncio.sleep(0.25)
        finally:
            # Release the Alpaca websocket CLEANLY before returning — on shutdown OR
            # reconnect — so the per-account connection slot is freed and the next connect
            # does not hit "connection limit exceeded". Not shielded: the only path that
            # cancels this coroutine is _serve()'s finally, which then awaits us, so the
            # loop stays alive and this teardown runs to completion during that cancellation.
            await self._close_stream(stream, run_task)

    async def _close_stream(self, stream: Any, run_task: "asyncio.Task[Any]") -> None:
        """Release an Alpaca websocket cleanly, idempotently, and within a bounded time.

        Order is deliberate:
          1. PUBLIC ``stop_ws()`` — the graceful signal; ``_consume`` sees it and closes
             the socket itself, flushing any message it is mid-dispatch (req b: no drop of
             already-received bars — only the transport is torn down; ingested bars already
             live in the dual-buffer coordinator, untouched here).
          2. Bounded wait for ``_run_forever`` to observe the stop and exit on its own.
          3. If it does not (a hung or auth-storming run loop), FORCE-CANCEL it (req c: no
             deadlock) so it can never reopen ``_ws`` after we close it.
          4. PUBLIC ``close()`` as the guaranteed release — a no-op when ``_ws`` is already
             None (req a: no double-close crash; alpaca-py guards both calls).

        Every await is timeout-bounded; teardown never raises into the supervisor.
        """
        timeout = WS_CLOSE_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(stream.stop_ws(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- teardown must never raise
            log.warning("ws_stop_ws_failed", error=str(exc))
        if not run_task.done():
            done, _pending = await asyncio.wait({run_task}, timeout=timeout)
            if not done:
                log.warning("ws_graceful_stop_timeout_force_cancel")
                run_task.cancel()
        # Await the run task to completion so it cannot reopen _ws under us (cancelled or
        # ended-with-error are both fine here; the supervisor logs the disconnect).
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run_task
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stream.close(), timeout=timeout)
