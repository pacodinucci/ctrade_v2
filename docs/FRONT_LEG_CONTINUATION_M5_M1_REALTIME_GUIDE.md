# Front Guide - `leg_continuation_m5_m1` en Tiempo Real

## Update 2026-05-02 (breaking contract)
Se corrigio la semantica de breakout en backend:
- Romper `continuation_level` ya no marca breakout.
- Ahora eso solo indica continuidad/extension del impulso.
- El breakout real se confirma con `breakout_level` (extremo correctivo).

Cambios de contrato en `strategyRuntimeState.current_setup`:
- Se agrega `breakout_level`.
- Se agrega `continuation_extended_at`.
- `breakout_time` se reemplaza por `breakout_confirmed_at`.
- Se agrega `entry_retest_done`.

Cambios de stage:
- `WAITING_M5_BREAKOUT`: setup activo sin breakout real.
- `WAITING_M1_ENTRY`: breakout real confirmado, esperando entrada M1.


## Objetivo
Definir exactamente como representar en frontend el ciclo de vida de la estrategia `leg_continuation_m5_m1` para que el usuario vea:
- Que esta esperando la estrategia.
- Que condicion ya se cumplio.
- En que paso exacto esta antes de abrir operacion.
- Por que no abre (si no abre).

Esta guia esta orientada a implementacion UI/UX y contrato de datos consumiendo la API actual del backend.

## Resumen operativo de la estrategia
La estrategia trabaja en dos capas:
1. Estructura en `M5` (deteccion de setup por legs/pivots).
2. Confirmacion/entrada en `M1` (apertura de trade).

Pipeline logico:
1. Acumula velas `M5`.
2. Detecta pivots y construye legs.
3. Busca patron de continuacion de 3 legs (`A-B-C`).
4. Crea `setup` con:
- `side` (`buy` o `sell`)
- `continuation_level`
- ventana de validez: `search_start` -> `search_end`
5. Marca breakout cuando precio `M5` rompe `continuation_level` dentro de ventana.
6. En `M1`, si hay breakout previo y sigue del mismo lado, intenta abrir operacion.
7. Antes de abrir, valida que no exista posicion abierta para ese simbolo.

## Ajuste importante ya aplicado (evita setups perdidos)
Cuando un setup se detecta tarde (por confirmacion de pivots), ahora se hace backfill de breakout con historico `M5` en memoria:
- Si la ruptura ya ocurrio dentro de la ventana, el setup nace con `breakout_time` ya definido.
- Si el setup ya esta vencido, se descarta.

Impacto UI: vas a poder ver setups que aparezcan directamente en estado "breakout detectado" sin esperar un nuevo `M5`.

## Endpoints a consumir
1. `GET /bots/{bot_id}`
2. `GET /bots/{bot_id}/logs?limit=100`
3. `GET /bots/{bot_id}/registro?status=OPEN&limit=200`
4. `GET /bots/{bot_id}/registro?status=CLOSED&limit=200`

Recomendacion de polling:
- `GET /bots/{bot_id}` cada 2-3s si `runtimeActive=true`.
- `GET /bots/{bot_id}/logs` cada 3-5s.
- `OPEN/CLOSED` cada 10-15s.

## Contrato relevante de `GET /bots/{bot_id}`
Para esta estrategia, `strategyRuntimeState` devuelve:
- `strategy`
- `symbol`
- `stage`
- `m5_count`
- `m1_count`
- `pivot_strength`
- `pending_setups_count`
- `current_setup` (si hay pending)
- `m5_last_4`
- `m1_last_4`

`current_setup`:
- `side`
- `continuation_level`
- `search_start`
- `search_end`
- `breakout_time` (null o timestamp)

## Maquina de estados recomendada para UI
Estado principal (badge grande):
1. `INACTIVO`
- Condicion: `runtimeActive=false`.
- Mensaje: "Bot pausado/detenido".

2. `CARGANDO ESTRUCTURA M5`
- Condicion: `runtimeActive=true` y `stage="WAITING_M5_LEGS"`.
- Mostrar:
- `m5_count`
- minimo esperado aproximado: `2*pivot_strength + 5`

3. `SETUP ACTIVO`
- Condicion: `stage="WAITING_BREAKOUT_OR_ENTRY"` y `current_setup` existe.
- Mostrar:
- `side`
- `continuation_level`
- ventana (`search_start`, `search_end`)

4. `BREAKOUT DETECTADO`
- Condicion: `current_setup.breakout_time != null`.
- Mensaje: "Esperando confirmacion M1 para entrada".

5. `ENTRADA EJECUTADA`
- Detectar por:
- nueva fila en `registro?status=OPEN`, o
- log de evento de apertura (si existe), o
- cambio de `pending_setups_count` a 0 junto con trade abierto.

6. `BLOQUEADO POR POSICION ABIERTA`
- Condicion inferida:
- hay `current_setup` con breakout,
- no abre trade,
- y existe trade `OPEN` para el bot.
- Mensaje: "Setup valido, bloqueado por regla de 1 posicion por simbolo".

