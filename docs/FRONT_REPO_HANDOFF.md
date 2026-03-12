# Front Handoff - ctrade_v2 (Resumen corto)

## Estado actual
Backend listo para integrar en front con:
- Selector de 27 pares forex majors/crosses.
- Creacion individual de bot (`POST /bots`).
- Creacion multiple de bots (`POST /bots/bulk`) para boton `Multiple bots`.
- `peak_dip` con regla H4 exacta 3+1 y validacion M15 por cierre.
- `peak_dip_m5_m1` (misma logica, pero en M5/M1) para probar carga mas rapido.
- Escalado mejorado para multiples bots (throttle/backoff + MarketDataHub + event bus interno por `bar_closed`).

## Endpoints clave
- `GET /strategies`
- `GET /market/symbols/majors`
- `GET /bots?userId=...`
- `GET /bots/{bot_id}`
- `POST /bots`
- `POST /bots/bulk`
- `POST /bots/{bot_id}/start`
- `POST /bots/{bot_id}/pause`
- `POST /bots/{bot_id}/stop`
- `DELETE /bots/{bot_id}`
- `GET /bots/{bot_id}/logs?limit=100`
- `GET /market/connection-status`
- `GET /market/hub/status`
- `GET /market/hub/symbol/{symbol}`
- `GET /market/events/status`
- `GET /market/runtime/health`
- `GET /ws/prices?symbols=EURUSD,GBPUSD&interval=1` (WebSocket)

## Estrategias recomendadas
- `peak_dip`: H4/M15
- `peak_dip_m5_m1`: M5/M1 (ideal para test de simultaneidad)

## Reglas importantes de payload
- En create bot, `volume` es obligatorio por estrategia (`strategyParams.volume` o atajo top-level `volume`/`volumeUnits`).
- Minimo de `volume`: `100000`.
- Timeframes de estrategia son internos (`H4/M15` en `peak_dip`, `M5/M1` en `peak_dip_m5_m1`).

## Ejemplo create bot (individual)
```json
{
  "symbol": "EURUSD",
  "strategy": "peak_dip",
  "accountId": "45440970",
  "userId": "45440970",
  "volume": 100000,
  "sl_points": 120,
  "tp_points": 240,
  "name": "Peak EURUSD"
}
```

## Ejemplo create multiple bots
```json
{
  "strategy": "peak_dip",
  "accountId": "45440970",
  "userId": "45440970",
  "namePrefix": "multi_peak",
  "autoStart": true,
  "bots": [
    {"symbol": "EURUSD", "volume": 100000, "sl_points": 120, "tp_points": 240},
    {"symbol": "GBPUSD", "volume": 100000, "sl_points": 120, "tp_points": 240},
    {"symbol": "USDJPY", "volume": 100000, "sl_points": 120, "tp_points": 240}
  ]
}
```

## Respuesta esperada de /bots/bulk
```json
{
  "total": 3,
  "created_count": 3,
  "started_count": 3,
  "failed_count": 0,
  "results": [
    {"index":0,"status":"ok","bot_id":"...","symbol":"EURUSD","strategy":"peak_dip","started":true}
  ]
}
```

## Datos para UI en detalle de bot
En `GET /bots/{bot_id}` cuando `runtimeActive=true` llega `strategyRuntimeState`.
Para `peak_dip`, mostrar:
- `stage` (`WAITING_H4_SETUP` | `WAITING_M15_ENTRY`)
- `h4_last_4`
- `h4_progress`

Para `peak_dip_m5_m1`, mostrar:
- `stage` (`WAITING_M5_SETUP` | `WAITING_M1_ENTRY`)
- `m5_last_4`
- `m5_progress`

## Archivos de referencia en este repo
- `docs/BOT_API_DOCUMENTATION.md`
- `docs/FRONT_INTEGRATION_CHECKLIST.md`

## Diagnostico rapido recomendado
Si el front detecta inconsistencias de stream/estado, consultar:
1. `GET /market/runtime/health`
2. `GET /market/hub/symbol/{symbol}` para un simbolo activo

`/market/runtime/health` centraliza:
- `ready` general del runtime
- estado de conexion cTrader
- estado del hub (simbolos/listeners/cache)
- estado del event bus interno
- estado runtime de bots activos/suscripciones