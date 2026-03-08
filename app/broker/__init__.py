# app/broker/__init__.py
from functools import lru_cache

from app.broker.ctrader import CTraderBroker
from app.broker.base import ExecutionBroker


@lru_cache
def get_broker() -> ExecutionBroker:
    """
    Devuelve una instancia singleton de CTraderBroker.
    Si en el futuro tenés varios brokers, acá podés elegir cuál.
    """
    return CTraderBroker()
