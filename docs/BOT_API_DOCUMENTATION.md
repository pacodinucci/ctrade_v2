# ctrade_v2 - Documentacion de Bot API

## 1. Objetivo
Esta aplicacion es una API backend en FastAPI para:
- Gestionar bots de trading (crear, editar, iniciar, pausar, detener, eliminar).
- Ejecutar estrategias sobre cTrader.
- Consultar precios de mercado.
- Persistir configuracion/estado/logs en Postgres (Neon) para recuperacion ante caidas.

## 2. Stack y componentes
- Framework API: FastAPI
- WebSocket runtime: uvicorn con soporte websocket (`uvicorn[standard]` o `websockets`/`wsproto`)
- Broker/market data: cTrader Open API + REST Spotware
- DB: Postgres (Neon)
- Driver DB: psycopg
  - Linux/macOS: async psycopg
  - Windows: fallback sync en threadpool (por incompatibilidad de psycopg async con ProactorEventLoop)

Estructura principal:
- `app/main.py`: arranque FastAPI, dependencias globales, startup.
- `app/api/routes.py`: endpoints HTTP.
- `app/bots/manager.py`: orquestacion de bots, ciclo de vida, validaciones por estrategia, dry-run, suscripcion por simbolo.
- `app/db/repository.py`: acceso a DB (bots, estrategia, estados, logs).
- `app/services/ctrader_client.py`: fachada de ejecucion/trading y fallback de market data (H4 polling).
- `app/services/market_data_hub.py`: cache de quotes por simbolo + bar buffers M1/M5/M15/H4.
- `app/services/internal_event_bus.py`: event bus interno (`bar_closed:{symbol}:{timeframe}`).
- `app/services/bar_builder.py`: construccion de velas M1 y derivacion M5/M15 desde ticks.
- `app/utils/time.py`: normalizacion de timestamps a UTC (naive/aware-safe).
- `app/strategies/registry.py`: registro central de estrategias (metadata + validacion + runtime + plan).
- `app/strategies/peak_dip/*`: logica de estrategia actual.

## 3. Modelo funcional
Un bot es:
- Una instancia de una estrategia.
- Con parametros propios de esa estrategia.
- Asociado a un instrumento (`symbol`).
- Asociado a una cuenta (`accountId`).
- Asociado a un usuario (`userId`, opcional en create: si falta se usa `accountId`).

Flujo normal:
1. Front consulta `GET /strategies`.
2. Front elige estrategia y arma payload.
3. API valida y persiste en DB.
4. API inicia/pausa/detiene bot por endpoint de ciclo de vida.
5. En restart, la API restaura bots con estado `RUNNING`.

## 4. Estrategias soportadas
Actualmente:
- `peak_dip` (setup H4 + entrada M15)
- `peak_dip_m5_m1` (setup M5 + entrada M1, version rapida para stress test)
- `fast_test` (apertura/cierre rapido para testing de integracion)

Parametros de `peak_dip`:
- `volume` (float >= 100000, requerido, en unidades Open API directas)
- `sl_points` (int > 0)
- `tp_points` (int > 0)

Parametros de `fast_test`:
- `side` (`buy` | `sell`, default `buy`)
- `volume` (float >= 100000, default `100000`, en unidades Open API directas)
- `sl_points` (int > 0, default `30`)
- `tp_points` (int > 0, default `30`)
- `open_interval_sec` (float >= 2, default `20`)
- `hold_sec` (float >= 1, default `8`)
- `max_cycles` (int >= 0, default `0`, infinito)

Endpoint de metadata:
- `GET /strategies`

### 4.1 Logica exacta de `peak_dip`
Regla H4 (cierre de vela):
- Tomar las ultimas 4 velas H4 no-doji.
- Si la vela mas reciente tiene color opuesto a las 3 anteriores, hay setup:
  - `bull,bull,bull,bear` -> candidato `sell`
  - `bear,bear,bear,bull` -> candidato `buy`

Regla M15 (solo si hay setup H4 activo):
- Evaluar en cada cierre de vela M15 hasta que venza la ventana de validez.
- Si se confirma entrada M15, se abre operacion con el `volume` configurado en la instancia.

