# app/broker/ctrader_market_data.py
from __future__ import annotations

import asyncio
import threading
import time
from typing import Dict, Optional, Set, List, Any
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import pandas as pd

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages import OpenApiMessages_pb2 as OAMsg
from twisted.internet import reactor  # type: ignore

from app.config import get_settings

settings = get_settings()

# CTRADER_TRADER_ACCOUNT_ID = ctidTraderAccountId (accountId real)
CTID_TRADER_ACCOUNT_ID = int(settings.CTRADER_TRADER_ACCOUNT_ID)

@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    mid: float
    timestamp: float

TF_TO_PERIOD = {
    "M1": 1,   # ProtoOATrendbarPeriod.M1
    "M5": 5,   # ProtoOATrendbarPeriod.M5
    "M15": 7,  # ProtoOATrendbarPeriod.M15
    "M30": 8,  # ProtoOATrendbarPeriod.M30
    "H1": 9,   # ProtoOATrendbarPeriod.H1
    "H4": 10,  # ProtoOATrendbarPeriod.H4
    "D1": 12,  # ProtoOATrendbarPeriod.D1
}


class CTraderMarketDataService:
    """
    Servicio de market data usando cTrader Open API (Spotware).
    Solo se encarga de:
      - conectar
      - autenticarse
      - cargar símbolos
      - suscribirse a spots
      - exponer get_price(symbol)
    """


    def __init__(self) -> None:
        self._client: Optional[Client] = None

        # symbolName -> symbolId
        self._symbol_ids: Dict[str, int] = {}
        # symbolId -> symbolName
        self._id_to_symbol: Dict[int, str] = {}

        # digits (cantidad de decimales)
        self._symbol_digits: Dict[str, int] = {}
        # lot size por simbolo (ej FX comun: 100000 unidades por 1 lote)
        self._symbol_lot_size: Dict[str, int] = {}

        # symbolName -> último quote
        self._latest_quotes: Dict[str, Quote] = {}

        # símbolos a los que ya nos suscribimos
        self._subscribed: Set[str] = set()

        # lista de queues que escuchan ese símbolo
        self._listeners: Dict[str, List[asyncio.Queue[Quote]]] = {}

        # Evento para saber cuándo ya tenemos la lista de símbolos
        self._account_ready = asyncio.Event()
        self._symbols_ready = asyncio.Event()
        self._account_ready_flag = threading.Event()
        self._symbols_ready_flag = threading.Event()

        # futures esperando ejecución por clientOrderId (para órdenes nuevas)
        self._pending_orders: Dict[str, asyncio.Future] = {}
        self._last_order_error: Optional[Dict[str, Any]] = None

        # loop asyncio principal (thread FastAPI) para puentear callbacks de Twisted.
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None

        # Throttle de requests de trendbars para evitar rate limit al escalar simbolos.
        self._trendbars_lock = asyncio.Lock()
        self._trendbars_next_allowed_ts: float = 0.0
        self._trendbars_min_gap_sec: float = 1.00
        self._auth_lock = asyncio.Lock()
        self._last_auth_retry_ts: float = 0.0
        self._connection_state: str = "CONNECTING"
        self._last_state_error: Optional[str] = None
        self._deferred_timeout_count: int = 0
        self._deferred_error_count: int = 0
        self._last_deferred_error: Optional[str] = None
        self._last_rate_limit_error: Optional[str] = None
        self._rate_limit_error_count: int = 0
        self._log_cooldowns: Dict[str, float] = {}

        # Lanzamos el reactor de Twisted en un thread aparte
        self._start_reactor_thread()

    def _log_with_cooldown(self, key: str, message: str, *, interval_sec: float = 15.0) -> None:
        now = time.monotonic()
        prev = self._log_cooldowns.get(key, 0.0)
        if (now - prev) < interval_sec:
            return
        self._log_cooldowns[key] = now
        print(message)

    # ---------- Twisted / OpenAPI setup ----------

    def _start_reactor_thread(self) -> None:
        t = threading.Thread(target=self._run_reactor, daemon=True)
        t.start()

    def _run_reactor(self) -> None:
        client = Client(
            EndPoints.PROTOBUF_DEMO_HOST,
            EndPoints.PROTOBUF_PORT,
            TcpProtocol,
        )
        self._client = client

        client.setConnectedCallback(self._on_connected)
        client.setDisconnectedCallback(self._on_disconnected)
        client.setMessageReceivedCallback(self._on_message_received)

        client.startService()
        reactor.run(installSignalHandlers=False)  # type: ignore

    # ---------- Callbacks de conexión ----------

    def _clear_session_state(self) -> None:
        self._symbol_ids.clear()
        self._id_to_symbol.clear()
        self._symbol_digits.clear()
        self._subscribed.clear()
        self._latest_quotes.clear()
        self._account_ready_flag.clear()
        self._symbols_ready_flag.clear()

        loop = self._get_asyncio_loop()
        if loop is not None:
            loop.call_soon_threadsafe(self._account_ready.clear)
            loop.call_soon_threadsafe(self._symbols_ready.clear)

    def _mark_account_ready(self) -> None:
        self._account_ready_flag.set()
        loop = self._get_asyncio_loop()
        if loop is not None:
            loop.call_soon_threadsafe(self._account_ready.set)

    def _mark_symbols_ready(self) -> None:
        self._symbols_ready_flag.set()
        loop = self._get_asyncio_loop()
        if loop is not None:
            loop.call_soon_threadsafe(self._symbols_ready.set)

    
    def _set_connection_state(self, state: str, *, error: Optional[str] = None) -> None:
        self._connection_state = state
        if error is not None:
            self._last_state_error = error

    def get_connection_status(self) -> Dict[str, Any]:
        next_allowed_in = max(0.0, self._trendbars_next_allowed_ts - time.monotonic())
        return {
            "state": self._connection_state,
            "ready": (
                self._connection_state == "READY"
                and self._account_ready_flag.is_set()
                and self._symbols_ready_flag.is_set()
            ),
            "account_ready": self._account_ready_flag.is_set(),
            "symbols_ready": self._symbols_ready_flag.is_set(),
            "symbols_loaded": len(self._symbol_ids),
            "trendbars_min_gap_sec": self._trendbars_min_gap_sec,
            "trendbars_next_allowed_in_sec": round(next_allowed_in, 3),
            "rate_limit_error_count": self._rate_limit_error_count,
            "last_rate_limit_error": self._last_rate_limit_error,
            "deferred_timeout_count": self._deferred_timeout_count,
            "deferred_error_count": self._deferred_error_count,
            "last_deferred_error": self._last_deferred_error,
            "last_error": self._last_state_error,
        }

    async def wait_until_ready(self, timeout: float = 20.0) -> None:
        await self._ensure_symbols_loaded(timeout=timeout)

    def _on_connected(self, client: Client) -> None:
        print("[MD] Conectado al proxy DEMO (market data)")
        self._set_connection_state("CONNECTING")
        self._clear_session_state()

        req = OAMsg.ProtoOAApplicationAuthReq()
        req.clientId = settings.CTRADER_CLIENT_ID
        req.clientSecret = settings.CTRADER_CLIENT_SECRET

        d = client.send(req)
        d.addErrback(self._on_error)

    def _on_disconnected(self, client: Client, reason) -> None:
        print("[MD] Desconectado:", reason)
        self._set_connection_state("RECONNECTING", error=str(reason))
        self._clear_session_state()

    def _on_message_received(self, client: Client, message) -> None:
        decoded = Protobuf.extract(message)

        if isinstance(decoded, OAMsg.ProtoOAApplicationAuthRes):
            self._on_app_auth_res(client, decoded)

        elif isinstance(decoded, OAMsg.ProtoOAAccountAuthRes):
            self._on_account_auth_res(client, decoded)

        elif isinstance(decoded, OAMsg.ProtoOASymbolsListRes):
            self._on_symbols_list_res(client, decoded)

        elif isinstance(decoded, OAMsg.ProtoOASpotEvent):
            self._on_spot_event(decoded)

        elif isinstance(decoded, OAMsg.ProtoOAExecutionEvent):  
            self._on_execution_event(decoded)


        elif isinstance(decoded, OAMsg.ProtoOAOrderErrorEvent):
            self._on_order_error_event(decoded)
        elif isinstance(decoded, OAMsg.ProtoOAErrorRes):
            error_code = str(getattr(decoded, "errorCode", "") or "")
            description = str(getattr(decoded, "description", "") or "")
            if error_code == "REQUEST_FREQUENCY_EXCEEDED":
                self._rate_limit_error_count += 1
                self._last_rate_limit_error = datetime.now(timezone.utc).isoformat()
                self._log_with_cooldown(
                    "rate_limit",
                    (
                        "[MD] RATE LIMIT cTrader (REQUEST_FREQUENCY_EXCEEDED), "
                        f"count={self._rate_limit_error_count}, trendbars_gap={self._trendbars_min_gap_sec:.2f}s"
                    ),
                    interval_sec=10.0,
                )
                return

            self._log_with_cooldown(
                f"proto_error_{error_code}_{description[:60]}",
                f"[MD] ProtoOAErrorRes -> code={error_code}, description={description}",
                interval_sec=15.0,
            )

    def _on_error(self, failure) -> None:
        error_text = str(failure)
        self._last_deferred_error = error_text
        if "TimeoutError" in error_text:
            self._deferred_timeout_count += 1
            self._log_with_cooldown(
                "deferred_timeout",
                (
                    "[MD] Deferred timeout en Open API (throttled), "
                    f"count={self._deferred_timeout_count}"
                ),
                interval_sec=20.0,
            )
        else:
            self._deferred_error_count += 1
            self._log_with_cooldown(
                f"deferred_error_{error_text[:80]}",
                f"[MD] Error en mensaje (Deferred): {error_text}",
                interval_sec=20.0,
            )
        self._set_connection_state("ERROR", error=str(failure))

    # ---------- Flujos de auth ----------

    def _on_app_auth_res(
        self,
        client: Client,
        decoded: OAMsg.ProtoOAApplicationAuthRes,
    ) -> None:
        print("✅ [MD] Application AUTH OK")
        self._set_connection_state("APP_AUTHENTICATED")

        req = OAMsg.ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
        req.accessToken = settings.CTRADER_ACCESS_TOKEN

        d = client.send(req)
        d.addErrback(self._on_error)

    def _on_account_auth_res(
        self,
        client: Client,
        decoded: OAMsg.ProtoOAAccountAuthRes,
    ) -> None:
        print("[MD] Account AUTH OK, pidiendo lista de simbolos...")
        self._set_connection_state("ACCOUNT_AUTHENTICATED")
        self._mark_account_ready()

        req = OAMsg.ProtoOASymbolsListReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID

        d = client.send(req)
        d.addErrback(self._on_error)

    def _on_symbols_list_res(
        self,
        client: Client,
        decoded: OAMsg.ProtoOASymbolsListRes,
    ) -> None:
        print("[MD] Symbols list recibida")

        for s in decoded.symbol:
            name = getattr(s, "symbolName", None)
            sid = getattr(s, "symbolId", None)
            if name is None or sid is None:
                continue

            name_u = str(name).upper()
            self._symbol_ids[name_u] = int(sid)
            self._id_to_symbol[int(sid)] = name_u

            # 👉 digits por símbolo (para escalar precios SL/TP)
            digits = getattr(s, "digits", None)
            if digits is None:
                digits = getattr(s, "pipPosition", 5)
            try:
                self._symbol_digits[name_u] = int(digits)
            except Exception:
                self._symbol_digits[name_u] = 5  # fallback

            lot_size = getattr(s, "lotSize", None)
            try:
                self._symbol_lot_size[name_u] = int(lot_size) if lot_size is not None else 100000
            except Exception:
                self._symbol_lot_size[name_u] = 100000

        self._mark_symbols_ready()
        self._set_connection_state("READY")


    def _on_spot_event(self, decoded: OAMsg.ProtoOASpotEvent) -> None:
        symbol_id = decoded.symbolId
        raw_bid = decoded.bid
        raw_ask = decoded.ask
        ts = time.time()

        symbol_name = self._id_to_symbol.get(symbol_id)
        if symbol_name is None:
            return

        # cuántos decimales tiene este símbolo (EURUSD → 5, muchos JPY → 3, etc.)
        digits = self._symbol_digits.get(symbol_name, 5)
        scale = 10 ** digits

        prev_quote = self._latest_quotes.get(symbol_name)

        bid: Optional[float]
        ask: Optional[float]

        bid = (raw_bid / scale) if raw_bid and raw_bid > 0 else (prev_quote.bid if prev_quote else None)
        ask = (raw_ask / scale) if raw_ask and raw_ask > 0 else (prev_quote.ask if prev_quote else None)

        # Algunos eventos traen solo una punta. Evitamos mids partidos.
        if bid is None and ask is None:
            return
        if bid is None:
            bid = ask
        if ask is None:
            ask = bid

        mid = (bid + ask) / 2.0

        quote = Quote(
            symbol=symbol_name,
            bid=float(bid),
            ask=float(ask),
            mid=float(mid),
            timestamp=ts,
        )

        # 1️⃣ actualizar cache
        self._latest_quotes[symbol_name] = quote

        # 2️⃣ notificar listeners
        listeners = self._listeners.get(symbol_name, [])
        if not listeners:
            return

        loop = self._get_asyncio_loop()
        if loop is None:
            for q in listeners:
                try:
                    q.put_nowait(quote)
                except Exception as e:
                    print(f"[MD] Error push quote a listener (no loop): {e!r}")
            return

        for q in listeners:
            loop.call_soon_threadsafe(q.put_nowait, quote)



    def _on_execution_event(self, decoded: OAMsg.ProtoOAExecutionEvent) -> None:
        """
        Maneja ProtoOAExecutionEvent para matchear con una orden enviada
        via clientOrderId y completar el Future correspondiente.
        """
        order = getattr(decoded, "order", None)
        client_order_id = getattr(order, "clientOrderId", None) if order is not None else None
        if not client_order_id:
            return

        execution_type = getattr(decoded, "executionType", None)
        error_code = getattr(decoded, "errorCode", None)
        is_rejected = (execution_type == 7) or bool(error_code)  # ORDER_REJECTED = 7

        position = getattr(decoded, "position", None)
        deal = getattr(decoded, "deal", None)

        position_id = (
            getattr(position, "positionId", None)
            or getattr(order, "positionId", None)
            or getattr(deal, "positionId", None)
        )
        price = (
            getattr(deal, "executionPrice", None)
            or getattr(order, "executionPrice", None)
            or getattr(position, "price", None)
        )
        volume = (
            getattr(deal, "filledVolume", None)
            or getattr(order, "executedVolume", None)
            or getattr(getattr(position, "tradeData", None), "volume", None)
        )
        side = (
            getattr(deal, "tradeSide", None)
            or getattr(getattr(order, "tradeData", None), "tradeSide", None)
            or getattr(getattr(position, "tradeData", None), "tradeSide", None)
        )
        timestamp = (
            getattr(deal, "executionTimestamp", None)
            or getattr(getattr(position, "tradeData", None), "openTimestamp", None)
            or getattr(order, "utcLastUpdateTimestamp", None)
        )

        if is_rejected:
            msg = (
                f"Orden rechazada por cTrader (clientOrderId={client_order_id}, "
                f"executionType={execution_type}, errorCode={error_code})"
            )
            self._resolve_pending_order(client_order_id, error=RuntimeError(msg))
            return

        result = {
            "position_id": position_id,
            "price": price,
            "volume": volume,
            "side": side,
            "timestamp": timestamp,
            "execution_type": execution_type,
            "error_code": error_code,
        }
        self._resolve_pending_order(client_order_id, result=result)

    def _on_order_error_event(self, decoded: OAMsg.ProtoOAOrderErrorEvent) -> None:
        error_code = getattr(decoded, "errorCode", None)
        description = getattr(decoded, "description", None)
        order_id = getattr(decoded, "orderId", None)
        position_id = getattr(decoded, "positionId", None)
        print(
            "[MD] ProtoOAOrderErrorEvent -> "
            f"errorCode={error_code}, description={description}, orderId={order_id}, positionId={position_id}"
        )
        self._last_order_error = {
            "ts": time.time(),
            "errorCode": str(error_code) if error_code is not None else None,
            "description": str(description) if description is not None else None,
            "orderId": int(order_id) if order_id else None,
            "positionId": int(position_id) if position_id else None,
        }

    def _resolve_pending_order(
        self,
        client_order_id: str,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        fut = self._pending_orders.pop(client_order_id, None)
        if fut is None:
            print("[MD] Evento de orden sin future registrado:", client_order_id)
            return

        loop = self._get_asyncio_loop()
        if loop is None:
            if fut.done():
                return
            if error is not None:
                fut.set_exception(error)
            else:
                fut.set_result(result)
            return

        def _finish() -> None:
            if fut.done():
                return
            if error is not None:
                fut.set_exception(error)
            else:
                fut.set_result(result)

        loop.call_soon_threadsafe(_finish)

    # ---------- Helpers async ----------

    def _get_asyncio_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        try:
            loop = asyncio.get_running_loop()
            self._async_loop = loop
            return loop
        except RuntimeError:
            return self._async_loop

    def _safe_set_future_result(self, fut: asyncio.Future, value: Any) -> None:
        if fut.done():
            return
        fut.set_result(value)

    def _safe_set_future_exception(self, fut: asyncio.Future, exc: Exception) -> None:
        if fut.done():
            return
        fut.set_exception(exc)

    async def _throttle_trendbar_request(self) -> None:
        now = time.monotonic()
        wait = self._trendbars_next_allowed_ts - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._trendbars_next_allowed_ts = time.monotonic() + self._trendbars_min_gap_sec

    async def _request_account_auth_retry(self, reason: str) -> None:
        await self._ensure_client_ready(timeout=5.0)
        if self._client is None:
            return

        async with self._auth_lock:
            now = time.monotonic()
            if (now - self._last_auth_retry_ts) < 2.0:
                return
            self._last_auth_retry_ts = now

            print(f"[MD] Reintentando Account AUTH ({reason})")
            req = OAMsg.ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
            req.accessToken = settings.CTRADER_ACCESS_TOKEN
            d = self._client.send(req)
            d.addErrback(self._on_error)

    async def _request_symbols_list_retry(self, reason: str) -> None:
        await self._ensure_client_ready(timeout=5.0)
        if self._client is None:
            return

        async with self._auth_lock:
            now = time.monotonic()
            if (now - self._last_auth_retry_ts) < 2.0:
                return
            self._last_auth_retry_ts = now

            print(f"[MD] Reintentando SymbolsList ({reason})")
            req = OAMsg.ProtoOASymbolsListReq()
            req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
            d = self._client.send(req)
            d.addErrback(self._on_error)

    async def _ensure_client_ready(self, timeout: float = 5.0) -> None:
        self._async_loop = asyncio.get_running_loop()
        """
        Espera a que el cliente OpenAPI esté inicializado por el thread de Twisted.
        """
        if self._client is not None:
            return

        elapsed = 0.0
        interval = 0.05

        while elapsed < timeout:
            if self._client is not None:
                return
            await asyncio.sleep(interval)
            elapsed += interval

        raise RuntimeError("Timeout esperando inicialización del cliente OpenAPI")


    async def _ensure_symbols_loaded(self, timeout: float = 12.0) -> None:
        # Primero nos aseguramos de que el cliente exista
        await self._ensure_client_ready(timeout=timeout)

        elapsed = 0.0
        interval = 0.1
        retried_auth = False

        while elapsed < timeout:
            if self._account_ready_flag.is_set():
                break
            if not retried_auth:
                await self._request_account_auth_retry("account_not_ready")
                retried_auth = True
            await asyncio.sleep(interval)
            elapsed += interval
        else:
            raise RuntimeError("Timeout esperando Account AUTH en cTrader Open API")

        if self._symbol_ids and self._symbols_ready_flag.is_set():
            return

        elapsed = 0.0
        retried_symbols = False
        while elapsed < timeout:
            if self._symbols_ready_flag.is_set() and self._symbol_ids:
                return
            if not retried_symbols:
                await self._request_symbols_list_retry("symbols_not_ready")
                retried_symbols = True
            await asyncio.sleep(interval)
            elapsed += interval

        raise RuntimeError("Timeout esperando lista de simbolos desde cTrader Open API")


    async def _subscribe_symbol_if_needed(self, symbol: str) -> None:
        symbol_u = symbol.upper()

        await self._ensure_symbols_loaded()

        if symbol_u not in self._symbol_ids:
            raise RuntimeError(f"Símbolo {symbol_u} no encontrado en lista de símbolos")

        if symbol_u in self._subscribed:
            return

        symbol_id = self._symbol_ids[symbol_u]

        if self._client is None:
            raise RuntimeError("Cliente OpenAPI aún no inicializado")

        req = OAMsg.ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
        req.symbolId.append(symbol_id)
        req.subscribeToSpotTimestamp = False

        d = self._client.send(req)
        d.addErrback(self._on_error)

        self._subscribed.add(symbol_u)
        print(f"📡 [MD] Suscripto a spots de {symbol_u} (symbolId={symbol_id})")

    def get_last_quote(self, symbol: str) -> Optional[Quote]:
        symbol_u = symbol.upper()
        return self._latest_quotes.get(symbol_u)

    def get_last_bid(self, symbol: str) -> Optional[float]:
        q = self.get_last_quote(symbol)
        return q.bid if q else None

    def get_last_ask(self, symbol: str) -> Optional[float]:
        q = self.get_last_quote(symbol)
        return q.ask if q else None

    def get_last_mid(self, symbol: str) -> Optional[float]:
        q = self.get_last_quote(symbol)
        return q.mid if q else None
    
    def get_last_quote(self, symbol: str) -> Optional[Quote]:
        symbol_u = symbol.upper()
        return self._latest_quotes.get(symbol_u)

    def get_last_bid(self, symbol: str) -> Optional[float]:
        q = self.get_last_quote(symbol)
        return q.bid if q else None

    def get_last_ask(self, symbol: str) -> Optional[float]:
        q = self.get_last_quote(symbol)
        return q.ask if q else None

    def get_last_mid(self, symbol: str) -> Optional[float]:
        q = self.get_last_quote(symbol)
        return q.mid if q else None


    def _to_volume_units(self, symbol: str, volume: float) -> int:
        raw = float(volume)
        if raw <= 0:
            raise ValueError("volume debe ser mayor a 0")

        # v1-compat: volume se recibe en unidades Open API y se envia tal cual.
        units = int(raw)
        if units <= 0:
            raise ValueError("volume invalido: usar unidades enteras (ej: 100, 1000, 100000)")

        return units

    def _normalize_price_for_symbol(self, symbol: str, price: float) -> float:
        symbol_u = symbol.upper()
        digits = int(self._symbol_digits.get(symbol_u, 5))
        digits = max(0, min(digits, 10))
        q = Decimal("1").scaleb(-digits)
        normalized = Decimal(str(price)).quantize(q, rounding=ROUND_HALF_UP)
        return float(format(normalized, f".{digits}f"))

    async def get_price(self, symbol: str, timeout: float = 5.0) -> float:
        symbol_u = symbol.upper()

        await self._subscribe_symbol_if_needed(symbol_u)

        elapsed = 0.0
        interval = 0.05

        while elapsed < timeout:
            q = self.get_last_quote(symbol_u)
            if q:
                bid = q.bid if q.bid and q.bid > 0 else None
                ask = q.ask if q.ask and q.ask > 0 else None

                if bid is not None and ask is not None:
                    return (bid + ask) / 2.0
                if bid is not None:
                    return bid
                if ask is not None:
                    return ask
                if q.mid and q.mid > 0:
                    return q.mid

            await asyncio.sleep(interval)
            elapsed += interval

        raise RuntimeError(f"Timeout esperando spot de {symbol_u} desde cTrader Open API")

    
    async def get_open_positions(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """
        Devuelve un snapshot de posiciones abiertas usando ProtoOAReconcileReq.
        """
        if self._client is None:
            raise RuntimeError("Cliente OpenAPI aun no inicializado")

        await self._ensure_symbols_loaded()

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        req = OAMsg.ProtoOAReconcileReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID

        def _on_reconcile(message):
            try:
                decoded = Protobuf.extract(message)
                if isinstance(decoded, OAMsg.ProtoOAErrorRes):
                    msg = (
                        f"Error en reconcile ({decoded.errorCode}): "
                        f"{getattr(decoded, 'description', None) or 'sin descripcion'}"
                    )
                    loop.call_soon_threadsafe(self._safe_set_future_exception, future, RuntimeError(msg))
                    return

                positions_list: List[Dict[str, Any]] = []
                for p in getattr(decoded, "position", []):
                    position_id = getattr(p, "positionId", None)
                    open_price = getattr(p, "openPrice", None) or getattr(p, "price", None)
                    stop_loss = getattr(p, "stopLoss", None)
                    take_profit = getattr(p, "takeProfit", None)

                    trade_data = getattr(p, "tradeData", None)
                    symbol_id = getattr(trade_data, "symbolId", None) if trade_data is not None else None
                    volume = getattr(trade_data, "volume", None) if trade_data is not None else None
                    trade_side = getattr(trade_data, "tradeSide", None) if trade_data is not None else None
                    open_timestamp = getattr(trade_data, "openTimestamp", None) if trade_data is not None else None

                    symbol_name = self._id_to_symbol.get(int(symbol_id)) if symbol_id is not None else None
                    positions_list.append(
                        {
                            "position_id": int(position_id) if position_id is not None else None,
                            "symbol_id": int(symbol_id) if symbol_id is not None else None,
                            "symbol": symbol_name,
                            "volume": int(volume) if volume is not None else None,
                            "trade_side": int(trade_side) if trade_side is not None else None,
                            "open_price": float(open_price) if open_price is not None else None,
                            "stop_loss": float(stop_loss) if stop_loss is not None else None,
                            "take_profit": float(take_profit) if take_profit is not None else None,
                            "stops": bool(stop_loss is not None and take_profit is not None),
                            "open_timestamp": int(open_timestamp) if open_timestamp is not None else None,
                        }
                    )

                loop.call_soon_threadsafe(self._safe_set_future_result, future, positions_list)
            except Exception as e:
                loop.call_soon_threadsafe(self._safe_set_future_exception, future, e)

        def _on_error_deferred(failure):
            loop.call_soon_threadsafe(
                self._safe_set_future_exception,
                future,
                RuntimeError(f"Error en ProtoOAReconcileReq: {failure!r}"),
            )

        d = self._client.send(req)
        d.addCallback(_on_reconcile)
        d.addErrback(_on_error_deferred)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout esperando ProtoOAReconcileRes con posiciones abiertas")

    async def get_trendbars(
        self,
        symbol: str,
        timeframe: str,
        count: int = 200,
        timeout: float = 15.0,
    ) -> pd.DataFrame:
        """
        Devuelve un DataFrame con columnas: time, open, high, low, close.
        """
        symbol_u = symbol.upper()

        await self._ensure_symbols_loaded()

        if symbol_u not in self._symbol_ids:
            raise RuntimeError(f"Simbolo {symbol_u} no encontrado en lista de simbolos")
        if timeframe not in TF_TO_PERIOD:
            raise RuntimeError(f"Timeframe no soportado: {timeframe}")

        symbol_id = self._symbol_ids[symbol_u]
        period = TF_TO_PERIOD[timeframe]

        now = datetime.now(timezone.utc)
        from_ts = int((now - timedelta(days=40)).timestamp() * 1000)
        to_ts = int(now.timestamp() * 1000)

        req = OAMsg.ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
        req.symbolId = symbol_id
        req.period = period
        req.fromTimestamp = from_ts
        req.toTimestamp = to_ts

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def _on_trendbars(message):
            try:
                decoded = Protobuf.extract(message)

                if isinstance(decoded, OAMsg.ProtoOAErrorRes):
                    error_code = str(getattr(decoded, "errorCode", "") or "")
                    if "REQUEST_FREQUENCY_EXCEEDED" in error_code:
                        self._trendbars_min_gap_sec = min(5.0, self._trendbars_min_gap_sec + 0.5)

                    msg = (
                        f"Error trendbars ({decoded.errorCode}): "
                        f"{getattr(decoded, 'description', None) or 'sin descripcion'}"
                    )
                    loop.call_soon_threadsafe(self._safe_set_future_exception, fut, RuntimeError(msg))
                    return

                bars = getattr(decoded, "trendbar", [])
                data = []
                for b in bars:
                    if hasattr(b, "utcTimestampInMinutes"):
                        ts_sec = b.utcTimestampInMinutes * 60
                        ts = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
                    elif hasattr(b, "utcTimestamp"):
                        ts = datetime.fromtimestamp(b.utcTimestamp / 1000, tz=timezone.utc)
                    else:
                        ts = datetime.now(timezone.utc)

                    low_raw = b.low
                    open_raw = low_raw + b.deltaOpen
                    close_raw = low_raw + b.deltaClose
                    high_raw = low_raw + b.deltaHigh
                    scale = 10**5

                    data.append(
                        {
                            "time": ts,
                            "open": open_raw / scale,
                            "high": high_raw / scale,
                            "low": low_raw / scale,
                            "close": close_raw / scale,
                        }
                    )

                if not data:
                    empty = pd.DataFrame(columns=["time", "open", "high", "low", "close"])
                    loop.call_soon_threadsafe(self._safe_set_future_result, fut, empty)
                    return

                df = pd.DataFrame(data).sort_values("time").reset_index(drop=True)
                if len(df) > count:
                    df = df.iloc[-count:].reset_index(drop=True)

                loop.call_soon_threadsafe(self._safe_set_future_result, fut, df)
            except Exception as e:
                loop.call_soon_threadsafe(self._safe_set_future_exception, fut, e)

        def _on_error_deferred(failure):
            loop.call_soon_threadsafe(
                self._safe_set_future_exception,
                fut,
                RuntimeError(f"Error en ProtoOAGetTrendbarsReq: {failure!r}"),
            )

        async with self._trendbars_lock:
            await self._throttle_trendbar_request()
            d = self._client.send(req)
            d.addCallback(_on_trendbars)
            d.addErrback(_on_error_deferred)

            try:
                result = await asyncio.wait_for(fut, timeout=timeout)
                # Si responde bien, relajamos lentamente el gap hasta 1s.
                self._trendbars_min_gap_sec = max(1.0, self._trendbars_min_gap_sec - 0.1)
                return result
            except asyncio.TimeoutError:
                self._trendbars_min_gap_sec = min(5.0, self._trendbars_min_gap_sec + 0.5)
                raise RuntimeError("Timeout esperando trendbars desde cTrader Open API")
    async def has_open_position(
        self,
        symbol: str,
        side: Optional[str] = None,  # "buy" | "sell" | None
    ) -> bool:
        """
        Devuelve True si hay al menos una posición abierta para ese símbolo.
        Si se pasa `side`, además debe coincidir el lado:

        - side="buy"  -> trade_side == 1
        - side="sell" -> trade_side == 2
        """
        positions = await self.get_open_positions()

        symbol_u = symbol.upper()
        desired_side = side.lower() if side else None

        for pos in positions:
            pos_symbol = str(pos.get("symbol") or "").upper()
            if pos_symbol != symbol_u:
                continue

            # Si no filtramos por lado, con que haya una posición del símbolo alcanza
            if desired_side is None:
                return True

            trade_side = pos.get("trade_side")  # 1 = BUY, 2 = SELL
            if trade_side is None:
                continue

            if trade_side == 1 and desired_side == "buy":
                return True
            if trade_side == 2 and desired_side == "sell":
                return True

        return False
    

    async def open_market_order(
        self,
        symbol: str,
        side: str,          # "buy" | "sell"
        volume: float,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """
        Abre una orden de mercado REAL vía Open API.

        - Enviamos ProtoOANewOrderReq.
        - Registramos un Future por clientOrderId para capturar ProtoOAExecutionEvent.
        - Intentamos esperar hasta `timeout` segundos.
        - Si hay timeout, buscamos la posición recién abierta con ProtoOAReconcileReq.
        """
        if self._client is None:
            raise RuntimeError("Cliente OpenAPI aún no inicializado")

        symbol_u = symbol.upper()
        await self._ensure_symbols_loaded()

        if symbol_u not in self._symbol_ids:
            raise RuntimeError(f"Símbolo {symbol_u} no encontrado en lista de símbolos")

        symbol_id = self._symbol_ids[symbol_u]
        volume_units = self._to_volume_units(symbol_u, volume)

        req = OAMsg.ProtoOANewOrderReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
        req.symbolId = symbol_id
        req.volume = int(volume_units)   # v1-compat: unidades directas
        req.orderType = 1          # 1 = MARKET

        # 1 = BUY, 2 = SELL
        if side.lower() == "buy":
            req.tradeSide = 1
            desired_trade_side = 1
        else:
            req.tradeSide = 2
            desired_trade_side = 2

        client_order_id = str(int(time.time() * 1000))
        req.clientOrderId = client_order_id

        print(
            f"[MD] Enviando MARKET order -> symbol={symbol_u}, "
            f"side={side}, volume={volume}, units={volume_units}, symbolId={symbol_id}, clientOrderId={client_order_id}"
        )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_orders[client_order_id] = fut

        d = self._client.send(req)

        def _on_new_order_response(message):
            try:
                decoded = Protobuf.extract(message)
                if isinstance(decoded, OAMsg.ProtoOAErrorRes):
                    msg = (
                        f"Error al enviar orden ({decoded.errorCode}): "
                        f"{getattr(decoded, 'description', None) or 'sin descripcion'}"
                    )
                    self._resolve_pending_order(client_order_id, error=RuntimeError(msg))
            except Exception:
                pass

        def _on_new_order_errback(failure):
            self._resolve_pending_order(
                client_order_id,
                error=RuntimeError(f"Fallo envio de orden: {failure!r}"),
            )
            self._on_error(failure)

        d.addCallback(_on_new_order_response)
        d.addErrback(_on_new_order_errback)

        try:
            # 🔹 Camino ideal: llega el ExecutionEvent
            result = await asyncio.wait_for(fut, timeout=timeout)

            position_id = result.get("position_id")
            price = result.get("price")
            executed_volume = result.get("volume")

            return {
                "position_id": position_id,
                "entry_price": price,
                "volume": executed_volume,
                "side": side,
                "raw": result,
            }

        except asyncio.TimeoutError:
            # 🔹 Plan B: no llegó el ExecutionEvent, intentamos identificar la posición
            print(
                "[MD] ⚠ Timeout esperando ExecutionEvent; intentando identificar posición vía Reconcile"
            )
            self._pending_orders.pop(client_order_id, None)
            last_err = self._last_order_error
            if last_err and (time.time() - float(last_err.get("ts") or 0)) <= (timeout + 1.5):
                code = last_err.get("errorCode") or "UNKNOWN"
                desc = last_err.get("description") or "sin descripcion"
                raise RuntimeError(f"Orden rechazada por cTrader: {code} - {desc}")

            try:
                positions = await self.get_open_positions()

                # Filtramos por símbolo y lado
                candidates = [
                    p
                    for p in positions
                    if str(p.get("symbol") or "").upper() == symbol_u
                    and p.get("trade_side") == desired_trade_side
                ]

                if candidates:
                    # Tomamos la más reciente por open_timestamp (si existe)
                    def key_pos(p: Dict[str, Any]):
                        ts = p.get("open_timestamp")
                        return ts if isinstance(ts, int) else 0

                    last_pos = max(candidates, key=key_pos)

                    position_id = last_pos.get("position_id")
                    price = last_pos.get("open_price")
                    executed_volume = last_pos.get("volume")

                    print(
                        f"[MD] Posición identificada por reconcile → "
                        f"position_id={position_id}, price={price}, volume={executed_volume}"
                    )

                    return {
                        "position_id": position_id,
                        "entry_price": price,
                        "volume": executed_volume,
                        "side": side,
                        "raw": {
                            "identified_via": "reconcile",
                            "position": last_pos,
                        },
                    }

                # Si no encontramos nada, volvemos al fallback "sent"
                print("[MD] ⚠ No se encontró posición coincidente en Reconcile")

            except Exception as e:
                print("[MD] ⚠ Error intentando identificar posición vía Reconcile:", repr(e))

            # Fallback final: sabemos que se envió, pero sin position_id
            return {
                "position_id": None,
                "entry_price": None,
                "volume": volume,
                "side": side,
                "raw": {
                    "sent": True,
                    "symbol": symbol_u,
                    "symbolId": symbol_id,
                    "volume": volume,
                    "side": side,
                    "clientOrderId": client_order_id,
                },
            }

        
    async def close_position(
        self,
        position_id: int,
        volume: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Cierra una posición vía ProtoOAClosePositionReq.

        - position_id: el positionId real de cTrader.
        - volume:
            * None → buscamos la posición y cerramos TODO el volumen.
            * valor numérico → cantidad a cerrar (int Open API).
        """
        if self._client is None:
            raise RuntimeError("Cliente OpenAPI aún no inicializado")

        # Nos aseguramos de que la sesión está fully inicializada
        await self._ensure_symbols_loaded()

        # Si no nos pasan volumen, buscamos la posición y usamos su volumen total
        if volume is None:
            positions = await self.get_open_positions()
            target = None
            for p in positions:
                if p.get("position_id") == position_id:
                    target = p
                    break

            if target is None:
                raise RuntimeError(
                    f"No se encontró la posición {position_id} entre las posiciones abiertas"
                )

            vol = target.get("volume")
            if vol is None:
                raise RuntimeError(
                    f"No se encontró 'volume' para la posición {position_id} en Reconcile"
                )

            volume = float(vol)

        req = OAMsg.ProtoOAClosePositionReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
        req.positionId = int(position_id)
        req.volume = int(volume)  # ahora siempre > 0

        print(
            f"[MD] Enviando CLOSE position → positionId={position_id}, volume={req.volume}"
        )

        d = self._client.send(req)
        d.addErrback(self._on_error)

        # De momento no esperamos ExecutionEvent de cierre
        return {
            "sent": True,
            "positionId": position_id,
            "volume": volume,
        }
    
    async def set_position_sl_tp(
        self,
        symbol: str,
        position_id: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Modifica SL/TP de una posición vía ProtoOAAmendPositionSLTPReq.

        - symbol: solo para logging (los precios son absolutos, en float).
        - position_id: id real de cTrader.
        - stop_loss / take_profit: precios absolutos (ej. 4257.93).
        """
        if self._client is None:
            raise RuntimeError("Cliente OpenAPI aún no inicializado")

        # Nos aseguramos de que ya tenemos sesión y símbolos cargados
        await self._ensure_symbols_loaded()

        symbol_u = symbol.upper()

        # ⚠️ OJO: en esta request los precios van como float absolutos,
        # NO hay que escalarlos por digits.
        req = OAMsg.ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = CTID_TRADER_ACCOUNT_ID
        req.positionId = int(position_id)

        normalized_sl = self._normalize_price_for_symbol(symbol_u, float(stop_loss)) if stop_loss is not None else None
        normalized_tp = self._normalize_price_for_symbol(symbol_u, float(take_profit)) if take_profit is not None else None

        if normalized_sl is not None:
            req.stopLoss = normalized_sl

        if normalized_tp is not None:
            req.takeProfit = normalized_tp

        print(
            f"[MD] Enviando AMEND SL/TP → positionId={position_id}, "
            f"symbol={symbol_u}, SL={normalized_sl}, TP={normalized_tp}"
        )

        d = self._client.send(req)
        d.addErrback(self._on_error)

        # De momento no esperamos ExecutionEvent específico
        return {
            "sent": True,
            "positionId": position_id,
            "symbol": symbol_u,
            "stopLoss": normalized_sl,
            "takeProfit": normalized_tp,
        }

    async def subscribe(self, symbol: str) -> asyncio.Queue[Quote]:
        """
        Crea una cola de quotes para ese símbolo y la registra como listener.
        """
        symbol_u = symbol.upper()

        # Aseguramos que el símbolo existe y estamos suscriptos en OpenAPI
        await self._subscribe_symbol_if_needed(symbol_u)

        q: asyncio.Queue[Quote] = asyncio.Queue()
        if symbol_u not in self._listeners:
            self._listeners[symbol_u] = []

        self._listeners[symbol_u].append(q)
        print(f"[MD] Listener agregado para {symbol_u}. Total: {len(self._listeners[symbol_u])}")
        return q

    async def unsubscribe(self, symbol: str, queue: asyncio.Queue[Quote]) -> None:
        """
        Elimina una cola de quotes de la lista de listeners del símbolo.
        """
        symbol_u = symbol.upper()
        queues = self._listeners.get(symbol_u)
        if not queues:
            return

        try:
            queues.remove(queue)
            print(f"[MD] Listener removido para {symbol_u}. Restan: {len(queues)}")
        except ValueError:
            # La queue ya no estaba en la lista
            pass

        if not queues:
            # Podrías opcionalmente desuscribirte de OpenAPI aquí.
            # Por ahora solo limpiamos el dict.
            self._listeners.pop(symbol_u, None)






# Singleton global
_market_data_service = CTraderMarketDataService()

def get_connection_status() -> Dict[str, Any]:
    return _market_data_service.get_connection_status()

async def wait_until_ready(timeout: float = 20.0) -> None:
    await _market_data_service.wait_until_ready(timeout=timeout)

async def get_current_price(symbol: str) -> float:
    """
    Helper que usa el singleton de market data.
    """
    return await _market_data_service.get_price(symbol)

async def get_open_positions() -> List[Dict[str, Any]]:
    """
    Helper global para obtener posiciones abiertas usando el singleton.
    """
    return await _market_data_service.get_open_positions()

async def has_open_position(
    symbol: str,
    side: Optional[str] = None,
) -> bool:
    """
    Helper global para saber si hay una posición abierta para un símbolo (y lado opcional).
    """
    return await _market_data_service.has_open_position(symbol, side)

async def open_market_order(symbol: str, side: str, volume: float) -> Dict[str, Any]:
    """
    Helper global para abrir una orden de mercado usando el singleton.
    """
    return await _market_data_service.open_market_order(symbol, side, volume)

async def close_position(
    position_id: int,
    volume: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Helper global para cerrar una posición usando el singleton.
    """
    return await _market_data_service.close_position(position_id, volume)

async def subscribe_quotes(symbol: str) -> asyncio.Queue[Quote]:
    return await _market_data_service.subscribe(symbol)

async def unsubscribe_quotes(symbol: str, queue: asyncio.Queue[Quote]) -> None:
    await _market_data_service.unsubscribe(symbol, queue)

async def get_trendbars(symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
    return await _market_data_service.get_trendbars(symbol, timeframe, count)

async def set_position_sl_tp(
    symbol: str,
    position_id: int,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Helper global para modificar SL/TP de una posición usando el singleton.
    """
    return await _market_data_service.set_position_sl_tp(
        symbol=symbol,
        position_id=position_id,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )






