7. `SETUP EXPIRADO`
- Condicion inferida:
- `current_setup` desaparece,
- no hubo trade abierto,
- paso `search_end`.
- Mensaje: "Ventana vencida sin entrada".

## Timeline en vivo (componente recomendado)
Renderizar un timeline de eventos con pasos fijos:
1. `M5 buffer update`
2. `Leg pattern detected (A-B-C)`
3. `Setup created`
4. `Breakout detected`
5. `M1 confirmation`
6. `Open trade`

Como construir cada item con datos actuales:
- 1: comparar `m5_count` previo vs actual.
- 2 y 3: cuando `pending_setups_count` sube o aparece nuevo `current_setup`.
- 4: cuando `current_setup.breakout_time` pasa de `null` a timestamp.
- 5: inferido cuando hay breakout y llegan nuevas `m1_last_4`.
- 6: detectar nuevo trade `OPEN`.

## Reglas de inferencia para "por que no abre"
Mostrar diagnostico explicito, en este orden:
1. Si `runtimeActive=false`: "Bot no esta corriendo".
2. Si `stage=WAITING_M5_LEGS`: "Sin estructura suficiente en M5".
3. Si no hay `current_setup`: "Sin setup de continuacion vigente".
4. Si `breakout_time=null`: "Setup detectado, esperando breakout M5".
5. Si `breakout_time!=null` y hay trade OPEN: "Regla de una posicion activa".
6. Si `breakout_time!=null` y no hay OPEN: "Esperando confirmacion M1".

## Modelo de datos frontend sugerido (TypeScript)
```ts
export type LegContinuationCurrentSetup = {
  side: "buy" | "sell";
  continuation_level: number;
  search_start: string;
  search_end: string;
  breakout_time: string | null;
};

export type LegContinuationM5M1State = {
  strategy: "leg_continuation_m5_m1";
  symbol: string;
  stage: "WAITING_M5_LEGS" | "WAITING_BREAKOUT_OR_ENTRY";
  m5_count: number;
  m1_count: number;
  pivot_strength: number;
  pending_setups_count: number;
  current_setup?: LegContinuationCurrentSetup;
  m5_last_4: Array<Record<string, unknown>>;
  m1_last_4: Array<Record<string, unknown>>;
};
```

## Wireframe funcional minimo (detalle bot)
1. Header
- Bot name, symbol, status, runtimeActive.

2. Strategy status card
- Estado principal (de la maquina).
- Barra de progreso por etapa.
- `pending_setups_count`.

3. Current setup card (si existe)
- `side`, `continuation_level`, countdown a `search_end`.
- chip `BREAKOUT` activo/inactivo segun `breakout_time`.

4. Candles preview
- tabla compacta con `m5_last_4` y `m1_last_4`.

5. Trades tabs
- `OPEN` / `CLOSED`.

6. Event timeline
- Feed inferido + logs backend.

## Formato de tiempo y UX
- Internamente siempre UTC (como API).
- En UI mostrar timezone del usuario con tooltip UTC.
- Resaltar countdown de `search_end` en rojo si faltan < 60s.

## Alertas front recomendadas
Disparar toast/notificacion cuando:
1. Aparece `current_setup` nuevo.
2. `breakout_time` se vuelve no nulo.
3. Se abre trade (`OPEN` +1).
4. Setup desaparece por expiracion sin entrada.

## Limitaciones actuales del contrato (importante)
Con API actual, el front puede mostrar casi todo, pero no tiene evento explicito de "motivo exacto de skip" por vela.

Si queres visibilidad 100% deterministica paso a paso, sugerencia backend futura:
- agregar `strategyRuntimeEvents` (ring buffer corto) dentro de `GET /bots/{id}` con eventos tipados:
- `SETUP_CREATED`
- `BREAKOUT_DETECTED`
- `ENTRY_CONDITION_M1_OK`
- `ENTRY_SKIPPED_OPEN_POSITION`
- `ENTRY_OPENED`
- `SETUP_EXPIRED`

## Checklist de implementacion front
1. Tipar `strategyRuntimeState` para `leg_continuation_m5_m1`.
2. Implementar polling 2-3s de `GET /bots/{id}`.
3. Implementar motor de inferencia de estados UI.
4. Mostrar card de setup actual + countdown.
5. Conectar tabs de trades (`OPEN/CLOSED`).
6. Agregar timeline de eventos inferidos.
7. Agregar alertas de hitos (setup/breakout/open/expire).
8. Probar con 27 pares verificando que cada bot tenga su estado independiente.

## Ejemplo de lectura rapida para soporte/operacion
"No abre operaciones":
1. Revisar `runtimeActive`.
2. Revisar `stage`.
3. Revisar `current_setup` y `breakout_time`.
4. Revisar si hay trade `OPEN`.
5. Revisar logs del bot para errores runtime.

Con estos 5 pasos se puede diagnosticar casi todos los casos sin mirar codigo.