Notas:
- Timeframes H4/M15 son internos de la estrategia, no propiedades del bot.
- `volume` es obligatorio por instancia y se valida `>= 100000`.

### 4.2 Logica exacta de `peak_dip_m5_m1`
- Misma logica de patron 3+1 por color, pero en M5 (trend) y M1 (entrada).
- Timeframes internos de estrategia: `M5` y `M1`.
- Misma validacion de params: `volume`, `sl_points`, `tp_points`.

## 5. Persistencia en DB
Tablas usadas:
- `public."Bot"` (fuente principal para estrategia y params)
- `public."BotStrategyConfig"` (legacy/compatibilidad)
- `public."BotEventLog"` (logs operativos)

### 5.1 `Bot`
Guarda identidad, instrumento, estado (`BotStatus`), timestamps, `userId`, `accountId`, y ahora tambien:
- `strategy` (text)
- `strategyParams` (jsonb)

Notas:
- Las columnas legacy `trendTimeframe` / `signalTimeframe` no se usan en la logica actual de estrategias.
- La API no las expone en responses de bots.

### 5.2 `BotStrategyConfig`
Se mantiene por compatibilidad:
- `botId` (PK/FK a Bot.id)
- `strategy` (text)
- `params` (jsonb)
- `createdAt`, `updatedAt`

### 5.3 `BotEventLog`
Guarda eventos operativos:
- `id` (text UUID)
- `botId` (FK a Bot.id)
- `timeUtc` (timestamp)
- `event` (text)
- `details` (jsonb)

Indice:
- `idx_bot_event_log_bot_time` sobre (`botId`, `timeUtc DESC`)

## 6. Ciclo de vida de bots
Estados en DB (`BotStatus`):
- `RUNNING`
- `PAUSED`
- `STOPPED`
- `ERROR`

Semantica de endpoints:
- `start`: monta estrategia en runtime, suscribe al mercado segun timeframes requeridos por la estrategia y pone `RUNNING`.
- `pause`: desmonta runtime y pone `PAUSED`.
- `stop`: desmonta runtime y pone `STOPPED`.
- `resume`: alias funcional de `start`.
- `delete`: soft delete (`isDeleted=true`) y estado `STOPPED`.

## 7. Endpoints
Base URL local:
- `http://127.0.0.1:8000`

### 7.1 Health
`GET /`

Prueba de alerta WhatsApp:
- `POST /alerts/test-whatsapp`

### 7.2 Estrategias
`GET /strategies`

Response ejemplo:
```json
{
  "count": 3,
  "strategies": [
    {
      "id": "peak_dip",
      "name": "Peak/Dip",
      "params": [
        {"key":"volume","type":"float","required":true,"default":100000},
        {"key":"sl_points","type":"int","required":true,"default":100},
        {"key":"tp_points","type":"int","required":true,"default":200}
      ]
    },
    {
      "id": "peak_dip_m5_m1",
      "name": "Peak/Dip M5/M1",
      "params": [
        {"key":"volume","type":"float","required":true,"default":100000},
        {"key":"sl_points","type":"int","required":true,"default":100},
        {"key":"tp_points","type":"int","required":true,"default":200}
      ]
    }
  ]
}
```

### 7.3 Bots - CRUD
#### Crear bot
`POST /bots`

Requeridos:
- `symbol`
- `strategy`
- `accountId` (o `account_id`)
- `volume` en `strategyParams` (o por atajo `volume`/`volumeUnits`), con minimo `100000`

Opcionales:
- `userId` (o `user_id`)
- `name`
- `strategyParams` (o `strategy_params`)
- `sl_points` / `tp_points` (atajo de estrategia)
- `slPoints` / `tpPoints` (atajo camelCase)
- `volume` / `volumeUnits` (atajo para volumen en unidades Open API)

Body ejemplo:
```json
{
  "symbol": "EURUSD",
  "strategy": "peak_dip",
  "accountId": "45440970",
  "volume": 100000,
  "sl_points": 120,
  "tp_points": 240,
  "userId": "user_xxx",
  "name": "Bot EURUSD"
}
```

