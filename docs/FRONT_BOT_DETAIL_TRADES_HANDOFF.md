# Front Handoff - Detalle de Bot (Operaciones Abiertas + Historial)

## Objetivo
Describir como el front debe consumir y mostrar, en la vista de detalle de cada bot:
- Operaciones abiertas actuales del bot.
- Historial de operaciones cerradas del bot.
- Estado operativo del bot (runtime + estrategia).

## Endpoints a usar (detalle de bot)
1. `GET /bots/{bot_id}`
2. `GET /bots/{bot_id}/registro?status=OPEN&limit=200`
3. `GET /bots/{bot_id}/registro?status=CLOSED&limit=200`
4. Opcional: `GET /bots/{bot_id}/logs?limit=100`

## Flujo recomendado de carga
1. Cargar `GET /bots/{bot_id}` para cabecera del detalle.
2. En paralelo, pedir:
   - `.../registro?status=OPEN`
   - `.../registro?status=CLOSED`
3. Renderizar:
   - Tab `Abiertas`: respuesta `OPEN`
   - Tab `Historial`: respuesta `CLOSED`
4. Refrescar cada `10-15s` el tab abierto (`OPEN`) mientras `runtimeActive=true`.

## Campos clave para UI
### 1) `GET /bots/{bot_id}`
Campos importantes:
- `id`, `name`, `instrument`, `strategy`, `status`
- `runtimeActive` (bool)
- `strategyRuntimeState` (solo si está activo en runtime)

`strategyRuntimeState` sirve para mostrar progreso interno de estrategia (ej. stage/setup/confirmación).

### 2) `GET /bots/{bot_id}/registro`
La respuesta incluye:
- `bot_id`
- `count`
- `filters` (`limit`, `status`)
- `trades[]`

Cada trade puede incluir:
- `positionId`
- `symbol`
- `side` (`buy`/`sell`)
- `volume`
- `openPrice`, `stopLoss`, `takeProfit`
- `openedAt`
- `status` (`OPEN`/`CLOSED`)
- `closedAt`, `closeReason`, `closePrice`, `pnl`
- `metadata`

## Mapeo de secciones en la pantalla
1. **Header del bot**
- `name`, `instrument`, `strategy`
- Badge de estado (`status`) + indicador `runtimeActive`

2. **Tab Operaciones Abiertas**
- Fuente: `/bots/{bot_id}/registro?status=OPEN`
- Columnas sugeridas:
  - `positionId`
  - `side`
  - `volume`
  - `openPrice`
  - `stopLoss` / `takeProfit`
  - `openedAt`

3. **Tab Historial**
- Fuente: `/bots/{bot_id}/registro?status=CLOSED`
- Columnas sugeridas:
  - `positionId`
  - `side`
  - `openPrice` -> `closePrice`
  - `pnl`
  - `closeReason`
  - `openedAt` / `closedAt`

4. **(Opcional) Estado runtime de estrategia**
- Fuente: `strategyRuntimeState` en `/bots/{bot_id}`
- Mostrar JSON resumido (por ejemplo `stage` y señales activas).

## Reglas de UX/consistencia
1. Si `runtimeActive=false`, igual mostrar historial y abiertas desde `registro`.
2. Si `OPEN` viene vacío, mostrar estado vacío explícito: "Sin operaciones abiertas".
3. Orden recomendado:
- Abiertas: `openedAt DESC`
- Historial: `closedAt DESC` (o `openedAt DESC` si `closedAt` no existe)
4. Usar `positionId` como key estable de fila.

## Limitaciones actuales (importante para front)
1. No existe endpoint público específico de "open positions reales del broker por bot".
2. La vista por bot se basa en `BotTradeLog` (`/registro`).
3. Trades manuales pueden existir con `bot_id = null` y no aparecer en `/bots/{bot_id}/registro`.

## Ejemplos de consumo
### Operaciones abiertas
```http
GET /bots/9f6c.../registro?status=OPEN&limit=200
```

### Historial
```http
GET /bots/9f6c.../registro?status=CLOSED&limit=200
```

### Bot detail
```http
GET /bots/9f6c...
```

## Contrato mínimo para implementar en front
1. Renderizar detalle con `/bots/{bot_id}`.
2. Renderizar tab `Abiertas` con `/bots/{bot_id}/registro?status=OPEN`.
3. Renderizar tab `Historial` con `/bots/{bot_id}/registro?status=CLOSED`.
4. Actualizar tab `Abiertas` por polling (10-15s) solo si `runtimeActive=true`.
