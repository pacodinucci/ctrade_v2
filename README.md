# ctrade_v2

Backend limpio para gestionar bots de trading conectados a cTrader. Mantiene la lógica core (conexión a cTrader, apertura/cierre de operaciones) y permite asociar distintas estrategias a cada bot.

## Estructura
- `app/config.py`: carga variables de entorno (`.env`).
- `app/services/ctrader_client.py`: wrapper para la API de cTrader (suscripciones y órdenes).
- `app/strategies/`:
  - `base.py`: contrato común para estrategias en tiempo real.
  - `peak_dip/`: implementación modular (detector H4, entrada M15, manager de trades) basada en la estrategia compartida.
- `app/bots/manager.py`: orquestador de bots ↔ estrategias.
- `app/api/routes.py`: endpoints HTTP (crear bot, listar, iniciar/detener).
- `app/main.py`: instancia FastAPI.

## Requisitos
```
python >= 3.11
uv or pipx (recomendado) / pip
```

## Variables de entorno
Copia `.env.example` a `.env` y completá los valores reales.

```
APP_NAME=cTrader Bot API
ENV=dev
DEBUG=true

CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_REDIRECT_URI=http://localhost:3000/callback
CTRADER_ACCOUNT_ID=...
CTRADER_TRADER_ACCOUNT_ID=...
CTRADER_ENV=demo
CTRADER_API_BASE_URL=https://api.spotware.com
CTRADER_ACCESS_TOKEN=...
CTRADER_REFRESH_TOKEN=...

DATABASE_URL=postgresql://user:pass@host:5432/dbname?sslmode=require
```

## Instalación rápida
```
cd ctrade_v2
uv sync   # o: pip install -r requirements.txt (si preferís pip)
uv run dev  # equivalente a: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Endpoints iniciales
- `POST /bots` → crea bot con estrategia.
- `GET /bots` → lista bots.
- `POST /bots/{bot_id}/start` / `.../stop`

Las estrategias viven en `app/strategies`. Peak/Dip ya está modularizada; para agregar otras, seguí el patrón de `peak_dip`.


Endpoint adicional: GET /market/price?symbol=EURUSD devuelve precio actual del instrumento.
Endpoint adicional: GET /market/prices?symbols=EURUSD,GBPUSD devuelve precios por lote.