Response:
```json
{
  "bot_id": "uuid"
}
```

#### Listar bots
`GET /bots`

Opcional por usuario:
- `GET /bots?userId=user_xxx`

#### Crear multiples bots (bulk)
`POST /bots/bulk`

Body ejemplo:
```json
{
  "strategy": "peak_dip",
  "accountId": "45440970",
  "userId": "45440970",
  "namePrefix": "multi_peak",
  "autoStart": true,
  "bots": [
    {"symbol": "EURUSD", "volume": 100000, "sl_points": 120, "tp_points": 240},
    {"symbol": "GBPUSD", "volume": 100000, "sl_points": 120, "tp_points": 240}
  ]
}
```

Response ejemplo:
```json
{
  "total": 2,
  "created_count": 2,
  "started_count": 2,
  "failed_count": 0,
  "results": [
    {"index":0,"status":"ok","bot_id":"uuid","symbol":"EURUSD","strategy":"peak_dip","started":true}
  ]
}
```

#### Obtener bot
`GET /bots/{bot_id}`


#### Estado runtime de estrategia (solo bot corriendo)
En `GET /bots/{bot_id}`, si el bot esta activo en runtime (`runtimeActive=true`), se agrega:
- `strategyRuntimeState`

Ejemplo (`peak_dip`):
```json
{
  "runtimeActive": true,
  "strategyRuntimeState": {
    "strategy": "peak_dip",
    "symbol": "EURUSD",
    "stage": "WAITING_H4_SETUP",
    "pending_windows_count": 0,
    "h4_last_4": [
      {
        "time_utc": "2026-03-10T08:00:00+00:00",
        "open": 1.0812,
        "high": 1.0840,
        "low": 1.0801,
        "close": 1.0835,
        "direction": "bull",
        "is_doji": false
      }
    ],
    "h4_progress": {
      "non_doji_count": 3,
      "step": "WAITING_REVERSAL_BEAR",
      "candidate_side": "sell",
      "message": "Esperando 3 velas en un sentido y 1 reversa en H4"
    }
  }
}
```

Stages posibles:
- `WAITING_H4_SETUP`: aun no se confirmo setup H4 completo (`peak_dip`).
- `WAITING_M15_ENTRY`: setup H4 detectado, esperando gatillo de entrada en M15 (`peak_dip`).
- `WAITING_M5_SETUP`: aun no se confirmo setup M5 completo (`peak_dip_m5_m1`).
- `WAITING_M1_ENTRY`: setup M5 detectado, esperando gatillo de entrada en M1 (`peak_dip_m5_m1`).
#### Actualizar bot
`PATCH /bots/{bot_id}`

Body parcial permitido:
```json
{
  "name": "Bot actualizado",
  "symbol": "GBPUSD",
  "strategy": "peak_dip",
  "strategyParams": {
    "sl_points": 150,
    "tp_points": 300
  }
}
```


Ejemplo `fast_test` para probar apertura/cierre rapido:
```json
{
  "symbol": "EURUSD",
  "strategy": "fast_test",
  "accountId": "45440970",
  "strategyParams": {
    "side": "buy",
    "volume": 100000,
    "sl_points": 30,
    "tp_points": 30,
    "open_interval_sec": 20,
    "hold_sec": 8,
    "max_cycles": 10
  }
}
```
#### Eliminar bot (soft delete)
`DELETE /bots/{bot_id}`

### 7.4 Bots - Control de ejecucion
`POST /bots/{bot_id}/start`

`POST /bots/{bot_id}/resume`

`POST /bots/{bot_id}/pause`

`POST /bots/{bot_id}/stop`

### 7.5 Bots - Logs y simulacion
#### Logs
`GET /bots/{bot_id}/logs?limit=100`

Response ejemplo:
```json
{
  "bot_id": "uuid",
  "count": 2,
  "logs": [
    {
      "id": "uuid",
      "botId": "uuid",
      "timeUtc": "2026-03-08T21:10:00",
      "event": "STARTED",
      "details": {"symbol":"EURUSD","strategy":"peak_dip"}
    }
  ]
}
```

