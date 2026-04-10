from __future__ import annotations

import asyncio
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from app.api.routes import router
from app.api.routes_manual import router as manual_router
from app.bots.manager import BotManager
from app.db.repository import BotRepository
from app.services.ctrader_client import cTraderClient
from app.services.whatsapp_alerts import get_whatsapp_alert_service

# Psycopg async on Windows requires a selector-based event loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = FastAPI()
bot_repository = BotRepository()
client = cTraderClient(repository=bot_repository)
bot_manager = BotManager(client, repository=bot_repository)
alert_service = get_whatsapp_alert_service()

app.include_router(router)
app.include_router(manual_router)


@app.middleware("http")
async def alert_on_server_errors(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:
        try:
            await alert_service.notify_api_error(
                method=request.method,
                path=request.url.path,
                status_code=500,
                detail=str(exc),
            )
        except Exception:
            pass
        raise

    if response.status_code >= 500:
        try:
            await alert_service.notify_api_error(
                method=request.method,
                path=request.url.path,
                status_code=int(response.status_code),
                detail="HTTP 5xx response",
            )
        except Exception:
            pass
    return response


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


@app.post("/alerts/test-whatsapp")
async def alerts_test_whatsapp():
    try:
        await alert_service.notify_api_error(
            method="TEST",
            path="/alerts/test-whatsapp",
            status_code=500,
            detail="Manual test alert",
        )
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    return {"ok": True, "message": "Alert processed (si estaba configurado, se envio)."}


def run_dev_server() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
