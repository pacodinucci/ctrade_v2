from __future__ import annotations

import asyncio
from fastapi import FastAPI

from app.api.routes import router
from app.services.ctrader_client import cTraderClient
from app.bots.manager import BotManager

app = FastAPI()
client = cTraderClient()
bot_manager = BotManager(client)

app.include_router(router)


@app.on_event("startup")
async def startup() -> None:
    await bot_manager.start()

    async def fake_feed():
        while False:
            await asyncio.sleep(1)

    asyncio.create_task(fake_feed())


@app.get("/")
async def root():
    return {"status": "ok", "bots": await bot_manager.list_bots()}