#### Dry-run (sin enviar orden real)
`POST /bots/{bot_id}/dry-run`

Body:
```json
{
  "side": "buy",
  "entry": 1.0845
}
```

`entry` es opcional:
- Si no se envia, usa precio actual de mercado.

Response ejemplo:
```json
{
  "bot_id": "uuid",
  "symbol": "EURUSD",
  "strategy": "peak_dip",
  "input": {"side":"buy","entry":1.0845},
  "plan": {"side":"buy","entry":1.0845,"sl":1.0725,"tp":1.1085,"rr":2.0},
  "used_market_price": false
}
```

### 7.6 Market data
Pares de forex (27 majors/crosses):
- `GET /market/symbols/majors`

Estado de conexion cTrader/OpenAPI:
- `GET /market/connection-status`

Precio unico:
- `GET /market/price?symbol=EURUSD`

Precio en lote:
- `GET /market/prices?symbols=EURUSD,GBPUSD,USDJPY`

Estado del hub interno (suscripciones/cache):
- `GET /market/hub/status`

Estado de un simbolo en hub (quote + barras cacheadas):
- `GET /market/hub/symbol/{symbol}`

Estado del event bus interno (`bar_closed:*`):
- `GET /market/events/status`

Health consolidado de runtime market (conexion + hub + event bus + bots runtime):
- `GET /market/runtime/health`

Stream interno para bots activos (suscribe/reafirma simbolos activos en runtime):
- `POST /market/stream/active-bots/start`

WebSocket para front (precios en vivo):
- `GET /ws/prices?symbols=EURUSD,GBPUSD&interval=1` (upgrade a WS)

Notas:
- `GET /market/hub/symbol/{symbol}` devuelve `quote: null` y `bars: {}` si ese simbolo aun no tiene cache.
- Los eventos de barra relevantes para estrategias se publican como topics `bar_closed:{SYMBOL}:{TF}`.

### 7.7 Manual trading (directo)
Abrir orden manual:
- `POST /manual/open`

Body:
```json
{
  "symbol": "EURUSD",
  "side": "buy",
  "volume": 100000
}
```

Cerrar posicion manual:
- `POST /manual/close`

Body:
```json
{
  "position_id": 123456789
}
```

Notas de volumen (manual, peak_dip y fast_test):
- `volume` se maneja en unidades Open API directas (compat v1). En creacion de bots se valida `volume >= 100000`.
- No se envian lotes (0.01/0.1/1.0) en estas rutas.
- SL/TP se fijan luego de abrir la posicion (post-open amend) usando precio real de entrada, con verificacion y reintentos.

## 8. Errores comunes
- `REQUEST_FREQUENCY_EXCEEDED`: cTrader limito la frecuencia de requests. El backend aplica throttle/backoff dinamico en trendbars y expone contadores en `GET /market/runtime/health`.
- `KeyError('time')`: respuesta de trendbars vacia o incompleta. El backend actual ignora ese tick y reintenta.
- `InvalidStateError: Future.set_exception()`: race en futures async. El backend actual setea resultados/excepciones de forma segura.
- Si ves `Cannot pass a datetime or Timestamp with tzinfo with the tz parameter`, revisar version desplegada: el fix actual usa normalizacion UTC robusta en poller y estrategia (`ensure_utc_timestamp`).
- Si `/ws/prices` falla en handshake/404, verificar dependencias WS instaladas y backend reiniciado.
- Para diagnostico rapido usa `GET /market/runtime/health` (ready, conexion, hub, eventos, bots).

- `400`: validacion de payload (`symbol/strategy/accountId` faltantes, `strategyParams` invalidos, etc.).
- `404`: bot inexistente.
- `500`: error interno (DB, runtime).
- `502`: fallo al consultar precio en cTrader.

