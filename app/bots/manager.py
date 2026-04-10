from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict

from app.db.repository import BotCreateInput, BotRepository
from app.services.ctrader_client import cTraderClient
from app.services.internal_event_bus import InternalEventBus, get_internal_event_bus
from app.services.market_data_hub import MarketDataHub, get_market_data_hub
from app.strategies.base import Strategy
from app.strategies.registry import get_strategy_definition, list_strategies_metadata, strategy_ids


class BotManager:
    def __init__(
        self,
        client: cTraderClient,
        repository: BotRepository | None = None,
        market_data_hub: MarketDataHub | None = None,
        event_bus: InternalEventBus | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.market_data_hub = market_data_hub or get_market_data_hub()
        self.event_bus = event_bus or get_internal_event_bus()
        self.bots: Dict[str, Strategy] = {}
        self._logs: Dict[str, deque[dict[str, Any]]] = {}
        self._subscribed_symbol_timeframes: set[tuple[str, str]] = set()
        self._bot_event_tasks: Dict[str, list[asyncio.Task]] = {}
        self._bot_event_subscriptions: Dict[str, list[tuple[str, asyncio.Queue[dict[str, Any]]]]] = {}
        self._hub_symbol_listeners: Dict[str, asyncio.Queue[dict[str, Any]]] = {}

    async def create_bot(
        self,
        *,
        symbol: str,
        strategy: str,
        strategy_params: dict[str, Any],
        user_id: str,
        name: str | None = None,
        account_id: str | None = None,
    ) -> str:
        normalized_strategy = strategy.strip()
        normalized_symbol = symbol.strip().upper()
        normalized_params = self._validate_and_normalize_params(normalized_strategy, strategy_params)

        bot_id = str(uuid.uuid4())
        if self.repository is not None:
            bot_name = (name or f"{normalized_strategy}_{normalized_symbol}_{bot_id[:8]}").strip()
            payload = BotCreateInput(
                id=bot_id,
                name=bot_name,
                instrument=normalized_symbol,
                user_id=user_id,
                strategy=normalized_strategy,
                strategy_params=normalized_params,
                account_id=account_id,
            )
            await self.repository.create_bot(payload)
        self._log(
            bot_id,
            "CREATED",
            {
                "symbol": normalized_symbol,
                "strategy": normalized_strategy,
                "params": normalized_params,
            },
        )

        return bot_id

    async def list_bots(self, user_id: str | None = None) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        records = await self.repository.list_bots(user_id=user_id)
        for row in records:
            row["runtimeActive"] = str(row["id"]) in self.bots
        return records

    async def get_bot(self, bot_id: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        row = await self.repository.get_bot(bot_id)
        if row is None:
            return None

        is_active = str(row["id"]) in self.bots
        row["runtimeActive"] = is_active

        if is_active:
            runtime = self.bots.get(str(row["id"]))
            if runtime is not None and hasattr(runtime, "get_runtime_state"):
                try:
                    row["strategyRuntimeState"] = runtime.get_runtime_state()
                except Exception as exc:
                    row["strategyRuntimeState"] = {"error": str(exc)}

        return row

    async def update_bot(
        self,
        *,
        bot_id: str,
        name: str | None,
        symbol: str | None,
        account_id: str | None,
        strategy: str | None,
        strategy_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if self.repository is None:
            raise RuntimeError("Bot repository is not configured")

        current = await self.repository.get_bot(bot_id)
        if current is None:
            raise KeyError("Bot no encontrado")

        next_strategy = (strategy or str(current.get("strategy") or "peak_dip")).strip()
        if next_strategy not in strategy_ids():
            raise ValueError(f"estrategia no soportada: {next_strategy}")

        next_symbol = (symbol or str(current.get("instrument") or "")).strip().upper()
        if not next_symbol:
            raise ValueError("symbol is required")

        next_name = (name or str(current.get("name") or "")).strip()
        if not next_name:
            raise ValueError("name no puede estar vacio")

        current_params = current.get("params") if isinstance(current.get("params"), dict) else {}
        merged_params = dict(current_params)
        if strategy_params:
            merged_params.update(strategy_params)
        normalized_params = self._validate_and_normalize_params(next_strategy, merged_params)

        next_account_id = account_id if account_id is not None else current.get("accountId")

        updated = await self.repository.update_bot(
            bot_id=bot_id,
            name=next_name,
            instrument=next_symbol,
            account_id=str(next_account_id) if next_account_id is not None else None,
            strategy=next_strategy,
            strategy_params=normalized_params,
        )
        if not updated:
            raise KeyError("Bot no encontrado")

        if bot_id in self.bots:
            previous_runtime = self.bots.get(bot_id)
            if previous_runtime is not None:
                await self._stop_runtime(bot_id, previous_runtime)
            self.bots[bot_id] = self._build_strategy(next_strategy, next_symbol, normalized_params)
            self._attach_runtime_metadata(self.bots[bot_id], bot_id=bot_id, strategy=next_strategy)
            await self._ensure_runtime_market_subscription(self.bots[bot_id])
            await self._start_runtime(bot_id, self.bots[bot_id])
        await self._release_unused_symbol_spot_streams()
        self._log(
            bot_id,
            "UPDATED",
            {"symbol": next_symbol, "strategy": next_strategy, "params": normalized_params},
        )

        row = await self.repository.get_bot(bot_id)
        if row is None:
            raise KeyError("Bot no encontrado")
        row["runtimeActive"] = bot_id in self.bots
        return row

    async def delete_bot(self, bot_id: str) -> bool:
        if self.repository is None:
            raise RuntimeError("Bot repository is not configured")

        runtime = self.bots.pop(bot_id, None)
        if runtime is not None:
            await self._stop_runtime(bot_id, runtime)
            await self._release_unused_symbol_spot_streams()
        deleted = await self.repository.soft_delete_bot(bot_id)
        if deleted:
            self._log(bot_id, "DELETED", {})
        return deleted

    async def start_bot(self, bot_id: str) -> dict[str, Any]:
        await self.client.ensure_ready()
        if self.repository is None:
            raise RuntimeError("Bot repository is not configured")

        row = await self.repository.get_bot(bot_id)
        if row is None:
            raise KeyError("Bot no encontrado")

        strategy = str(row.get("strategy") or "peak_dip").strip()
        symbol = str(row.get("instrument") or "").upper()
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        normalized_params = self._validate_and_normalize_params(strategy, params)

        self.bots[bot_id] = self._build_strategy(strategy, symbol, normalized_params)
        self._attach_runtime_metadata(self.bots[bot_id], bot_id=bot_id, strategy=strategy)
        await self._ensure_runtime_market_subscription(self.bots[bot_id])
        await self._start_runtime(bot_id, self.bots[bot_id])
        await self._release_unused_symbol_spot_streams()

        ok = await self.repository.set_status(bot_id=bot_id, status="RUNNING", last_error=None)
        if not ok:
            raise KeyError("Bot no encontrado")

        fresh = await self.repository.get_bot(bot_id)
        if fresh is None:
            raise KeyError("Bot no encontrado")
        fresh["runtimeActive"] = True
        self._log(bot_id, "STARTED", {"symbol": symbol, "strategy": strategy})
        return fresh

    async def pause_bot(self, bot_id: str) -> dict[str, Any]:
        if self.repository is None:
            raise RuntimeError("Bot repository is not configured")

        runtime = self.bots.pop(bot_id, None)
        if runtime is not None:
            await self._stop_runtime(bot_id, runtime)
            await self._release_unused_symbol_spot_streams()
        ok = await self.repository.set_status(bot_id=bot_id, status="PAUSED", last_error=None)
        if not ok:
            raise KeyError("Bot no encontrado")

        row = await self.repository.get_bot(bot_id)
        if row is None:
            raise KeyError("Bot no encontrado")
        row["runtimeActive"] = False
        self._log(bot_id, "PAUSED", {})
        return row

    async def stop_bot(self, bot_id: str) -> dict[str, Any]:
        if self.repository is None:
            raise RuntimeError("Bot repository is not configured")

        runtime = self.bots.pop(bot_id, None)
        if runtime is not None:
            await self._stop_runtime(bot_id, runtime)
            await self._release_unused_symbol_spot_streams()
        ok = await self.repository.set_status(bot_id=bot_id, status="STOPPED", last_error=None)
        if not ok:
            raise KeyError("Bot no encontrado")

        row = await self.repository.get_bot(bot_id)
        if row is None:
            raise KeyError("Bot no encontrado")
        row["runtimeActive"] = False
        self._log(bot_id, "STOPPED", {})
        return row

    async def list_strategies(self) -> list[dict[str, Any]]:
        return list_strategies_metadata()

    async def list_trade_registry(
        self,
        *,
        limit: int = 200,
        bot_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return await self.repository.list_trades(
            limit=limit,
            bot_id=bot_id,
            symbol=symbol,
            status=status,
        )


    async def start_active_bots_market_stream(self) -> dict[str, Any]:
        await self.client.ensure_ready()

        active_symbols = sorted(
            {
                getattr(bot, "symbol", "").strip().upper()
                for bot in self.bots.values()
                if getattr(bot, "symbol", "").strip()
            }
        )

        for bot in self.bots.values():
            await self._ensure_runtime_market_subscription(bot)

        subscribed = sorted(
            [
                {"symbol": s, "timeframe": tf}
                for (s, tf) in self._subscribed_symbol_timeframes
            ],
            key=lambda x: (x["symbol"], x["timeframe"]),
        )

        return {
            "active_bots": len(self.bots),
            "active_symbols": active_symbols,
            "subscribed": subscribed,
        }
    async def runtime_health_snapshot(self) -> dict[str, Any]:
        active_symbols = sorted(
            {
                getattr(bot, "symbol", "").strip().upper()
                for bot in self.bots.values()
                if getattr(bot, "symbol", "").strip()
            }
        )
        subscribed = sorted(
            [
                {"symbol": s, "timeframe": tf}
                for (s, tf) in self._subscribed_symbol_timeframes
            ],
            key=lambda x: (x["symbol"], x["timeframe"]),
        )
        return {
            "active_bots": len(self.bots),
            "active_symbols": active_symbols,
            "active_symbols_count": len(active_symbols),
            "subscribed_symbol_timeframes_count": len(self._subscribed_symbol_timeframes),
            "subscribed": subscribed,
            "event_consumers_count": sum(len(v) for v in self._bot_event_tasks.values()),
            "event_topics_subscribed_count": sum(len(v) for v in self._bot_event_subscriptions.values()),
            "hub_spot_streams_count": len(self._hub_symbol_listeners),
            "hub_spot_stream_symbols": sorted(self._hub_symbol_listeners.keys()),
        }
    async def start(self) -> None:
        await self.client.connect()
        if self.repository is not None:
            await self.repository.ensure_schema()
        await self.restore_from_storage()

    async def restore_from_storage(self) -> None:
        if self.repository is None:
            return

        records = await self.repository.list_active_bots()
        for row in records:
            bot_id = str(row["id"])
            status = str(row.get("status") or "PAUSED")
            if status != "RUNNING":
                continue
            if bot_id in self.bots:
                continue

            try:
                strategy = str(row.get("strategy") or "peak_dip").strip()
                symbol = str(row.get("instrument") or "").upper()
                params = row.get("params") if isinstance(row.get("params"), dict) else {}
                normalized_params = self._validate_and_normalize_params(strategy, params)
                self.bots[bot_id] = self._build_strategy(strategy, symbol, normalized_params)
                self._attach_runtime_metadata(self.bots[bot_id], bot_id=bot_id, strategy=strategy)
                await self._ensure_runtime_market_subscription(self.bots[bot_id])
                await self._start_runtime(bot_id, self.bots[bot_id])
                await self._release_unused_symbol_spot_streams()
                self._log(bot_id, "RESTORED", {"symbol": symbol, "strategy": strategy})
            except Exception as exc:
                await self.repository.set_status(bot_id=bot_id, status="ERROR", last_error=str(exc))
                self._log(bot_id, "RESTORE_ERROR", {"error": str(exc)})

    async def dispatch_h4(self, candle: dict) -> None:
        symbol = str(candle.get("symbol") or "").upper()
        if not symbol:
            return
        await self._dispatch_symbol_h4(symbol, candle)

    async def dispatch_m15(self, candle: dict) -> None:
        symbol = str(candle.get("symbol") or "").upper()
        if not symbol:
            return
        await self._dispatch_symbol_m15(symbol, candle)

    async def get_bot_logs(self, bot_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if self.repository is not None:
            row = await self.repository.get_bot(bot_id)
            if row is None:
                raise KeyError("Bot no encontrado")
            try:
                db_logs = await self.repository.list_logs(bot_id, limit=limit)
                if db_logs:
                    return db_logs
            except Exception:
                pass
        logs = list(self._logs.get(bot_id, deque()))
        if limit > 0:
            logs = logs[-limit:]
        return logs

    async def dry_run_bot(
        self,
        bot_id: str,
        *,
        side: str,
        entry: float | None = None,
    ) -> dict[str, Any]:
        if self.repository is None:
            raise RuntimeError("Bot repository is not configured")

        row = await self.repository.get_bot(bot_id)
        if row is None:
            raise KeyError("Bot no encontrado")

        strategy = str(row.get("strategy") or "peak_dip").strip()
        symbol = str(row.get("instrument") or "").upper()
        if not symbol:
            raise ValueError("Bot sin instrument configurado")

        normalized_side = side.strip().lower()
        if normalized_side not in ("buy", "sell"):
            raise ValueError("side debe ser buy o sell")

        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        normalized_params = self._validate_and_normalize_params(strategy, params)

        resolved_entry = float(entry) if entry is not None else float(await self.client.price(symbol))
        definition = get_strategy_definition(strategy)
        plan = definition.build_plan(symbol, normalized_params, normalized_side, resolved_entry)

        rr = None
        risk = abs(float(plan["entry"]) - float(plan["sl"]))
        reward = abs(float(plan["tp"]) - float(plan["entry"]))
        if risk > 0:
            rr = reward / risk

        result = {
            "bot_id": bot_id,
            "symbol": symbol,
            "strategy": strategy,
            "input": {"side": normalized_side, "entry": resolved_entry},
            "plan": {
                "side": plan["side"],
                "entry": plan["entry"],
                "sl": plan["sl"],
                "tp": plan["tp"],
                "rr": rr,
            },
            "used_market_price": entry is None,
        }
        self._log(bot_id, "DRY_RUN", result["input"])
        return result

    def _validate_and_normalize_params(
        self,
        strategy: str,
        strategy_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        definition = get_strategy_definition(strategy)
        return definition.normalize_params(strategy_params)

    def _build_strategy(
        self,
        strategy: str,
        symbol: str,
        params: dict[str, Any],
    ) -> Strategy:
        definition = get_strategy_definition(strategy)
        return definition.create_runtime(symbol, self.client, params)

    @staticmethod
    def _attach_runtime_metadata(runtime: Strategy, *, bot_id: str, strategy: str) -> None:
        setattr(runtime, "bot_id", bot_id)
        setattr(runtime, "strategy_id", strategy)

    async def _start_runtime(self, bot_id: str, runtime: Strategy) -> None:
        await self._start_bot_consumers(bot_id, runtime)

        hook = getattr(runtime, "on_start", None)
        if callable(hook):
            await hook()

    async def _stop_runtime(self, bot_id: str, runtime: Strategy) -> None:
        await self._stop_bot_consumers(bot_id)

        hook = getattr(runtime, "shutdown", None)
        if callable(hook):
            await hook()

    def _runtime_timeframes(self, runtime: Strategy) -> tuple[str, ...]:
        timeframes = getattr(runtime, "required_timeframes", None)
        if isinstance(timeframes, (list, tuple, set)):
            normalized = tuple(str(tf).strip().upper() for tf in timeframes if str(tf).strip())
            if normalized:
                return normalized
        return ("H4", "M15")

    def _bar_topic(self, symbol: str, timeframe: str) -> str:
        return f"bar_closed:{symbol.upper()}:{timeframe.upper()}"

    def _runtime_handler_for_timeframe(self, runtime: Strategy, timeframe: str):
        tf = timeframe.upper()
        if tf == "H4":
            return getattr(runtime, "on_h4_close", None)
        if tf == "M15":
            return getattr(runtime, "on_m15_close", None)
        if tf == "M5":
            return getattr(runtime, "on_m5_close", None)
        if tf == "M1":
            return getattr(runtime, "on_m1_close", None)
        return None

    async def _start_bot_consumers(self, bot_id: str, runtime: Strategy) -> None:
        await self._stop_bot_consumers(bot_id)

        symbol = str(getattr(runtime, "symbol", "")).strip().upper()
        if not symbol:
            return

        tasks: list[asyncio.Task] = []
        subs: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []

        for timeframe in self._runtime_timeframes(runtime):
            topic = self._bar_topic(symbol, timeframe)
            queue = await self.event_bus.subscribe(topic, maxsize=100)
            task = asyncio.create_task(
                self._consume_bot_topic(bot_id, runtime, timeframe, queue),
                name=f"bot-consumer-{bot_id}-{symbol}-{timeframe}",
            )
            tasks.append(task)
            subs.append((topic, queue))

        self._bot_event_tasks[bot_id] = tasks
        self._bot_event_subscriptions[bot_id] = subs

    async def _stop_bot_consumers(self, bot_id: str) -> None:
        tasks = self._bot_event_tasks.pop(bot_id, [])
        subs = self._bot_event_subscriptions.pop(bot_id, [])

        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        for topic, queue in subs:
            await self.event_bus.unsubscribe(topic, queue)

    async def _consume_bot_topic(
        self,
        bot_id: str,
        runtime: Strategy,
        timeframe: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        handler = self._runtime_handler_for_timeframe(runtime, timeframe)
        if not callable(handler):
            return

        while True:
            event = await queue.get()
            candle = event.get("candle") if isinstance(event, dict) else None
            if not isinstance(candle, dict):
                continue
            try:
                await handler(candle)
            except Exception as exc:
                self._log(bot_id, "RUNTIME_EVENT_ERROR", {"timeframe": timeframe, "error": str(exc)})

    async def _ensure_runtime_market_subscription(self, runtime: Strategy) -> None:
        symbol = str(getattr(runtime, "symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")

        for timeframe in self._runtime_timeframes(runtime):
            await self._ensure_symbol_timeframe_subscription(symbol, timeframe)

    async def _ensure_symbol_timeframe_subscription(self, symbol: str, timeframe: str) -> None:
        normalized_symbol = symbol.strip().upper()
        normalized_tf = timeframe.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")

        key = (normalized_symbol, normalized_tf)
        if key in self._subscribed_symbol_timeframes:
            return

        if normalized_tf == "H4":
            async def on_h4(candle: dict) -> None:
                await self._dispatch_symbol_h4(normalized_symbol, candle)

            await self.client.subscribe_h4(normalized_symbol, on_h4)
        elif normalized_tf in ("M15", "M5", "M1"):
            await self._ensure_symbol_spot_stream(normalized_symbol)
        else:
            raise ValueError(f"timeframe no soportado para suscripcion: {normalized_tf}")

        self._subscribed_symbol_timeframes.add(key)

    async def _ensure_symbol_spot_stream(self, symbol: str) -> None:
        symbol_u = symbol.strip().upper()
        if not symbol_u:
            return
        if symbol_u in self._hub_symbol_listeners:
            return

        listener = await self.market_data_hub.subscribe_symbol(symbol_u)
        self._hub_symbol_listeners[symbol_u] = listener

    async def _release_unused_symbol_spot_streams(self) -> None:
        needed: set[str] = set()
        for runtime in self.bots.values():
            symbol = str(getattr(runtime, "symbol", "")).strip().upper()
            if not symbol:
                continue
            tfs = self._runtime_timeframes(runtime)
            if any(tf in ("M15", "M5", "M1") for tf in tfs):
                needed.add(symbol)

        for symbol in list(self._hub_symbol_listeners.keys()):
            if symbol in needed:
                continue
            listener = self._hub_symbol_listeners.pop(symbol, None)
            if listener is not None:
                await self.market_data_hub.unsubscribe_symbol(symbol, listener)

    async def _publish_bar_closed(self, symbol: str, timeframe: str, candle: dict) -> None:
        symbol_u = symbol.upper()
        tf_u = timeframe.upper()
        await self.market_data_hub.record_bar_close(symbol_u, tf_u, candle)
        topic = self._bar_topic(symbol_u, tf_u)
        await self.event_bus.publish(
            topic,
            {
                "type": "bar_closed",
                "symbol": symbol_u,
                "timeframe": tf_u,
                "candle": candle,
            },
        )

    async def _dispatch_symbol_h4(self, symbol: str, candle: dict) -> None:
        await self._publish_bar_closed(symbol, "H4", candle)

    async def _dispatch_symbol_m15(self, symbol: str, candle: dict) -> None:
        await self._publish_bar_closed(symbol, "M15", candle)

    async def _dispatch_symbol_m5(self, symbol: str, candle: dict) -> None:
        await self._publish_bar_closed(symbol, "M5", candle)

    async def _dispatch_symbol_m1(self, symbol: str, candle: dict) -> None:
        await self._publish_bar_closed(symbol, "M1", candle)
    def _log(self, bot_id: str, event: str, details: dict[str, Any]) -> None:
        payload = details or {}
        ts = datetime.now(timezone.utc)
        bucket = self._logs.setdefault(bot_id, deque(maxlen=500))
        bucket.append(
            {
                "time_utc": ts.isoformat(),
                "event": event,
                "details": payload,
            }
        )
        if self.repository is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._persist_log(bot_id=bot_id, event=event, details=payload, time_utc=ts))
            except RuntimeError:
                pass

    async def _persist_log(
        self,
        *,
        bot_id: str,
        event: str,
        details: dict[str, Any],
        time_utc: datetime,
    ) -> None:
        if self.repository is None:
            return
        try:
            await self.repository.create_log(
                bot_id=bot_id,
                event=event,
                details=details,
                time_utc=time_utc,
            )
        except Exception as exc:
            print(f"[BotManager] log persist failed for {bot_id}: {exc!r}")


























