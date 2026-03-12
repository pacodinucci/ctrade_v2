# Frontend Integration Checklist - ctrade_v2 API

## Objetivo
Conectar el front (Next.js) con la API de bots de `ctrade_v2` de forma estable y predecible.

## 1) Variables de entorno
- Definir `NEXT_PUBLIC_API_URL`.
- Ejemplo local: `http://127.0.0.1:8000`.
- Backend: ejecutar con soporte WS (`uvicorn[standard]` o `websockets`/`wsproto`).

## 2) Endpoints a implementar en el cliente
- `GET /strategies`
- `GET /bots?userId=...`
- `GET /bots/{bot_id}`
- `POST /bots`
- `POST /bots/bulk`
- `PATCH /bots/{bot_id}`
- `DELETE /bots/{bot_id}`
- `POST /bots/{bot_id}/start`
- `POST /bots/{bot_id}/pause`
- `POST /bots/{bot_id}/stop`
- `POST /bots/{bot_id}/resume`
- `GET /bots/{bot_id}/logs?limit=100`
- `POST /bots/{bot_id}/dry-run`
- `GET /market/symbols/majors`
- `GET /market/price?symbol=EURUSD`
- `GET /market/prices?symbols=EURUSD,GBPUSD`
- `POST /market/stream/active-bots/start`
- `GET /ws/prices?symbols=EURUSD,GBPUSD&interval=1` (WebSocket)
- `POST /manual/open`
- `POST /manual/close`

## 3) Tipos TS recomendados
```ts
export type StrategyParamDef = {
  key: string;
  type: 'int' | string;
  required: boolean;
  default: number | string | boolean | null;
};

export type StrategyMeta = {
  id: string;
  name: string;
  params: StrategyParamDef[];
};

export type Bot = {
  id: string;
  name: string;
  instrument: string;
  status: 'RUNNING' | 'PAUSED' | 'STOPPED' | 'ERROR';
  userId: string;
  accountId: string;
  strategy: string;
  params: Record<string, unknown>;
  runtimeActive: boolean;
  strategyRuntimeState?: Record<string, unknown>;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
};

export type CreateBotPayload = {
  symbol: string;
  strategy: string;
  accountId: string;
  userId?: string;
  name?: string;
  strategyParams?: Record<string, unknown>;
  volume?: number;
  sl_points?: number;
  tp_points?: number;
};

export type BulkCreateBotsPayload = {
  strategy?: string;
  accountId?: string;
  userId?: string;
  namePrefix?: string;
  autoStart?: boolean;
  bots: CreateBotPayload[];
};
```

## 4) Contrato de create bot
Requeridos en `POST /bots`:
- `symbol`
- `strategy`
- `accountId`
- `volume` (en `strategyParams` o por atajo top-level `volume`/`volumeUnits`)

Opcionales:
- `userId`
- `name`
- `strategyParams`
- `sl_points` / `tp_points` (atajo para `peak_dip`)

Minimo de volumen validado por backend: `100000`.


## 4.1) Estrategias para pruebas
- `peak_dip`: H4 + M15
- `peak_dip_m5_m1`: M5 + M1 (mas frecuente para probar carga)
- `fast_test`: apertura/cierre rapido

## 4.2) Estrategia de test rapido
Para validar apertura/cierre sin esperar setup H4/M15, usar `fast_test`:
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

`volume` en `fast_test` se interpreta en unidades Open API directas.

## 4.3) Crear multiples bots (boton "Multiple bots")
Payload recomendado para `POST /bots/bulk` (ejemplo con `peak_dip_m5_m1`):
```json
{
  "strategy": "peak_dip_m5_m1",
  "accountId": "45440970",
  "userId": "45440970",
  "namePrefix": "multi_fast",
  "autoStart": true,
  "bots": [
    {"symbol": "EURUSD", "volume": 100000, "sl_points": 120, "tp_points": 240},
    {"symbol": "GBPUSD", "volume": 100000, "sl_points": 120, "tp_points": 240}
  ]
}
```

Payload alternativo:
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

## 4.4) Lista de 27 pares (selector)
Consumir `GET /market/symbols/majors`.

