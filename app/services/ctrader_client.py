from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List

import pandas as pd

from app.broker.ctrader import CTraderBroker
from app.broker.ctrader_market_data import (
    get_connection_status,
    get_current_price,
    get_open_positions,
    get_trendbars,
    has_open_position as md_has_open_position,
    set_position_sl_tp,
    wait_until_ready,
)
from app.strategies.peak_dip.utils import point_size
from app.utils.time import ensure_utc_timestamp

CandleHandler = Callable[[dict[str, Any]], Awaitable[None]]


def _seconds_until_next_close(timeframe: str, *, delay_sec: float = 2.0) -> float:
    now = datetime.now(timezone.utc)

    if timeframe == "M15":
        next_minute = ((now.minute // 15) + 1) * 15
        next_hour = now.hour
        next_day = now.date()
        if next_minute >= 60:
            next_minute = 0
            next_hour += 1
            if next_hour >= 24:
                next_hour = 0
                next_day = (now + timedelta(days=1)).date()

        nxt = datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            next_hour,
            next_minute,
            0,
            tzinfo=timezone.utc,
        )
        return max(1.0, (nxt - now).total_seconds() + delay_sec)

    if timeframe == "M5":
        next_minute = ((now.minute // 5) + 1) * 5
        next_hour = now.hour
        next_day = now.date()
        if next_minute >= 60:
            next_minute = 0
            next_hour += 1
            if next_hour >= 24:
                next_hour = 0
                next_day = (now + timedelta(days=1)).date()

        nxt = datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            next_hour,
            next_minute,
            0,
            tzinfo=timezone.utc,
        )
        return max(1.0, (nxt - now).total_seconds() + delay_sec)

    if timeframe == "M1":
        nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        return max(1.0, (nxt - now).total_seconds() + delay_sec)

    if timeframe == "H4":
        next_hour = ((now.hour // 4) + 1) * 4
        next_day = now.date()
        if next_hour >= 24:
            next_hour = 0
            next_day = (now + timedelta(days=1)).date()

        nxt = datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            next_hour,
            0,
            0,
            tzinfo=timezone.utc,
        )
        return max(1.0, (nxt - now).total_seconds() + delay_sec)

    return 60.0


class cTraderClient:
    """Facade sobre el broker + market data real de cTrader."""

    def __init__(self) -> None:
        self.broker = CTraderBroker()
        self._h4_handlers: Dict[str, List[CandleHandler]] = {}
        self._m15_handlers: Dict[str, List[CandleHandler]] = {}
        self._m5_handlers: Dict[str, List[CandleHandler]] = {}
        self._m1_handlers: Dict[str, List[CandleHandler]] = {}
        self._poll_tasks: List[asyncio.Task] = []
        self._last_close: Dict[tuple[str, str], pd.Timestamp] = {}
        self._poll_error_log_ts: Dict[tuple[str, str, str], float] = {}
        self._open_trade_symbol_locks: Dict[str, asyncio.Lock] = {}

    def _log_poll_error_throttled(self, symbol: str, timeframe: str, message: str, *, interval_sec: float) -> None:
        key = (symbol, timeframe, message[:120])
        now = datetime.now(timezone.utc).timestamp()
        prev = self._poll_error_log_ts.get(key, 0.0)
        if (now - prev) < interval_sec:
            return
        self._poll_error_log_ts[key] = now
        print("[cTraderClient] Error polling {} {}: {}".format(symbol, timeframe, message))

    async def connect(self) -> None:
        # El broker real autentica al instanciarse; aca esperamos estado READY.
        await self._ensure_pollers()
        await self.ensure_ready()

    async def _ensure_pollers(self) -> None:
        if self._poll_tasks:
            return
        # pollers se activan cuando haya suscripciones
        return

    async def ensure_ready(self, timeout: float = 20.0) -> None:
        await wait_until_ready(timeout=timeout)

    async def connection_status(self) -> dict[str, Any]:
        return get_connection_status()

    async def subscribe_h4(self, symbol: str, handler: CandleHandler) -> None:
        await self._register_handler(symbol.upper(), "H4", handler, self._h4_handlers)

    async def subscribe_m15(self, symbol: str, handler: CandleHandler) -> None:
        await self._register_handler(symbol.upper(), "M15", handler, self._m15_handlers)

    async def subscribe_m5(self, symbol: str, handler: CandleHandler) -> None:
        await self._register_handler(symbol.upper(), "M5", handler, self._m5_handlers)

    async def subscribe_m1(self, symbol: str, handler: CandleHandler) -> None:
        await self._register_handler(symbol.upper(), "M1", handler, self._m1_handlers)

    async def _register_handler(
        self,
        symbol: str,
        timeframe: str,
        handler: CandleHandler,
        store: Dict[str, List[CandleHandler]],
    ) -> None:
        handlers = store.setdefault(symbol, [])
        handlers.append(handler)
        if not any(task.get_name() == f"poll-{symbol}-{timeframe}" for task in self._poll_tasks):
            task = asyncio.create_task(self._poll_candles(symbol, timeframe), name=f"poll-{symbol}-{timeframe}")
            self._poll_tasks.append(task)

    async def _poll_candles(self, symbol: str, timeframe: str) -> None:
        if timeframe == "H4":
            store = self._h4_handlers
        elif timeframe == "M15":
            store = self._m15_handlers
        elif timeframe == "M5":
            store = self._m5_handlers
        else:
            store = self._m1_handlers
        key = (symbol, timeframe)
        backoff_sec = 0.0

        while True:
            try:
                if backoff_sec > 0:
                    await asyncio.sleep(backoff_sec)
                else:
                    await asyncio.sleep(_seconds_until_next_close(timeframe))

                df = await get_trendbars(symbol, timeframe, count=8)
                if df.empty or "time" not in df.columns:
                    continue

                last_row = df.iloc[-1]
                last_time = ensure_utc_timestamp(last_row["time"])
                prev = self._last_close.get(key)
                if prev is not None and last_time <= prev:
                    backoff_sec = 0.0
                    continue

                candle = {
                    "symbol": symbol,
                    "time_utc": last_time,
                    "open": float(last_row["open"]),
                    "high": float(last_row["high"]),
                    "low": float(last_row["low"]),
                    "close": float(last_row["close"]),
                }
                handlers = store.get(symbol, [])
                for handler in handlers:
                    await handler(candle)

                self._last_close[key] = last_time
                backoff_sec = 0.0

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                msg = str(exc)
                if "REQUEST_FREQUENCY_EXCEEDED" in msg:
                    self._log_poll_error_throttled(symbol, timeframe, msg, interval_sec=20.0)
                    backoff_sec = min(60.0, backoff_sec * 2 + 5.0)
                elif "Timeout esperando Account AUTH" in msg:
                    self._log_poll_error_throttled(symbol, timeframe, msg, interval_sec=30.0)
                    backoff_sec = min(30.0, backoff_sec * 2 + 2.0)
                else:
                    self._log_poll_error_throttled(symbol, timeframe, msg, interval_sec=15.0)
                    backoff_sec = min(30.0, backoff_sec * 2 + 2.0)

    async def open_trade(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        sl_points: int | None = None,
        tp_points: int | None = None,
    ) -> str:
        symbol_u = symbol.upper().strip()
        if not symbol_u:
            raise RuntimeError("symbol is required")

        # Apertura atomica por simbolo para evitar doble posicion por carrera.
        symbol_lock = self._open_trade_symbol_locks.setdefault(symbol_u, asyncio.Lock())
        async with symbol_lock:
            await self.ensure_ready()
            if await md_has_open_position(symbol_u, None):
                raise RuntimeError(
                    f"Ya existe una posicion abierta para {symbol_u}; no se permite abrir mas de una."
                )

            # Open market sin SL/TP server-side. Luego se hace amend usando precio real de entrada.
            res = await self.broker.open_market_order(
                symbol=symbol_u,
                side=side,
                volume=volume,
                stop_loss=None,
                take_profit=None,
            )

            position_id_raw = res.get("position_id")
            if position_id_raw is None:
                raise RuntimeError("No se obtuvo position_id al abrir la orden")
            position_id = int(position_id_raw)
            close_volume_raw = res.get("volume")
            close_volume = float(close_volume_raw) if close_volume_raw is not None else float(volume)

            final_sl = sl
            final_tp = tp
            if sl_points is not None and tp_points is not None:
                entry_price = await self._resolve_entry_price(
                    symbol=symbol_u,
                    position_id=position_id,
                    fallback_entry=res.get("entry_price"),
                )
                pt = point_size(symbol_u)
                if side.lower() == "sell":
                    final_sl = float(entry_price) + (int(sl_points) * pt)
                    final_tp = float(entry_price) - (int(tp_points) * pt)
                else:
                    final_sl = float(entry_price) - (int(sl_points) * pt)
                    final_tp = float(entry_price) + (int(tp_points) * pt)

            if final_sl is not None or final_tp is not None:
                try:
                    await self._apply_sl_tp_with_verification(
                        symbol=symbol_u,
                        position_id=position_id,
                        expected_sl=final_sl,
                        expected_tp=final_tp,
                    )
                except Exception as exc:
                    closed = await self._close_position_on_unprotected_open(
                        position_id=position_id,
                        close_volume=close_volume,
                    )
                    if closed:
                        raise RuntimeError(
                            f"SL/TP no aplicado para position_id={position_id}. "
                            "La posicion fue cerrada automaticamente para evitar riesgo."
                        ) from exc
                    raise RuntimeError(
                        f"SL/TP no aplicado para position_id={position_id} y no se pudo confirmar cierre automatico."
                    ) from exc

            return str(position_id)

    async def _resolve_entry_price(
        self,
        *,
        symbol: str,
        position_id: int,
        fallback_entry: Any,
    ) -> float:
        if fallback_entry is not None:
            try:
                value = float(fallback_entry)
                if value > 0:
                    return value
            except Exception:
                pass

        for _ in range(8):
            try:
                positions = await get_open_positions()
                for p in positions:
                    if int(p.get("position_id") or 0) != int(position_id):
                        continue
                    open_price = p.get("open_price")
                    if open_price is None:
                        continue
                    value = float(open_price)
                    if value > 0:
                        return value
            except Exception:
                pass
            await asyncio.sleep(0.35)

        # Fallback final (menos ideal): precio de mercado actual.
        return float(await self.price(symbol))

    async def _apply_sl_tp_with_verification(
        self,
        *,
        symbol: str,
        position_id: int,
        expected_sl: float | None,
        expected_tp: float | None,
        max_attempts: int = 10,
        sleep_sec: float = 0.45,
    ) -> None:
        tolerance = max(point_size(symbol.upper()) * 1.2, 1e-6)

        for _ in range(max_attempts):
            await set_position_sl_tp(
                symbol=symbol,
                position_id=position_id,
                stop_loss=expected_sl,
                take_profit=expected_tp,
            )
            await asyncio.sleep(sleep_sec)

            if await self._is_sl_tp_applied(
                position_id=position_id,
                expected_sl=expected_sl,
                expected_tp=expected_tp,
                tolerance=tolerance,
            ):
                return

        raise RuntimeError(
            f"No se pudieron fijar SL/TP luego de {max_attempts} intentos (position_id={position_id})"
        )

    async def _close_position_on_unprotected_open(
        self,
        *,
        position_id: int,
        close_volume: float,
        max_attempts: int = 3,
        wait_between_attempts_sec: float = 0.8,
    ) -> bool:
        for _ in range(max_attempts):
            try:
                await self.broker.close_position(position_id, close_volume)
            except Exception:
                pass

            for _ in range(5):
                await asyncio.sleep(0.4)
                if not await self._position_exists(position_id=position_id):
                    return True

            await asyncio.sleep(wait_between_attempts_sec)

        return not await self._position_exists(position_id=position_id)

    async def _position_exists(self, *, position_id: int) -> bool:
        try:
            positions = await get_open_positions()
        except Exception:
            return True

        for p in positions:
            if int(p.get("position_id") or 0) == int(position_id):
                return True
        return False

    async def _is_sl_tp_applied(
        self,
        *,
        position_id: int,
        expected_sl: float | None,
        expected_tp: float | None,
        tolerance: float,
    ) -> bool:
        positions = await get_open_positions()
        target = None
        for p in positions:
            if int(p.get("position_id") or 0) == int(position_id):
                target = p
                break

        if target is None:
            return False

        actual_sl = target.get("stop_loss")
        actual_tp = target.get("take_profit")

        sl_ok = (
            expected_sl is None
            or (actual_sl is not None and abs(float(actual_sl) - float(expected_sl)) <= tolerance)
        )
        tp_ok = (
            expected_tp is None
            or (actual_tp is not None and abs(float(actual_tp) - float(expected_tp)) <= tolerance)
        )
        return bool(sl_ok and tp_ok)

    async def close_trade(self, position_id: int) -> None:
        await self.ensure_ready()
        await self.broker.close_position(position_id)

    async def price(self, symbol: str) -> float:
        await self.ensure_ready()
        return await get_current_price(symbol)

    async def has_open_position(self, symbol: str, side: str | None = None) -> bool:
        await self.ensure_ready()
        return await md_has_open_position(symbol, side)

