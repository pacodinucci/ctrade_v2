# Front Guide — Leg Continuation trigger contract

Este documento define cómo debe interpretar el front el flujo de entrada de las estrategias `leg_continuation_m5_m1` y `leg_continuation_h4_m15` después de la actualización de trigger.

## Resumen ejecutivo

Ambas estrategias usan la misma lógica conceptual:

1. El timeframe mayor confirma breakout.
2. Se guarda el cierre de la vela que confirmó el breakout.
3. El timeframe menor espera que el precio vuelva a la zona del nivel quebrado.
4. Luego espera un nuevo cierre a favor que supere/perfore el cierre del breakout.
5. Si el precio cierra demasiado en contra, la búsqueda del trigger se invalida.

## Estrategias alcanzadas

| Estrategia | Timeframe estructura | Timeframe trigger | Nivel quebrado | Cierre confirmador |
|---|---:|---:|---|---|
| `leg_continuation_m5_m1` | M5 | M1 | `breakout_level` | `breakout_confirmed_close` |
| `leg_continuation_h4_m15` | H4 | M15 | `continuation_level` | `breakout_close` |

> Nota para front: los nombres no son idénticos por compatibilidad, pero conceptualmente representan lo mismo.

## Flujo operativo

### 1. Esperando estructura

La estrategia todavía no tiene patrón válido de legs/pivots.

| Estrategia | Stage esperado |
|---|---|
| M5/M1 | `WAITING_M5_LEGS` |
| H4/M15 | `WAITING_H4_LEGS` |

### 2. Setup detectado, esperando breakout

Hay setup vigente, pero todavía no se confirmó el quiebre en el timeframe mayor.

| Estrategia | Señal de front |
|---|---|
| M5/M1 | `stage = WAITING_M5_BREAKOUT` y `current_setup.breakout_confirmed_at = null` |
| H4/M15 | `stage = WAITING_BREAKOUT_OR_ENTRY` y `current_setup.breakout_time = null` |

### 3. Breakout confirmado

El timeframe mayor cerró a favor del quiebre.

| Estrategia | Campo de tiempo | Campo de precio |
|---|---|---|
| M5/M1 | `breakout_confirmed_at` | `breakout_confirmed_close` |
| H4/M15 | `breakout_time` | `breakout_close` |

### 4. Retest en timeframe menor

El retest ya NO requiere rechazo por mecha.

Retest válido = la vela del timeframe menor visita la zona:

```text
nivel_quebrado ± retest_tolerance_points
```

Importante:

- `retest_tolerance_points` son puntos, no pips.
- Default actual: `10.0` puntos.
- Para buy y sell se usa la misma idea: visitar la zona del nivel quebrado.

### 5. Trigger de entrada

Después del retest, la entrada se habilita solo si el timeframe menor confirma nuevamente a favor:

| Side | Condición |
|---|---|
| `buy` | cierre del timeframe menor `>` cierre confirmador del breakout |
| `sell` | cierre del timeframe menor `<` cierre confirmador del breakout |

Ejemplo M5/M1:

```text
buy: close M1 > breakout_confirmed_close
sell: close M1 < breakout_confirmed_close
```

Ejemplo H4/M15:

```text
buy: close M15 > breakout_close
sell: close M15 < breakout_close
```

## Invalidación de búsqueda de trigger

La búsqueda del trigger no queda viva para siempre.

Se invalida si el timeframe menor cierra demasiado en contra del nivel quebrado:

| Side | Condición de invalidación |
|---|---|
| `buy` | close menor `< nivel_quebrado - trigger_invalidation_points` |
| `sell` | close menor `> nivel_quebrado + trigger_invalidation_points` |

Default actual:

```text
trigger_invalidation_points = 200.0
```

También son puntos, no pips.

## Campos útiles para UI

### `leg_continuation_m5_m1`

```ts
type LegContinuationM5M1CurrentSetup = {
  side: "buy" | "sell";
  breakout_level: number;
  search_start: string;
  search_end: string;
  continuation_extended_at: string | null;
  breakout_confirmed_at: string | null;
  breakout_confirmed_close: number | null;
  entry_retest_done: boolean;
};
```

Campos recomendados para mostrar:

| Campo | Uso UI |
|---|---|
| `breakout_level` | nivel quebrado / zona de retest |
| `breakout_confirmed_at` | marca de breakout confirmado |
| `breakout_confirmed_close` | precio que debe superar/perforar el trigger menor |
| `entry_retest_done` | si ya visitó zona de retest |
| `setup_status_reason` | motivo actual/invalidation si está disponible |

### `leg_continuation_h4_m15`

```ts
type LegContinuationH4M15CurrentSetup = {
  side: "buy" | "sell";
  continuation_level: number;
  search_start: string;
  search_end: string;
  breakout_time: string | null;
  breakout_close: number | null;
};
```

Campos recomendados para mostrar:

| Campo | Uso UI |
|---|---|
| `continuation_level` | nivel quebrado / zona de retest |
| `breakout_time` | marca de breakout confirmado |
| `breakout_close` | precio que debe superar/perforar el trigger menor |

## Copy sugerido para estados

| Estado | Mensaje UI |
|---|---|
| Sin estructura | `Esperando estructura suficiente en timeframe mayor` |
| Setup activo | `Setup detectado: esperando breakout del nivel` |
| Breakout confirmado | `Breakout confirmado: esperando retest en timeframe menor` |
| Retest realizado | `Retest realizado: esperando cierre de confirmación` |
| Trigger invalidado | `Setup invalidado: el precio cerró contra el nivel de quiebre` |
| Entrada ejecutada | `Trigger confirmado: operación abierta` |

## Checklist de implementación front

- [ ] Mostrar el nivel quebrado como zona principal de retest.
- [ ] Mostrar el cierre confirmador como el umbral real del trigger.
- [ ] No llamar “trigger listo” solo por tocar la zona: falta cierre menor a favor.
- [ ] Tratar `retest_tolerance_points` y `trigger_invalidation_points` como puntos, no pips.
- [ ] En M5/M1, usar `entry_retest_done` para separar “esperando retest” vs “esperando trigger”.
- [ ] En H4/M15, si no existe flag de retest en runtime state, inferirlo solo si se agrega después al contrato.

## Regla mental simple

```text
Breakout mayor → vuelve a zona → cierre menor supera cierre del breakout → entrada.

Si cierra 200 puntos contra el nivel quebrado → se descarta.
```
