"""Q3 Bug-3: Alpaca websocket close-before-reconnect lifecycle (market_data_stream).

These assert the teardown contract that stops the "connection limit exceeded" storms:
the prior stream's socket is ALWAYS released (close() reached) before the supervisor can
build a new one, teardown is idempotent (safe double-close), and a hung library stop can
never deadlock the supervisor (bounded force-cancel fallback).

Sync tests drive the coroutines with asyncio.run so no pytest-asyncio config is required.
A FakeStream mimics the alpaca-py 0.43.4 semantics we depend on: _run_forever loops until
stop_ws() flips _should_run; close() is guarded/idempotent and clears _ws.
"""
from __future__ import annotations

import asyncio

import src.ingestor.market_data_stream as mds
from src.ingestor.market_data_stream import AlpacaMarketDataStreamClock


class FakeStream:
    """Minimal stand-in for alpaca-py StockDataStream/CryptoDataStream teardown surface."""

    def __init__(self, *, hang_stop: bool = False, hang_run: bool = False) -> None:
        self._ws = "OPEN"           # truthy == connected (alpaca guards close() on `if self._ws`)
        self._should_run = True
        self.hang_stop = hang_stop
        self.hang_run = hang_run
        self.stop_ws_calls = 0
        self.close_calls = 0

    async def _run_forever(self) -> None:
        while self._should_run:
            if self.hang_run:
                await asyncio.sleep(3600)   # simulate a stuck/auth-storming run loop
            await asyncio.sleep(0.005)

    async def stop_ws(self) -> None:
        self.stop_ws_calls += 1
        if self.hang_stop:
            await asyncio.sleep(3600)       # simulate a hung library stop
        self._should_run = False            # graceful: _run_forever will exit

    async def close(self) -> None:
        self.close_calls += 1
        self._ws = None                     # idempotent: a second call is a harmless no-op


def _clock() -> AlpacaMarketDataStreamClock:
    return AlpacaMarketDataStreamClock("key", "secret")


def test_graceful_close_releases_socket_before_return() -> None:
    """Normal reconnect: stop_ws signals, run loop exits, close() releases the socket."""
    async def scenario() -> FakeStream:
        clock = _clock()
        stream = FakeStream()
        run_task = asyncio.create_task(stream._run_forever())
        await asyncio.sleep(0.01)
        await clock._close_stream(stream, run_task)
        return stream

    stream = asyncio.run(scenario())
    assert stream.stop_ws_calls >= 1
    assert stream.close_calls >= 1
    assert stream._ws is None                # socket released


def test_double_close_is_safe() -> None:
    """Closing again after the socket is already gone must not raise (idempotent)."""
    async def scenario() -> FakeStream:
        clock = _clock()
        stream = FakeStream()
        run_task = asyncio.create_task(stream._run_forever())
        await asyncio.sleep(0.01)
        await clock._close_stream(stream, run_task)   # first teardown
        run_task2 = asyncio.create_task(stream._run_forever())
        await asyncio.sleep(0.01)
        await clock._close_stream(stream, run_task2)  # second teardown on same object
        return stream

    stream = asyncio.run(scenario())
    assert stream.close_calls >= 2               # called twice, no exception
    assert stream._ws is None


def test_hung_stop_forces_cancel_and_still_closes(monkeypatch) -> None:
    """A hung stop_ws + stuck run loop must NOT deadlock: bounded fallback force-cancels
    the run task and still reaches close()."""
    monkeypatch.setattr(mds, "WS_CLOSE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> tuple[FakeStream, "asyncio.Task"]:
        clock = _clock()
        stream = FakeStream(hang_stop=True, hang_run=True)
        run_task = asyncio.create_task(stream._run_forever())
        await asyncio.sleep(0.01)
        await asyncio.wait_for(clock._close_stream(stream, run_task), timeout=2.0)
        return stream, run_task

    stream, run_task = asyncio.run(scenario())
    assert stream.close_calls >= 1               # guaranteed release still reached
    assert stream._ws is None
    assert run_task.cancelled()                  # hung run loop was force-cancelled


def test_run_until_stopped_closes_on_stop_event() -> None:
    """Shutdown path: setting the stop event exits the loop and tears the socket down."""
    async def scenario() -> FakeStream:
        clock = _clock()
        stream = FakeStream()
        clock._stop_event.set()                  # request shutdown before entry
        await asyncio.wait_for(clock._run_until_stopped(stream), timeout=2.0)
        return stream

    stream = asyncio.run(scenario())
    assert stream.close_calls >= 1
    assert stream._ws is None


class _FakeClock:
    """Stands in for AlpacaMarketDataStreamClock. stop() no-ops when not running, exactly
    like the real one, so we can assert the websocket is closed EXACTLY ONCE across repeated
    shutdowns."""

    def __init__(self) -> None:
        self.stop_call_count = 0
        self.close_count = 0       # only increments when a real close happens
        self._running = True

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self.stop_call_count += 1
        if not self._running:      # real clock.stop() returns immediately when idle
            return
        self._running = False
        self.close_count += 1


def test_orchestrator_shutdown_closes_stream_exactly_once() -> None:
    """The shutdown wiring (SIGTERM/redeploy path) releases the websocket exactly once,
    and a repeated shutdown does not double-close."""
    from src.engine.orchestrator import TradingOrchestrator

    orch = TradingOrchestrator.__new__(TradingOrchestrator)  # bypass heavy __init__
    fake = _FakeClock()
    orch._market_data_stream_clock = fake

    orch.shutdown()
    assert fake.close_count == 1          # exactly one websocket close on shutdown
    orch.shutdown()                       # idempotent: safe to call again
    assert fake.close_count == 1          # still exactly one — no double-close


def test_orchestrator_shutdown_without_clock_is_safe() -> None:
    """Shutdown must not raise when the stream clock was never created (early-exit path)."""
    from src.engine.orchestrator import TradingOrchestrator

    orch = TradingOrchestrator.__new__(TradingOrchestrator)
    orch.shutdown()                       # no _market_data_stream_clock attr -> no-op, no raise
