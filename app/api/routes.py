from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from app.bots.manager import BotManager
from app.services.ctrader_client import cTraderClient
from app.services.market_data_hub import MarketDataHub, get_market_data_hub
from app.services.internal_event_bus import InternalEventBus, get_internal_event_bus


def get_bot_manager() -> BotManager:
    from app.main import bot_manager  # circular safe
    return bot_manager


def get_ctrader_client() -> cTraderClient:
    from app.main import client  # circular safe
    return client


router = APIRouter()
def get_market_data_hub_dep() -> MarketDataHub:
    return get_market_data_hub()


def get_internal_event_bus_dep() -> InternalEventBus:
    return get_internal_event_bus()


MAJOR_FX_PAIRS_27 = (
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF", "EURJPY",
    "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF", "GBPJPY",
    "AUDNZD", "AUDCAD", "AUDCHF", "AUDJPY",
    "NZDCAD", "NZDCHF", "NZDJPY",
    "CADCHF", "CADJPY",
)


def _extract_strategy_params(payload: dict) -> dict:
    raw = payload.get("strategyParams") or payload.get("strategy_params")
    params = dict(raw) if isinstance(raw, dict) else {}

    sl_points = payload.get("sl_points", payload.get("slPoints"))
    tp_points = payload.get("tp_points", payload.get("tpPoints"))
    volume = payload.get("volume", payload.get("volumeUnits"))
    if sl_points is not None:
        params["sl_points"] = sl_points
    if tp_points is not None:
        params["tp_points"] = tp_points
    if volume is not None:
        params["volume"] = volume
    return params


@router.get("/strategies")
async def list_strategies(manager: BotManager = Depends(get_bot_manager)):
    strategies = await manager.list_strategies()
    return {"count": len(strategies), "strategies": strategies}


@router.get("/bots")
async def list_bots(
    userId: str | None = None,
    user_id: str | None = None,
    manager: BotManager = Depends(get_bot_manager),
):
    effective_user_id = userId or user_id
    bots = await manager.list_bots(user_id=effective_user_id)
    return {"count": len(bots), "bots": bots}


@router.get("/bots/{bot_id}")
async def get_bot(bot_id: str, manager: BotManager = Depends(get_bot_manager)):
    bot = await manager.get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@router.post("/bots")
