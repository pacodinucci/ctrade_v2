# Handoff Backend - Estrategia `leg_continuation_h4_m15`

## Resumen
Se implemento una nueva estrategia runtime llamada `leg_continuation_h4_m15` basada en la logica del backtest H4->M15 (continuacion por legs/pivots).

Objetivo: que el bot detecte setup estructural en H4, espere breakout de nivel de continuacion en H4 y ejecute entrada en cierre M15, con SL/TP en puntos.

Adicionalmente, se implemento una variante de test de mayor frecuencia:
- `leg_continuation_m5_m1` (estructura en M5 + entrada en M1)

## Archivos agregados/modificados
- `app/strategies/leg_continuation/__init__.py`
- `app/strategies/leg_continuation/pivots.py`
- `app/strategies/leg_continuation/strategy.py`
- `app/strategies/registry.py` (registro + params + factory + plan de dry-run)

## Strategy ID y metadata
Nuevo `strategy.id`:
- `leg_continuation_h4_m15`
- `leg_continuation_m5_m1`

Nombre:
- `Leg Continuation H4/M15`

Parametros expuestos por `GET /strategies`:
- `volume` (float, requerido, default `100000`, validacion `>= 100000`)
- `sl_points` (int, requerido, default `100`, validacion `> 0`)
- `tp_points` (int, requerido, default `200`, validacion `> 0`)
- `pivot_strength` (int, requerido, default `2`, validacion `>= 1`)
- `leg_mode` (string, requerido, default `extended`, solo admite `extended`)

## Comportamiento runtime (operativo)
Timeframes requeridos por la estrategia:
- `leg_continuation_h4_m15`: `H4` + `M15`
- `leg_continuation_m5_m1`: `M5` + `M1`

Pipeline operativo:
1. En cada cierre H4:
- Actualiza buffer H4.
- Calcula pivots (`find_pivots`) y compresion (`compress_pivots`).
- Construye legs (`build_legs_extended`).
- Detecta setups por tripletas:
  - `bullish -> bearish -> bullish` => `buy`
  - `bearish -> bullish -> bearish` => `sell`
- Define `continuation_level = leg_a.end_price`.
- Ventana de busqueda:
  - `search_start = leg_c.start_time`
  - `search_end = next_leg.start_time` (si existe), sino `last_h4_time + 4h`.
- Marca breakout cuando una vela H4 cierra:
  - `buy`: `close > continuation_level`
  - `sell`: `close < continuation_level`

2. En cada cierre M15:
- Si ya hubo breakout H4 y estamos dentro de ventana, busca entrada M15:
  - `buy`: `close > continuation_level`
  - `sell`: `close < continuation_level`
- Antes de abrir valida que no haya posicion abierta en el simbolo.
- Abre trade con `volume`, `sl_points`, `tp_points`.

La variante `leg_continuation_m5_m1` replica exactamente el mismo pipeline, reemplazando:
- H4 -> M5
- M15 -> M1
- extension de ventana sin next-leg: `+4h` -> `+5m`

Regla operativa mantenida:
- Maximo una posicion simultanea por bot/simbolo (misma politica que estrategias actuales).

## Runtime state (GET /bots/{id})
La estrategia expone `strategyRuntimeState` con:
- `strategy`
- `symbol`
- `stage` (`WAITING_H4_LEGS` o `WAITING_BREAKOUT_OR_ENTRY`)
- `h4_count`
- `m15_count`
- `pivot_strength`
- `pending_setups_count`
- `current_setup` (si aplica)
- `h4_last_4`
- `m15_last_4`

## Dry run
`POST /bots/{id}/dry-run` soporta la nueva estrategia via registry.
Plan calculado:
- `buy`: `sl = entry - sl_points*pt`, `tp = entry + tp_points*pt`
- `sell`: `sl = entry + sl_points*pt`, `tp = entry - tp_points*pt`

## Payload ejemplo (create bot)
```json
{
  "symbol": "EURUSD",
  "strategy": "leg_continuation_h4_m15",
  "accountId": "45440970",
  "strategyParams": {
    "volume": 100000,
    "sl_points": 100,
    "tp_points": 200,
    "pivot_strength": 2,
    "leg_mode": "extended"
  }
}
```

## Compatibilidad / impacto
- No rompe estrategias existentes (`peak_dip`, `peak_dip_m5_m1`, `fast_test`).
- No requiere cambios de schema DB adicionales (usa `strategy` + `strategyParams` ya existentes).
- Se integra al flujo actual de `BotManager`, `start/pause/stop`, logs y dry-run.

## Pendiente recomendado para backend
1. Agregar esta estrategia a la documentacion principal de API (`docs/BOT_API_DOCUMENTATION.md`).
2. Opcional: test de contrato para `GET /strategies` verificando presencia de `leg_continuation_h4_m15`.
3. Opcional: test de validacion de params (`leg_mode`, `pivot_strength`, `volume`, `sl/tp`).

## Nota tecnica
No se ejecutaron pruebas end-to-end de trading en este entorno por faltantes de dependencias/runtime externo (cTrader stack), pero la integracion de codigo y compilacion local de los modulos nuevos quedo OK.
