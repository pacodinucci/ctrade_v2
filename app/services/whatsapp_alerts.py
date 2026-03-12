from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from typing import Optional

from app.config import get_settings


class WhatsAppAlertService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._cooldowns: dict[str, float] = {}

    def _provider(self) -> str:
        return str(self._settings.ALERTS_WHATSAPP_PROVIDER or "").strip().lower()

    def _is_enabled(self) -> bool:
        if not self._settings.ALERTS_WHATSAPP_ENABLED:
            return False

        provider = self._provider()
        if provider == "twilio":
            required = [
                self._settings.ALERTS_WHATSAPP_TO,
                self._settings.ALERTS_WHATSAPP_FROM,
                self._settings.TWILIO_ACCOUNT_SID,
                self._settings.TWILIO_AUTH_TOKEN,
            ]
            return all(bool((v or "").strip()) for v in required)

        if provider == "bot_whatsapp":
            required = [
                self._settings.ALERTS_WHATSAPP_TO,
                self._settings.BOT_WHATSAPP_WEBHOOK_URL,
            ]
            return all(bool((v or "").strip()) for v in required)

        return False

    def _allow(self, key: str) -> bool:
        cooldown_sec = max(0, int(self._settings.ALERTS_WHATSAPP_COOLDOWN_SEC))
        if cooldown_sec == 0:
            return True
        now = time.monotonic()
        prev = self._cooldowns.get(key, 0.0)
        if (now - prev) < cooldown_sec:
            return False
        self._cooldowns[key] = now
        return True

    async def notify_api_error(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        detail: str,
    ) -> None:
        if not self._is_enabled():
            return

        key = f"{method}:{path}:{status_code}"
        if not self._allow(key):
            return

        env = self._settings.ENV
        body = (
            f"[ctrade_v2][{env}] API error {status_code}\n"
            f"{method} {path}\n"
            f"detail: {detail[:600]}"
        )

        provider = self._provider()
        if provider == "twilio":
            await asyncio.to_thread(self._send_twilio_whatsapp, body)
            return
        if provider == "bot_whatsapp":
            await asyncio.to_thread(self._send_bot_whatsapp, body)
            return

    def _send_twilio_whatsapp(self, body: str) -> None:
        sid = str(self._settings.TWILIO_ACCOUNT_SID or "").strip()
        token = str(self._settings.TWILIO_AUTH_TOKEN or "").strip()
        to = str(self._settings.ALERTS_WHATSAPP_TO or "").strip()
        from_ = str(self._settings.ALERTS_WHATSAPP_FROM or "").strip()

        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        payload = urllib.parse.urlencode({"From": from_, "To": to, "Body": body}).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        credentials = (f"{sid}:{token}").encode("utf-8")
        import base64

        b64 = base64.b64encode(credentials).decode("ascii")
        req.add_header("Authorization", f"Basic {b64}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        with urllib.request.urlopen(req, timeout=int(self._settings.ALERTS_WHATSAPP_TIMEOUT_SEC)) as resp:  # noqa: S310
            if resp.status >= 300:
                raise RuntimeError(f"Twilio alert failed: status={resp.status}")

    def _send_bot_whatsapp(self, body: str) -> None:
        url = str(self._settings.BOT_WHATSAPP_WEBHOOK_URL or "").strip()
        token = str(self._settings.BOT_WHATSAPP_WEBHOOK_TOKEN or "").strip()
        to = str(self._settings.ALERTS_WHATSAPP_TO or "").strip()

        payload = {
            "to": to,
            "message": body,
            "channel": "whatsapp",
            "source": "ctrade_v2",
        }
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        with urllib.request.urlopen(req, timeout=int(self._settings.ALERTS_WHATSAPP_TIMEOUT_SEC)) as resp:  # noqa: S310
            if resp.status >= 300:
                raise RuntimeError(f"bot_whatsapp alert failed: status={resp.status}")


_whatsapp_alert_service: Optional[WhatsAppAlertService] = None


def get_whatsapp_alert_service() -> WhatsAppAlertService:
    global _whatsapp_alert_service
    if _whatsapp_alert_service is None:
        _whatsapp_alert_service = WhatsAppAlertService()
    return _whatsapp_alert_service
