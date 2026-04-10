# Front Handoff - Seccion Registro

## Objetivo
Implementar en front una seccion `Registro` que muestre el historial de operaciones (aperturas/cierres) con filtros y estado.

## Endpoints de Registro
- `GET /registro?limit=200&status=ALL&symbol=EURUSD`
- `GET /bots/{bot_id}/registro?limit=200&status=ALL`

## Query params
- `limit`: `1..2000` (recomendado UI: `100` o `200`)
- `status`: `ALL | OPEN | CLOSED`
- `symbol`: opcional (solo en `/registro`)

## Respuesta (shape)
```json
{
  "count": 2,
  "filters": {
    "limit": 200,
    "status": "ALL",
    "symbol": "EURUSD"
  },
  "trades": [
    {
      "id": "9e6...",
      "positionId": "123456789",
      "botId": "0c2...",
      "source": "bot",
      "strategy": "peak_dip",
      "symbol": "EURUSD",
      "side": "buy",
      "volume": 100000.0,
      "openPrice": 1.08321,
      "stopLoss": 1.08221,
      "takeProfit": 1.08521,
      "openedAt": "2026-04-07T12:10:22.120Z",
      "closedAt": "2026-04-07T12:20:42.331Z",
      "status": "CLOSED",
      "closeReason": "MANUAL_CLOSE",
      "closePrice": 1.08411,
      "pnl": 90.0,
      "metadata": {
        "source": "bot"
      }
    }
  ]
}
```

## Semantica de campos
- `source`: `bot | manual`
- `status`: `OPEN | CLOSED`
- `closeReason`: ejemplo `MANUAL_CLOSE`, `STRATEGY_CLOSE` (puede venir `null` si sigue abierta)
- `pnl`: calculado en backend cuando existe `closePrice` y `openPrice`
- `openedAt`, `closedAt`: UTC ISO8601

## Tipos TS sugeridos
```ts
export type TradeStatus = 'OPEN' | 'CLOSED';
export type TradeSource = 'bot' | 'manual' | string;

export type TradeRegistryItem = {
  id: string;
  positionId: string;
  botId: string | null;
  source: TradeSource;
  strategy: string | null;
  symbol: string;
  side: 'buy' | 'sell' | string;
  volume: number;
  openPrice: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  openedAt: string;
  closedAt: string | null;
  status: TradeStatus;
  closeReason: string | null;
  closePrice: number | null;
  pnl: number | null;
  metadata: Record<string, unknown>;
};

export type TradeRegistryResponse = {
  count: number;
  filters: {
    limit: number;
    status: 'ALL' | TradeStatus;
    symbol?: string | null;
  };
  trades: TradeRegistryItem[];
};
```

## UI recomendada para Registro
- Tabla principal:
  - `Fecha apertura`
  - `Simbolo`
  - `Lado`
  - `Volumen`
  - `Entrada`
  - `SL`
  - `TP`
  - `Estado` (`OPEN/CLOSED`)
  - `Resultado (PnL)`
  - `Fuente` (`bot/manual`)
  - `Bot` (si `botId` existe)
- Filtros:
  - `Estado`: `ALL/OPEN/CLOSED`
  - `Simbolo` (solo en vista global)
  - `Limit` (50/100/200)
- Orden: por `openedAt DESC` (ya viene asi)

## Flujo de carga sugerido
1. Vista global: llamar `GET /registro?limit=100&status=ALL`
2. Vista detalle bot: llamar `GET /bots/{bot_id}/registro?limit=100&status=ALL`
3. Re-fetch:
   - al abrir/cerrar orden manual
   - al iniciar/pausar/reanudar bot
   - polling suave opcional cada 10-20s en pantalla activa

## Estado actual del backend (importante)
- Se registra apertura al abrir trade desde bots y desde `/manual/open`.
- Se registra cierre al cerrar por API (`/manual/close` y cierres que pasan por `client.close_trade`).
- Si una posicion cierra por SL/TP directamente en broker sin pasar por cierre local, puede quedar `OPEN` hasta implementar reconciliacion automatica.

## Checklist rapido para front
- Crear cliente `getRegistro(params)` y `getBotRegistro(botId, params)`.
- Agregar pagina/seccion `Registro`.
- Agregar filtros `status/symbol/limit`.
- Pintar `PnL` con color:
  - verde: `pnl > 0`
  - rojo: `pnl < 0`
  - neutro: `null` o `0`
- Mostrar badge `OPEN/CLOSED`.
