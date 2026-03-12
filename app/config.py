from functools import lru_cache
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

EnvType = Literal["dev", "prod"]
CTraderEnvironment = Literal["demo", "live"]


class Settings(BaseSettings):
    APP_NAME: str = "cTrader Bot API"
    ENV: EnvType = "dev"
    DEBUG: bool = True

    CTRADER_CLIENT_ID: str
    CTRADER_CLIENT_SECRET: str
    CTRADER_REDIRECT_URI: str

    CTRADER_ACCOUNT_ID: int
    CTRADER_TRADER_ACCOUNT_ID: int

    CTRADER_ENV: CTraderEnvironment = "demo"
    CTRADER_API_BASE_URL: str = "https://api.spotware.com"

    CTRADER_ACCESS_TOKEN: Optional[str] = None
    CTRADER_REFRESH_TOKEN: Optional[str] = None

    DATABASE_URL: Optional[str] = None

    ALERTS_WHATSAPP_ENABLED: bool = False
    ALERTS_WHATSAPP_PROVIDER: str = "twilio"
    ALERTS_WHATSAPP_TO: Optional[str] = None
    ALERTS_WHATSAPP_FROM: Optional[str] = None
    ALERTS_WHATSAPP_COOLDOWN_SEC: int = 120
    ALERTS_WHATSAPP_TIMEOUT_SEC: int = 10

    BOT_WHATSAPP_WEBHOOK_URL: Optional[str] = None
    BOT_WHATSAPP_WEBHOOK_TOKEN: Optional[str] = None

    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
