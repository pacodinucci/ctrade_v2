from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, DefaultDict


class InternalEventBus:
    """
    Event bus in-memory con topics.
    Cada subscriber recibe su propia queue.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: DefaultDict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    async def subscribe(self, topic: str, *, maxsize: int = 100) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers[topic].add(q)
        return q

    async def unsubscribe(self, topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            bucket = self._subscribers.get(topic)
            if not bucket:
                return
            bucket.discard(queue)
            if not bucket:
                self._subscribers.pop(topic, None)

    async def publish(self, topic: str, event: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subscribers.get(topic, set()))

        for q in targets:
            if q.full():
                try:
                    q.get_nowait()
                except Exception:
                    pass
            try:
                q.put_nowait(event)
            except Exception:
                pass

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            topics = sorted(self._subscribers.keys())
            return {
                "topics_count": len(topics),
                "topics": topics,
                "subscribers_count": sum(len(v) for v in self._subscribers.values()),
            }


_internal_event_bus = InternalEventBus()


def get_internal_event_bus() -> InternalEventBus:
    return _internal_event_bus