Respuesta ejemplo:
```json
{
  "count": 27,
  "symbols": ["EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCAD","USDCHF","USDJPY","EURGBP","EURAUD","EURNZD","EURCAD","EURCHF","EURJPY","GBPAUD","GBPNZD","GBPCAD","GBPCHF","GBPJPY","AUDNZD","AUDCAD","AUDCHF","AUDJPY","NZDCAD","NZDCHF","NZDJPY","CADCHF","CADJPY"]
}
```

## 5) Flujo UI recomendado
1. Cargar `GET /strategies` y `GET /market/symbols/majors` al abrir pantalla.
2. Mostrar boton `New bot` y boton `Multiple bots`.
3. `New bot`: selector de estrategia + selector de simbolo (de los 27 pares) + params.
4. `Multiple bots`: seleccionar varios simbolos y enviar un unico `POST /bots/bulk`.
5. Si el usuario marca inicio inmediato, usar `autoStart: true` en bulk.
6. Mostrar resultado item por item (`ok/error`) segun `results` de `/bots/bulk`.
7. Refrescar listado (`GET /bots`) y detalle/logs por bot cuando corresponda.
8. Opcional: `POST /market/stream/active-bots/start` para reafirmar stream interno.

## 6) Estados y UX
- `PAUSED` o `STOPPED`: boton principal = "Iniciar".
- `RUNNING`: boton principal = "Pausar".
- `ERROR`: mostrar `lastError` y permitir `start`/`stop`.
- `runtimeActive` indica si existe instancia activa en memoria.
- Si `runtimeActive=true`, mostrar `strategyRuntimeState` en el detalle del bot.

Para `peak_dip`, exponer al menos:
- `stage` (`WAITING_H4_SETUP` | `WAITING_M15_ENTRY`)
- `h4_last_4` (ultimas 4 velas H4)
- `h4_progress` (progreso del patron 3+1)

Para `peak_dip_m5_m1`, exponer al menos:
- `stage` (`WAITING_M5_SETUP` | `WAITING_M1_ENTRY`)
- `m5_last_4` (ultimas 4 velas M5)
- `m5_progress` (progreso del patron 3+1)

## 7) Manejo de errores
- `400`: validacion de payload (mostrar mensaje de detalle).
- `404`: bot no encontrado.
- `500`: error interno.
- `502`: fallo de market data.

Sugerencia: centralizar parseo de error HTTP:
- si respuesta trae `{ detail: string }`, mostrar `detail`.
- fallback: "Unexpected error".

## 8) WebSocket en front
URL:
- `ws://127.0.0.1:8000/ws/prices?symbols=EURUSD,GBPUSD&interval=1`

Snippet:
```ts
const ws = new WebSocket("ws://127.0.0.1:8000/ws/prices?symbols=EURUSD,GBPUSD&interval=1");

ws.onopen = () => console.log("open");
ws.onmessage = (e) => {
  const payload = JSON.parse(e.data);
  console.log(payload);
};
ws.onerror = (e) => console.error("ws error", e);
ws.onclose = (e) => console.log("close", e.code, e.reason);
```

## 9) Prueba rapida (smoke test)
1. `GET /strategies`
2. `GET /market/symbols/majors`
3. `POST /bots`
4. `POST /bots/bulk`
5. `POST /bots/{id}/start`
6. `POST /market/stream/active-bots/start`
7. `GET /bots/{id}/logs`
8. Conectar `ws://.../ws/prices?...`
9. `POST /manual/open` (opcional para prueba directa)
10. `POST /manual/close` (opcional para prueba directa)
11. `POST /bots/{id}/pause`
12. `DELETE /bots/{id}`

## 10) Notas de arquitectura
- El backend normaliza timestamps a UTC de forma segura (naive/aware) para evitar errores de polling.
- Timeframes (`H4/M15` en `peak_dip`, `M5/M1` en `peak_dip_m5_m1`) son internos de la estrategia.
- El front no define timeframes del bot.
- El bot escucha mercado al hacer `start` y opera cuando la estrategia detecta condiciones.
- Para escalar (ej. 27 bots), backend usa throttle/backoff en trendbars y polling alineado a cierre de vela.