## 9. Restauracion tras caida
En startup:
1. Se conecta cliente cTrader.
2. Se asegura schema auxiliar (`Bot`, `BotStrategyConfig`, `BotEventLog`).
3. Se consultan bots activos.
4. Solo bots con estado `RUNNING` se restauran en memoria.
5. Bots restaurados se vuelven a suscribir a mercado por simbolo.
6. Si falla restauracion de un bot, pasa a estado `ERROR` y se registra `lastError`.

## 10. Notas de integracion front
- Front y backend comparten DB.
- Front debe elegir estrategia desde `/strategies`.
- Front envia `strategy` en create (obligatorio).
- Recomendado: front valide params por estrategia usando metadata de `/strategies`.

## 11. Checklist rapido de prueba
1. `GET /strategies`
2. `GET /market/symbols/majors`
3. `POST /bots`
4. `POST /bots/{id}/start`
5. `POST /bots/{id}/dry-run`
6. `GET /bots/{id}/logs`
7. `POST /bots/{id}/pause`
8. `DELETE /bots/{id}`

## 12. Alertas WhatsApp (errores API)
El backend puede enviar alerta de WhatsApp cuando ocurre un error 5xx en la API.

Providers soportados:
- `twilio`
- `bot_whatsapp` (bridge HTTP para tu bot Node con Baileys)

Variables `.env` comunes:
- `ALERTS_WHATSAPP_ENABLED=true`
- `ALERTS_WHATSAPP_PROVIDER=bot_whatsapp` (o `twilio`)
- `ALERTS_WHATSAPP_TO=54911XXXXXXXX` (o formato que espere tu bot)
- `ALERTS_WHATSAPP_COOLDOWN_SEC=120` (anti-spam)
- `ALERTS_WHATSAPP_TIMEOUT_SEC=10`

Si usas `bot_whatsapp` (recomendado para tu caso):
- `BOT_WHATSAPP_WEBHOOK_URL=http://127.0.0.1:3001/alerts/whatsapp`
- `BOT_WHATSAPP_WEBHOOK_TOKEN=tu_token_opcional`

Payload que envia ctrade_v2 al webhook:
```json
{
  "to": "54911XXXXXXXX",
  "message": "[ctrade_v2][dev] API error 500...",
  "channel": "whatsapp",
  "source": "ctrade_v2"
}
```
Header opcional:
- `Authorization: Bearer <BOT_WHATSAPP_WEBHOOK_TOKEN>`

Si usas `twilio`:
- `ALERTS_WHATSAPP_TO=whatsapp:+54911XXXXXXXX`
- `ALERTS_WHATSAPP_FROM=whatsapp:+14155238886` (sandbox Twilio) o numero habilitado
- `TWILIO_ACCOUNT_SID=AC...`
- `TWILIO_AUTH_TOKEN=...`

Notas:
- Se notifican errores HTTP 5xx y excepciones no controladas en requests.
- Hay cooldown por ruta/metodo/status para evitar spam.
- Para validar configuracion: `POST /alerts/test-whatsapp`.

## 13. Historico de velas (`/history`)
Endpoint:
- `GET /history/{instrument}/{timeframe}`

Timeframes canonicos:
- `M1`, `M5`, `M15`, `H1`, `H4`

Aliases aceptados (normaliza a canonico):
- `m1`, `1m`, `MINUTE_1`
- `m5`, `5m`, `MINUTE_5`
- `m15`, `15m`, `MINUTE_15`
- `1h`, `HOUR_1`
- `4h`, `HOUR_4`

Query params canonicos:
- `start`: ISO datetime o `YYYY-MM-DD`
- `end`: ISO datetime o `YYYY-MM-DD`
- `limit`: entero entre `1` y `2500` (default `500`)

Shape de respuesta (siempre estable):
```json
{
  "instrument": "USDCHF",
  "timeframe": "M5",
  "count": 1234,
  "candles": [
    {
      "time": "2026-03-31T16:15:00Z",
      "open": 0.80231,
      "high": 0.80256,
      "low": 0.80227,
      "close": 0.80249
    }
  ]
}
```

Semantica de errores:
- `400`: instrumento/timeframe/query invalidos.
- `502`: error consultando datasource de mercado.
