# app/broker/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal, Optional, Dict, Any, List

Side = Literal["buy", "sell"]


class ExecutionBroker(ABC):
    """
    Interfaz que debe implementar cualquier broker (cTrader, OANDA, etc.)
    Toda la lógica del bot (orders, trailing, manual, trades) depende de estas firmas.
    """

    # ---------- Info de cuenta ----------
    @abstractmethod
    async def get_account_balance(self) -> float:
        """Devuelve el balance disponible de la cuenta."""
        raise NotImplementedError

    # ---------- Precios ----------
    @abstractmethod
    async def get_current_price(self, symbol: str) -> float:
        """
        Devuelve el precio actual (mid/last).
        Luego se ajustará si queremos bid/ask según la lógica del bot.
        """
        raise NotImplementedError

    # ---------- Apertura de orden ----------
    @abstractmethod
    async def open_market_order(
        self,
        symbol: str,
        side: Side,
        volume: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Abre una orden a mercado.
        Devuelve un dict con:
          - position_id
          - entry_price
          - volume
          - stop_loss
          - raw (respuesta cruda del broker)
        """
        raise NotImplementedError

    # ---------- Cierre ----------
    @abstractmethod
    async def close_position(self, position_id: int) -> Dict[str, Any]:
        """Cierra una posición abierta por ID."""
        raise NotImplementedError

    # ---------- Consulta de posiciones ----------
    @abstractmethod
    async def has_open_position(self, symbol: str, side: Side) -> bool:
        """
        Devuelve True si ya hay una posición abierta para ese símbolo y lado.
        Esto se usa como candado en open_risked_market_order.
        """
        raise NotImplementedError

    @abstractmethod
    async def list_open_positions(self) -> List[Dict[str, Any]]:
        """
        Devuelve todas las posiciones abiertas de la cuenta.
        Se utiliza en:
          - routes_trades
          - lógica del bot avanzada
        """
        raise NotImplementedError
