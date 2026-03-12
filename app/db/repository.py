from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sys
import uuid
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None

from app.config import get_settings


@dataclass
class BotCreateInput:
    id: str
    name: str
    instrument: str
    user_id: str
    strategy: str
    strategy_params: dict[str, Any]
    account_id: str | None = None


class BotRepository:
    def __init__(self, database_url: str | None = None) -> None:
        settings = get_settings()
        self._database_url = database_url or settings.DATABASE_URL
        # Psycopg async is not compatible with ProactorEventLoop on Windows.
        self._use_sync_driver = sys.platform == "win32"

    async def list_active_bots(self) -> list[dict[str, Any]]:
        if not self._database_url:
            return []
        self._ensure_driver(require_dict=True)

        query = """
            SELECT
                b."id",
                b."name",
                b."instrument",
                b."status",
                b."userId",
                b."accountId",
                COALESCE(b."strategy", cfg."strategy", 'peak_dip') AS "strategy",
                COALESCE(b."strategyParams", cfg."params", '{}'::jsonb) AS "params"
            FROM public."Bot" b
            LEFT JOIN public."BotStrategyConfig" cfg
              ON cfg."botId" = b."id"
            WHERE b."isDeleted" = FALSE
            ORDER BY b."createdAt" ASC
        """
        rows = await self._fetchall_dict(query)
        return [self._normalize_row(dict(row)) for row in rows]

    async def list_bots(self, user_id: str | None = None) -> list[dict[str, Any]]:
        if not self._database_url:
            return []
        self._ensure_driver(require_dict=True)

        query = """
            SELECT
                b."id",
                b."name",
                b."instrument",
                b."status",
                b."userId",
                b."accountId",
                b."riskPercent",
                b."maxOpenTrades",

                b."lastError",
                b."isDeleted",
                b."createdAt",
                b."updatedAt",
                COALESCE(b."strategy", cfg."strategy", 'peak_dip') AS "strategy",
                COALESCE(b."strategyParams", cfg."params", '{}'::jsonb) AS "params"
            FROM public."Bot" b
            LEFT JOIN public."BotStrategyConfig" cfg
              ON cfg."botId" = b."id"
            WHERE b."isDeleted" = FALSE
              AND (%(user_id)s::text IS NULL OR b."userId" = %(user_id)s)
            ORDER BY b."createdAt" DESC
        """
        rows = await self._fetchall_dict(query, {"user_id": user_id})
        return [self._normalize_row(dict(row)) for row in rows]

    async def get_bot(self, bot_id: str) -> dict[str, Any] | None:
        if not self._database_url:
            return None
        self._ensure_driver(require_dict=True)

        query = """
            SELECT
                b."id",
                b."name",
                b."instrument",
                b."status",
                b."userId",
                b."accountId",
                b."riskPercent",
                b."maxOpenTrades",

                b."lastError",
                b."isDeleted",
                b."createdAt",
                b."updatedAt",
                COALESCE(b."strategy", cfg."strategy", 'peak_dip') AS "strategy",
                COALESCE(b."strategyParams", cfg."params", '{}'::jsonb) AS "params"
            FROM public."Bot" b
            LEFT JOIN public."BotStrategyConfig" cfg
              ON cfg."botId" = b."id"
            WHERE b."id" = %(bot_id)s AND b."isDeleted" = FALSE
            LIMIT 1
        """
        row = await self._fetchone_dict(query, {"bot_id": bot_id})
        if not row:
            return None
        return self._normalize_row(dict(row))

    async def create_bot(self, payload: BotCreateInput) -> None:
        if not self._database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self._ensure_driver()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        bot_query = """
            INSERT INTO public."Bot" (
                "id",
                "name",
                "instrument",
                "accountId",
                "strategy",
                "strategyParams",
                "createdAt",
                "updatedAt",
                "userId"
            )
            VALUES (
                %(id)s,
                %(name)s,
                %(instrument)s,
                %(account_id)s,
                %(strategy)s,
                %(strategy_params)s::jsonb,
                %(created_at)s,
                %(updated_at)s,
                %(user_id)s
            )
        """
        cfg_query = """
            INSERT INTO public."BotStrategyConfig" (
                "botId",
                "strategy",
                "params",
                "createdAt",
                "updatedAt"
            )
            VALUES (
                %(bot_id)s,
                %(strategy)s,
                %(params)s::jsonb,
                %(created_at)s,
                %(updated_at)s
            )
        """
        bot_params = {
            "id": payload.id,
            "name": payload.name,
            "instrument": payload.instrument,
            "account_id": payload.account_id,
            "strategy": payload.strategy,
            "strategy_params": json.dumps(payload.strategy_params),
            "created_at": now,
            "updated_at": now,
            "user_id": payload.user_id,
        }
        cfg_params = {
            "bot_id": payload.id,
            "strategy": payload.strategy,
            "params": json.dumps(payload.strategy_params),
            "created_at": now,
            "updated_at": now,
        }

        if self._use_sync_driver:
            await asyncio.to_thread(self._create_bot_sync, bot_query, bot_params, cfg_query, cfg_params)
            return

        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(bot_query, bot_params)
                await cur.execute(cfg_query, cfg_params)
            await conn.commit()

    async def update_bot(
        self,
        *,
        bot_id: str,
        name: str,
        instrument: str,
        account_id: str | None,
        strategy: str,
        strategy_params: dict[str, Any],
    ) -> bool:
        if not self._database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self._ensure_driver()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        bot_query = """
            UPDATE public."Bot"
            SET
                "name" = %(name)s,
                "instrument" = %(instrument)s,
                "accountId" = %(account_id)s,
                "strategy" = %(strategy)s,
                "strategyParams" = %(strategy_params)s::jsonb,
                "updatedAt" = %(updated_at)s
            WHERE "id" = %(bot_id)s AND "isDeleted" = FALSE
        """
        cfg_query = """
            INSERT INTO public."BotStrategyConfig" ("botId", "strategy", "params", "createdAt", "updatedAt")
            VALUES (%(bot_id)s, %(strategy)s, %(params)s::jsonb, %(created_at)s, %(updated_at)s)
            ON CONFLICT ("botId")
            DO UPDATE SET
                "strategy" = EXCLUDED."strategy",
                "params" = EXCLUDED."params",
                "updatedAt" = EXCLUDED."updatedAt"
        """
        bot_params = {
            "bot_id": bot_id,
            "name": name,
            "instrument": instrument,
            "account_id": account_id,
            "strategy": strategy,
            "strategy_params": json.dumps(strategy_params),
            "updated_at": now,
        }
        cfg_params = {
            "bot_id": bot_id,
            "strategy": strategy,
            "params": json.dumps(strategy_params),
            "created_at": now,
            "updated_at": now,
        }

        if self._use_sync_driver:
            return await asyncio.to_thread(self._update_bot_sync, bot_query, bot_params, cfg_query, cfg_params)

        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(bot_query, bot_params)
                if cur.rowcount == 0:
                    await conn.rollback()
                    return False
                await cur.execute(cfg_query, cfg_params)
            await conn.commit()
            return True

    async def set_status(self, *, bot_id: str, status: str, last_error: str | None = None) -> bool:
        if not self._database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self._ensure_driver()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        query = """
            UPDATE public."Bot"
            SET
                "status" = %(status)s::"BotStatus",
                "lastError" = %(last_error)s,
                "updatedAt" = %(updated_at)s
            WHERE "id" = %(bot_id)s AND "isDeleted" = FALSE
        """
        params = {
            "bot_id": bot_id,
            "status": status,
            "last_error": last_error,
            "updated_at": now,
        }
        updated_rows = await self._execute_write(query, params)
        return updated_rows > 0

    async def soft_delete_bot(self, bot_id: str) -> bool:
        if not self._database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self._ensure_driver()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        query = """
            UPDATE public."Bot"
            SET
                "isDeleted" = TRUE,
                "status" = 'STOPPED'::"BotStatus",
                "updatedAt" = %(updated_at)s
            WHERE "id" = %(bot_id)s AND "isDeleted" = FALSE
        """
        updated_rows = await self._execute_write(query, {"bot_id": bot_id, "updated_at": now})
        return updated_rows > 0

    async def create_log(
        self,
        *,
        bot_id: str,
        event: str,
        details: dict[str, Any],
        time_utc: datetime | None = None,
    ) -> None:
        if not self._database_url:
            return
        self._ensure_driver()

        ts = (time_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
        query = """
            INSERT INTO public."BotEventLog" (
                "id",
                "botId",
                "timeUtc",
                "event",
                "details"
            )
            VALUES (
                %(id)s,
                %(bot_id)s,
                %(time_utc)s,
                %(event)s,
                %(details)s::jsonb
            )
        """
        params = {
            "id": str(uuid.uuid4()),
            "bot_id": bot_id,
            "time_utc": ts,
            "event": event,
            "details": json.dumps(details or {}),
        }
        await self._execute_write(query, params)

    async def list_logs(self, bot_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not self._database_url:
            return []
        self._ensure_driver(require_dict=True)

        query = """
            SELECT
                l."id",
                l."botId",
                l."timeUtc",
                l."event",
                COALESCE(l."details", '{}'::jsonb) AS "details"
            FROM public."BotEventLog" l
            JOIN public."Bot" b ON b."id" = l."botId"
            WHERE l."botId" = %(bot_id)s
              AND b."isDeleted" = FALSE
            ORDER BY l."timeUtc" DESC
            LIMIT %(limit)s
        """
        rows = await self._fetchall_dict(query, {"bot_id": bot_id, "limit": max(1, limit)})
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_details = item.get("details")
            if isinstance(raw_details, str):
                try:
                    item["details"] = json.loads(raw_details)
                except json.JSONDecodeError:
                    item["details"] = {}
            elif raw_details is None:
                item["details"] = {}
            normalized.append(item)
        normalized.reverse()
        return normalized

    async def ensure_schema(self) -> None:
        if not self._database_url:
            return
        self._ensure_driver()

        query_add_strategy_col = """
            ALTER TABLE public."Bot"
            ADD COLUMN IF NOT EXISTS "strategy" text
        """
        query_add_strategy_params_col = """
            ALTER TABLE public."Bot"
            ADD COLUMN IF NOT EXISTS "strategyParams" jsonb NOT NULL DEFAULT '{}'::jsonb
        """
        query_backfill_strategy = """
            UPDATE public."Bot" b
            SET
                "strategy" = COALESCE(b."strategy", cfg."strategy", 'peak_dip'),
                "strategyParams" = COALESCE(b."strategyParams", cfg."params", '{}'::jsonb)
            FROM public."BotStrategyConfig" cfg
            WHERE cfg."botId" = b."id"
              AND (b."strategy" IS NULL OR b."strategyParams" IS NULL)
        """
        query_set_default_strategy = """
            UPDATE public."Bot"
            SET "strategy" = 'peak_dip'
            WHERE "strategy" IS NULL
        """
        query_strategy = """
            CREATE TABLE IF NOT EXISTS public."BotStrategyConfig" (
                "botId" text PRIMARY KEY REFERENCES public."Bot"("id") ON DELETE CASCADE,
                "strategy" text NOT NULL,
                "params" jsonb NOT NULL DEFAULT '{}'::jsonb,
                "createdAt" timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "updatedAt" timestamp without time zone NOT NULL
            )
        """
        query_logs = """
            CREATE TABLE IF NOT EXISTS public."BotEventLog" (
                "id" text PRIMARY KEY,
                "botId" text NOT NULL REFERENCES public."Bot"("id") ON DELETE CASCADE,
                "timeUtc" timestamp without time zone NOT NULL,
                "event" text NOT NULL,
                "details" jsonb NOT NULL DEFAULT '{}'::jsonb
            )
        """
        query_logs_idx = """
            CREATE INDEX IF NOT EXISTS idx_bot_event_log_bot_time
            ON public."BotEventLog" ("botId", "timeUtc" DESC)
        """

        if self._use_sync_driver:
            await asyncio.to_thread(
                self._ensure_schema_sync,
                query_add_strategy_col,
                query_add_strategy_params_col,
                query_strategy,
                query_backfill_strategy,
                query_set_default_strategy,
                query_logs,
                query_logs_idx,
            )
            return

        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(query_add_strategy_col)
                await cur.execute(query_add_strategy_params_col)
                await cur.execute(query_strategy)
                await cur.execute(query_backfill_strategy)
                await cur.execute(query_set_default_strategy)
                await cur.execute(query_logs)
                await cur.execute(query_logs_idx)
            await conn.commit()

    @staticmethod
    def _normalize_row(item: dict[str, Any]) -> dict[str, Any]:
        raw_params = item.get("params")
        if isinstance(raw_params, str):
            try:
                item["params"] = json.loads(raw_params)
            except json.JSONDecodeError:
                item["params"] = {}
        elif raw_params is None:
            item["params"] = {}
        # Legacy timeframe columns belong to old strategy plumbing and are not
        # part of the current strategy contract exposed by this API.
        item.pop("trendTimeframe", None)
        item.pop("signalTimeframe", None)
        return item

    def _ensure_driver(self, *, require_dict: bool = False) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed. Run: uv sync")
        if require_dict and dict_row is None:
            raise RuntimeError("psycopg rows support is not available")

    async def _fetchall_dict(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._use_sync_driver:
            return await asyncio.to_thread(self._fetchall_dict_sync, query, params)

        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params or {})
                return await cur.fetchall()

    async def _fetchone_dict(self, query: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self._use_sync_driver:
            return await asyncio.to_thread(self._fetchone_dict_sync, query, params)

        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params or {})
                return await cur.fetchone()

    async def _execute_write(self, query: str, params: dict[str, Any] | None = None) -> int:
        if self._use_sync_driver:
            return await asyncio.to_thread(self._execute_write_sync, query, params)

        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params or {})
                updated_rows = cur.rowcount
            await conn.commit()
            return updated_rows

    def _fetchall_dict_sync(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or {})
                return cur.fetchall()

    def _fetchone_dict_sync(self, query: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or {})
                return cur.fetchone()

    def _execute_write_sync(self, query: str, params: dict[str, Any] | None = None) -> int:
        with psycopg.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or {})
                updated_rows = cur.rowcount
            conn.commit()
            return updated_rows

    def _create_bot_sync(
        self,
        bot_query: str,
        bot_params: dict[str, Any],
        cfg_query: str,
        cfg_params: dict[str, Any],
    ) -> None:
        with psycopg.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(bot_query, bot_params)
                cur.execute(cfg_query, cfg_params)
            conn.commit()

    def _update_bot_sync(
        self,
        bot_query: str,
        bot_params: dict[str, Any],
        cfg_query: str,
        cfg_params: dict[str, Any],
    ) -> bool:
        with psycopg.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(bot_query, bot_params)
                if cur.rowcount == 0:
                    conn.rollback()
                    return False
                cur.execute(cfg_query, cfg_params)
            conn.commit()
            return True

    def _ensure_schema_sync(
        self,
        query_add_strategy_col: str,
        query_add_strategy_params_col: str,
        query_strategy: str,
        query_backfill_strategy: str,
        query_set_default_strategy: str,
        query_logs: str,
        query_logs_idx: str,
    ) -> None:
        with psycopg.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(query_add_strategy_col)
                cur.execute(query_add_strategy_params_col)
                cur.execute(query_strategy)
                cur.execute(query_backfill_strategy)
                cur.execute(query_set_default_strategy)
                cur.execute(query_logs)
                cur.execute(query_logs_idx)
            conn.commit()