async def create_bot(payload: dict, manager: BotManager = Depends(get_bot_manager)):
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    account_id = payload.get("accountId") or payload.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId is required")

    strategy = str(payload.get("strategy", "")).strip()
    if not strategy:
        raise HTTPException(status_code=400, detail="strategy is required")
    user_id = payload.get("userId") or payload.get("user_id") or account_id
    strategy_params = _extract_strategy_params(payload)

    try:
        bot_id = await manager.create_bot(
            symbol=symbol,
            strategy=strategy,
            strategy_params=strategy_params,
            user_id=str(user_id).strip(),
            name=payload.get("name"),
            account_id=str(account_id).strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not create bot: {exc}") from exc

    return {"bot_id": bot_id}


@router.post("/bots/bulk")
async def create_bots_bulk(payload: dict, manager: BotManager = Depends(get_bot_manager)):
    raw_bots = payload.get("bots")
    if not isinstance(raw_bots, list) or not raw_bots:
        raise HTTPException(status_code=400, detail="bots must be a non-empty array")

    default_strategy = str(payload.get("strategy", "")).strip() or None
    default_account_id = payload.get("accountId") or payload.get("account_id")
    default_user_id = payload.get("userId") or payload.get("user_id") or default_account_id
    name_prefix = str(payload.get("namePrefix", "")).strip()
    auto_start = bool(payload.get("autoStart", False))

    created_count = 0
    started_count = 0
    failed_count = 0
    results: list[dict] = []

    for idx, item in enumerate(raw_bots):
        if not isinstance(item, dict):
            failed_count += 1
            results.append({"index": idx, "status": "error", "error": "each bot item must be an object"})
            continue

        symbol = str(item.get("symbol") or payload.get("symbol") or "").strip().upper()
        strategy = str(item.get("strategy") or default_strategy or "").strip()
        account_id = item.get("accountId") or item.get("account_id") or default_account_id
        user_id = item.get("userId") or item.get("user_id") or default_user_id or account_id

        if not symbol:
            failed_count += 1
            results.append({"index": idx, "status": "error", "error": "symbol is required", "symbol": ""})
            continue
        if not strategy:
            failed_count += 1
            results.append({"index": idx, "status": "error", "error": "strategy is required", "symbol": symbol})
            continue
        if not account_id:
            failed_count += 1
            results.append({"index": idx, "status": "error", "error": "accountId is required", "symbol": symbol})
            continue

        merged = dict(payload)
        merged.update(item)
        strategy_params = _extract_strategy_params(merged)

        raw_name = item.get("name")
        if raw_name is None and name_prefix:
            raw_name = f"{name_prefix}_{symbol}_{idx + 1}"

        try:
            bot_id = await manager.create_bot(
                symbol=symbol,
                strategy=strategy,
                strategy_params=strategy_params,
                user_id=str(user_id).strip(),
                name=raw_name,
                account_id=str(account_id).strip(),
            )
            created_count += 1

            started = False
            if auto_start:
                await manager.start_bot(bot_id)
                started = True
                started_count += 1

            results.append(
                {
                    "index": idx,
                    "status": "ok",
                    "bot_id": bot_id,
                    "symbol": symbol,
                    "strategy": strategy,
                    "started": started,
                }
            )
        except Exception as exc:
            failed_count += 1
            results.append(
                {
                    "index": idx,
                    "status": "error",
                    "symbol": symbol,
                    "strategy": strategy,
                    "error": str(exc),
                }
            )

    return {
        "total": len(raw_bots),
        "created_count": created_count,
        "started_count": started_count,
        "failed_count": failed_count,
        "results": results,
    }


@router.patch("/bots/{bot_id}")
async def update_bot(bot_id: str, payload: dict, manager: BotManager = Depends(get_bot_manager)):
    try:
        updated = await manager.update_bot(
            bot_id=bot_id,
            name=payload.get("name"),
            symbol=payload.get("symbol"),
            account_id=payload.get("accountId") if "accountId" in payload else payload.get("account_id"),
            strategy=payload.get("strategy"),
            strategy_params=payload.get("strategyParams") or payload.get("strategy_params"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not update bot: {exc}") from exc

    return updated


@router.delete("/bots/{bot_id}")
async def delete_bot(bot_id: str, manager: BotManager = Depends(get_bot_manager)):
    try:
        deleted = await manager.delete_bot(bot_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete bot: {exc}") from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {"deleted": True, "bot_id": bot_id}


@router.post("/bots/{bot_id}/start")
async def start_bot(bot_id: str, manager: BotManager = Depends(get_bot_manager)):
    try:
        bot = await manager.start_bot(bot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start bot: {exc}") from exc
    return bot


@router.post("/bots/{bot_id}/resume")
async def resume_bot(bot_id: str, manager: BotManager = Depends(get_bot_manager)):
    return await start_bot(bot_id, manager)


@router.post("/bots/{bot_id}/pause")
async def pause_bot(bot_id: str, manager: BotManager = Depends(get_bot_manager)):
    try:
        bot = await manager.pause_bot(bot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not pause bot: {exc}") from exc
    return bot


@router.post("/bots/{bot_id}/stop")
async def stop_bot(bot_id: str, manager: BotManager = Depends(get_bot_manager)):
    try:
        bot = await manager.stop_bot(bot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not stop bot: {exc}") from exc
    return bot


@router.get("/bots/{bot_id}/logs")
async def get_bot_logs(
    bot_id: str,
    limit: int = 100,
    manager: BotManager = Depends(get_bot_manager),
):
    try:
        logs = await manager.get_bot_logs(bot_id, limit=max(1, min(limit, 500)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not get logs: {exc}") from exc
    return {"bot_id": bot_id, "count": len(logs), "logs": logs}


@router.post("/bots/{bot_id}/dry-run")
async def dry_run_bot(bot_id: str, payload: dict, manager: BotManager = Depends(get_bot_manager)):
    side = str(payload.get("side", "")).strip().lower()
    if not side:
        raise HTTPException(status_code=400, detail="side is required")

    raw_entry = payload.get("entry")
    entry = None if raw_entry is None else float(raw_entry)

    try:
        result = await manager.dry_run_bot(bot_id, side=side, entry=entry)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not dry-run bot: {exc}") from exc
    return result



@router.get("/market/symbols/majors")
async def list_major_fx_pairs():
    return {"count": len(MAJOR_FX_PAIRS_27), "symbols": list(MAJOR_FX_PAIRS_27)}


@router.post("/market/stream/active-bots/start")
async def start_active_bots_market_stream(manager: BotManager = Depends(get_bot_manager)):
    try:
        result = await manager.start_active_bots_market_stream()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start active-bots market stream: {exc}") from exc
    return result


@router.get("/market/connection-status")
async def get_market_connection_status(
    client: cTraderClient = Depends(get_ctrader_client),
):
    return await client.connection_status()

@router.get("/market/price")
async def get_market_price(
    symbol: str,
    hub: MarketDataHub = Depends(get_market_data_hub_dep),
):
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    try:
        quote = await hub.get_or_fetch_price(symbol)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch price for {symbol}: {exc}",
        ) from exc

    return quote

@router.get("/market/prices")
async def get_market_prices(
    symbols: str,
    hub: MarketDataHub = Depends(get_market_data_hub_dep),
):
    requested_symbols = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not requested_symbols:
        raise HTTPException(status_code=400, detail="symbols is required")

    prices = await hub.snapshot_many(requested_symbols)

    # Fallback puntual para simbolos aun sin cache.
    for idx, item in enumerate(prices):
        if item.get("price") is not None:
            continue
        try:
            prices[idx] = await hub.get_or_fetch_price(str(item.get("symbol") or ""))
        except Exception as exc:
            prices[idx] = {"symbol": item.get("symbol"), "error": str(exc)}

    return {"count": len(requested_symbols), "prices": prices}





@router.get("/market/hub/status")
async def get_market_hub_status(
    hub: MarketDataHub = Depends(get_market_data_hub_dep),
):
    return await hub.stats()


@router.get("/market/hub/symbol/{symbol}")
async def get_market_hub_symbol_state(
    symbol: str,
    hub: MarketDataHub = Depends(get_market_data_hub_dep),
):
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    return await hub.get_symbol_state(symbol)





@router.get("/market/events/status")
async def get_market_events_status(
    bus: InternalEventBus = Depends(get_internal_event_bus_dep),
):
    return await bus.stats()

@router.get("/market/runtime/health")
async def get_market_runtime_health(
    manager: BotManager = Depends(get_bot_manager),
    client: cTraderClient = Depends(get_ctrader_client),
    hub: MarketDataHub = Depends(get_market_data_hub_dep),
    bus: InternalEventBus = Depends(get_internal_event_bus_dep),
):
    connection = await client.connection_status()
    hub_stats = await hub.stats()
    events = await bus.stats()
    bots = await manager.runtime_health_snapshot()

    ready = bool(connection.get("ready") and connection.get("state") == "READY")
    return {
        "ready": ready,
        "connection": connection,
        "hub": hub_stats,
        "events": events,
        "bots": bots,
    }

@router.websocket("/ws/prices")
async def ws_prices(
    websocket: WebSocket,
    hub: MarketDataHub = Depends(get_market_data_hub_dep),
):
    await websocket.accept()

    raw_symbols = websocket.query_params.get("symbols", "")
    symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
    if not symbols:
        await websocket.send_json(
            {
                "type": "error",
                "detail": "symbols query param is required. Example: /ws/prices?symbols=EURUSD,GBPUSD",
            }
        )
        await websocket.close(code=1008)
        return

    interval_raw = websocket.query_params.get("interval", "1")
    try:
        interval = float(interval_raw)
    except ValueError:
        interval = 1.0
    interval = max(0.5, min(interval, 30.0))

    subscriptions: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []

    try:
        for symbol in symbols:
            listener = await hub.subscribe_symbol(symbol)
            subscriptions.append((symbol, listener))

        while True:
            prices = await hub.snapshot_many(symbols)
            await websocket.send_json(
                {
                    "type": "prices",
                    "count": len(symbols),
                    "symbols": symbols,
                    "prices": prices,
                }
            )
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "detail": str(exc)})
        except Exception:
            pass
        await websocket.close(code=1011)
    finally:
        for symbol, listener in subscriptions:
            try:
                await hub.unsubscribe_symbol(symbol, listener)
            except Exception:
                pass






