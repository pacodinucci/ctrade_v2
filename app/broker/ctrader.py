# app/broker/ctrader.py
from __future__ import annotations

from typing import Dict, Any, Optional, List

import httpx

from app.config import get_settings
from app.broker.base import ExecutionBroker, Side
from app.broker.ctrader_market_data import (
    get_current_price as md_get_current_price,
    get_open_positions as md_get_open_positions,
    has_open_position as md_has_open_position,
    open_market_order as md_open_market_order,
    close_position as md_close_position,
)


class CTraderBroker(ExecutionBroker):
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.CTRADER_API_BASE_URL.rstrip("/"),
            timeout=15.0,
        )

        self._access_token: Optional[str] = self.settings.CTRADER_ACCESS_TOKEN
        self._refresh_token: Optional[str] = self.settings.CTRADER_REFRESH_TOKEN

    async def _ensure_access_token(self) -> str:
        if not self._access_token:
            raise RuntimeError("No access token configured for cTrader")
        return self._access_token

    async def _authorized_headers(self) -> Dict[str, str]:
        token = await self._ensure_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _rest_auth_params(self) -> Dict[str, str]:
        """
        Parámetros comunes de auth para la API REST de Spotware.
        Usa ?oauth_token=... en lugar de Authorization: Bearer ...
        """
        token = await self._ensure_access_token()
        return {"oauth_token": token}

    # ---------- SOLO LECTURA ----------

    async def get_account_info(self) -> Dict[str, Any]:
        """
        Devuelve el objeto de cuenta completo correspondiente a
        CTRADER_TRADER_ACCOUNT_ID (accountId en la respuesta de Spotware).
        """
        params = await self._rest_auth_params()

        resp = await self._client.get("/connect/tradingaccounts", params=params)
        print("[cTrader] /connect/tradingaccounts →", resp.status_code)
        resp.raise_for_status()

        data = resp.json()
        accounts = data.get("data")

        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("No se encontraron cuentas en /connect/tradingaccounts")

        target_trader_id = str(self.settings.CTRADER_TRADER_ACCOUNT_ID)

        account_obj: Optional[Dict[str, Any]] = None
        for acc in accounts:
            acc_id = acc.get("accountId")
            if acc_id is not None and str(acc_id) == target_trader_id:
                account_obj = acc
                break

        if account_obj is None:
            # fallback opcional: buscar por accountNumber si tenés CTRADER_ACCOUNT_ID
            target_number = getattr(self.settings, "CTRADER_ACCOUNT_ID", None)
            if target_number is not None:
                for acc in accounts:
                    acc_number = acc.get("accountNumber")
                    if acc_number is not None and str(acc_number) == str(target_number):
                        account_obj = acc
                        break

        if account_obj is None:
            account_obj = accounts[0]
            print(
                f"[cTrader] ⚠ No se encontró accountId={target_trader_id}, "
                f"usando la primera cuenta: {account_obj}"
            )

        return account_obj

    async def get_account_balance(self) -> float:
        params = await self._rest_auth_params()

        resp = await self._client.get("/connect/tradingaccounts", params=params)
        print("[cTrader] /connect/tradingaccounts →", resp.status_code, resp.text)
        resp.raise_for_status()

        data = resp.json()
        accounts = data.get("data")

        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("No se encontraron cuentas en /connect/tradingaccounts")

        target_number = str(self.settings.CTRADER_ACCOUNT_ID)

        account_obj = None
        for acc in accounts:
            acc_number = acc.get("accountNumber")
            acc_id = acc.get("accountId")
            if (
                acc_number is not None and str(acc_number) == target_number
            ) or (acc_id is not None and str(acc_id) == target_number):
                account_obj = acc
                break

        if account_obj is None:
            account_obj = accounts[0]
            print(
                f"[cTrader] ⚠ No se encontró accountNumber/accountId = {target_number}, "
                f"usando la primera cuenta: {account_obj}"
            )

        balance = account_obj.get("balance")
        if balance is None:
            raise RuntimeError(f"No pude encontrar 'balance' en la cuenta: {account_obj}")

        return float(balance)

    async def get_current_price(self, symbol: str) -> float:
        """
        Precio actual real usando el servicio de market data (Open API).
        """
        price = await md_get_current_price(symbol)
        print(f"[cTrader] get_current_price({symbol}) -> {price}")
        return price

    # ---------- MÉTODOS “VACÍOS” PARA CUMPLIR LA INTERFAZ ----------

    async def open_market_order(
        self,
        symbol: str,
        side: Side,
        volume: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Por ahora ignoramos SL/TP server-side
        return await md_open_market_order(symbol, side, volume)

    async def close_position(
        self,
        position_id: int,
        volume: Optional[float] = None,
    ) -> Dict[str, Any]:
        return await md_close_position(position_id, volume)

    async def list_open_positions(self) -> List[Dict[str, Any]]:
        """
        Devuelve las posiciones abiertas usando Open API (ProtoOAReconcileReq).
        """
        return await md_get_open_positions()

    async def has_open_position(self, symbol: str, side: Side) -> bool:
        return await md_has_open_position(symbol, side)

    async def aclose(self) -> None:
        await self._client.aclose()

# Singleton para broker REST
_broker = CTraderBroker()


async def get_account_balance() -> float:
    """
    Helper global para obtener el balance actual de la cuenta cTrader.
    """
    return await _broker.get_account_balance()

async def get_account_info():
    return await _broker.get_account_info()

async def get_price(symbol: str):
    return await _broker.get_current_price(symbol)
