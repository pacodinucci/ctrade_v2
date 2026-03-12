from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.broker.ctrader_market_data import Quote, get_current_price, subscribe_quotes, unsubscribe_quotes
from app.services.bar_builder import BarBuilder
from app.services.internal_event_bus import InternalEventBus, get_internal_event_bus


@dataclass
class SymbolState:
    symbol: str
    bid: float
    ask: float
    mid: float
    last_tick_time: str


class MarketDataHub:
    """
    Hub interno de market data:
    - Una suscripcion upstream por simbolo.
    - Multiples consumidores internos leyendo cache/snapshots.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: Dict[str, SymbolState] = {}
        self._bar_buffers: Dict[str, Dict[str, deque[dict[str, Any]]]] = {}
        self._listeners: Dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._refcounts: Dict[str, int] = {}
        self._upstream_queues: Dict[str, asyncio.Queue[Quote]] = {}
        self._upstream_tasks: Dict[str, asyncio.Task] = {}
        self._bar_buffer_limits: Dict[str, int] = {
            "M1": 500,
            "M5": 500,
            "M15": 500,
            "H4": 240,
        }
        self._bar_builders: Dict[str, BarBuilder] = {}
        self.event_bus: InternalEventBus = get_internal_event_bus()

    async def subscribe_symbol(self, symbol: str) -> asyncio.Queue[dict[str, Any]]:
        symbol_u = symbol.strip().upper()
        if not symbol_u:
            raise ValueError("symbol is required")

        listener: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        start_needed = False

        async with self._lock:
            listeners = self._listeners.setdefault(symbol_u, set())
            listeners.add(listener)

            current = self._refcounts.get(symbol_u, 0)
            self._refcounts[symbol_u] = current + 1
            start_needed = current == 0

        if start_needed:
            upstream_q = await subscribe_quotes(symbol_u)
            task = asyncio.create_task(self._pump_symbol(symbol_u, upstream_q), name=f"hub-{symbol_u}")
            async with self._lock:
                self._upstream_queues[symbol_u] = upstream_q
                self._upstream_tasks[symbol_u] = task
                self._bar_builders[symbol_u] = BarBuilder(symbol_u)

        return listener

    async def unsubscribe_symbol(self, symbol: str, listener: asyncio.Queue[dict[str, Any]]) -> None:
        symbol_u = symbol.strip().upper()
        if not symbol_u:
            return

        stop_needed = False
        task: Optional[asyncio.Task] = None
        upstream_q: Optional[asyncio.Queue[Quote]] = None

        async with self._lock:
            listeners = self._listeners.get(symbol_u)
            if listeners and listener in listeners:
                listeners.remove(listener)
            if listeners is not None and not listeners:
                self._listeners.pop(symbol_u, None)

            current = self._refcounts.get(symbol_u, 0)
            if current <= 1:
                self._refcounts.pop(symbol_u, None)
                stop_needed = True
                task = self._upstream_tasks.pop(symbol_u, None)
                upstream_q = self._upstream_queues.pop(symbol_u, None)
                self._bar_builders.pop(symbol_u, None)
            else:
                self._refcounts[symbol_u] = current - 1

        if stop_needed:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if upstream_q is not None:
                await unsubscribe_quotes(symbol_u, upstream_q)

    async def snapshot(self, symbol: str) -> Optional[dict[str, Any]]:
        symbol_u = symbol.strip().upper()
        if not symbol_u:
            return None

        async with self._lock:
            state = self._states.get(symbol_u)
            if state is None:
                return None
            return {
                "symbol": state.symbol,
                "bid": state.bid,
                "ask": state.ask,
                "mid": state.mid,
                "price": state.mid,
                "last_tick_time": state.last_tick_time,
                "source": "hub_cache",
            }

    async def snapshot_many(self, symbols: list[str]) -> list[dict[str, Any]]:
        normalized = [s.strip().upper() for s in symbols if s.strip()]
        async with self._lock:
            out: list[dict[str, Any]] = []
            for symbol_u in normalized:
                state = self._states.get(symbol_u)
                if state is None:
                    out.append({"symbol": symbol_u, "price": None, "source": "missing"})
                else:
                    out.append(
                        {
                            "symbol": state.symbol,
                            "bid": state.bid,
                            "ask": state.ask,
                            "mid": state.mid,
                            "price": state.mid,
                            "last_tick_time": state.last_tick_time,
                            "source": "hub_cache",
                        }
                    )
            return out

    async def get_or_fetch_price(self, symbol: str) -> dict[str, Any]:
        symbol_u = symbol.strip().upper()
        if not symbol_u:
            raise ValueError("symbol is required")

        snap = await self.snapshot(symbol_u)
        if snap is not None and snap.get("price") is not None:
            return snap

        price = await get_current_price(symbol_u)
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._states[symbol_u] = SymbolState(
                symbol=symbol_u,
                bid=float(price),
                ask=float(price),
                mid=float(price),
                last_tick_time=now_iso,
            )

        return {
            "symbol": symbol_u,
            "bid": float(price),
            "ask": float(price),
            "mid": float(price),
            "price": float(price),
            "last_tick_time": now_iso,
            "source": "fallback_price",
        }

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            active_symbols = sorted(self._refcounts.keys())
            return {
                "active_symbols_count": len(active_symbols),
                "active_symbols": active_symbols,
                "listeners_count": sum(len(v) for v in self._listeners.values()),
                "cached_symbols_count": len(self._states),
                "bar_buffered_symbols_count": len(self._bar_buffers),
            }

    async def record_bar_close(self, symbol: str, timeframe: str, candle: dict[str, Any]) -> None:
        symbol_u = symbol.strip().upper()
        tf_u = timeframe.strip().upper()
        if not symbol_u or tf_u not in self._bar_buffer_limits:
            return

        normalized = {
            "symbol": symbol_u,
            "timeframe": tf_u,
            "time_utc": str(candle.get("time_utc")),
            "open": float(candle.get("open", 0.0)),
            "high": float(candle.get("high", 0.0)),
            "low": float(candle.get("low", 0.0)),
            "close": float(candle.get("close", 0.0)),
        }

        async with self._lock:
            by_tf = self._bar_buffers.setdefault(symbol_u, {})
            buff = by_tf.get(tf_u)
            if buff is None:
                buff = deque(maxlen=self._bar_buffer_limits[tf_u])
                by_tf[tf_u] = buff
            buff.append(normalized)

    async def get_symbol_state(self, symbol: str) -> dict[str, Any]:
        symbol_u = symbol.strip().upper()
        if not symbol_u:
            raise ValueError("symbol is required")

        async with self._lock:
            quote = self._states.get(symbol_u)
            by_tf = self._bar_buffers.get(symbol_u, {})
            bars = {
                tf: {
                    "count": len(buff),
                    "last": (list(buff)[-1] if buff else None),
                    "last_4": list(buff)[-4:],
                }
                for tf, buff in by_tf.items()
            }

            payload: dict[str, Any] = {
                "symbol": symbol_u,
                "quote": None,
                "bars": bars,
            }
            if quote is not None:
                payload["quote"] = {
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "mid": quote.mid,
                    "price": quote.mid,
                    "last_tick_time": quote.last_tick_time,
                }
            return payload

    async def _pump_symbol(self, symbol: str, upstream_q: asyncio.Queue[Quote]) -> None:
        while True:
            quote = await upstream_q.get()
            ts_utc = datetime.fromtimestamp(float(quote.timestamp), tz=timezone.utc)
            last_tick_iso = ts_utc.isoformat()
            payload = {
                "symbol": symbol,
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "mid": float(quote.mid),
                "price": float(quote.mid),
                "last_tick_time": last_tick_iso,
                "source": "hub_stream",
            }

            async with self._lock:
                self._states[symbol] = SymbolState(
                    symbol=symbol,
                    bid=float(quote.bid),
                    ask=float(quote.ask),
                    mid=float(quote.mid),
                    last_tick_time=last_tick_iso,
                )
                listeners = list(self._listeners.get(symbol, set()))
                builder = self._bar_builders.get(symbol)

            for listener in listeners:
                if listener.full():
                    try:
                        listener.get_nowait()
                    except Exception:
                        pass
                try:
                    listener.put_nowait(payload)
                except Exception:
                    pass

            if builder is not None:
                closed = builder.on_tick(price=float(quote.mid), ts=ts_utc)
                for tf, candle in closed:
                    await self.record_bar_close(symbol, tf, candle)
                    await self.event_bus.publish(
                        f"bar_closed:{symbol}:{tf}",
                        {
                            "type": "bar_closed",
                            "symbol": symbol,
                            "timeframe": tf,
                            "candle": candle,
                        },
                    )


_market_data_hub = MarketDataHub()


def get_market_data_hub() -> MarketDataHub:
    return _market_data_hub
