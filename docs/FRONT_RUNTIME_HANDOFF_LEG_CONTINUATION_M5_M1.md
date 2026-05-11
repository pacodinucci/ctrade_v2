# Frontend Handoff — Ajuste contrato runtime (`leg_continuation_m5_m1`)

## Contexto
Backend cambió la semántica del setup en runtime para `leg_continuation_m5_m1`:
- Se eliminó `continuation_level` del `current_setup`.
- Se mantiene un único nivel operativo: `breakout_level`.
- La lógica de breakout quedó alineada a continuidad sobre la **anteúltima leg confirmada**.

---

## Qué cambió en el contrato

### Antes (`current_setup`)
- `continuation_level`
- `breakout_level`

### Ahora (`current_setup`)
- `breakout_level` (único nivel)

Además, el payload runtime puede incluir:
- `setup_status_reason`: `active | expired_window | invalidated_structure | stream_gap`
- `server_now_utc`: timestamp UTC de servidor
- `setup_invalidated_at`: timestamp UTC cuando se invalida setup (si aplica)

---

## Acción requerida en frontend

1. Reemplazar todas las lecturas de:
   - `current_setup.continuation_level`
   por:
   - `current_setup.breakout_level`

2. Actualizar tipados/interfaces del runtime state para `leg_continuation_m5_m1`:
   - eliminar `continuation_level`
   - mantener `breakout_level`
   - agregar (si aún no están) `setup_status_reason`, `server_now_utc`, `setup_invalidated_at`

3. Ajustar labels/UI:
   - donde diga “continuation level”, mostrar “breakout level” (o “nivel de quiebre”).

4. Validar notificaciones/estados:
   - no asumir expiración silenciosa;
   - usar `setup_status_reason` para diagnóstico visual si corresponde.

---

## Ejemplo de shape sugerido (TypeScript)

```ts
type SetupStatusReason =
  | "active"
  | "expired_window"
  | "invalidated_structure"
  | "stream_gap";

type LegContinuationCurrentSetup = {
  side: "buy" | "sell";
  breakout_level: number;
  search_start: string; // ISO UTC
  search_end: string;   // ISO UTC
  continuation_extended_at: string | null;
  breakout_confirmed_at: string | null;
  entry_retest_done: boolean;
};

type LegContinuationM5M1Runtime = {
  strategy: "leg_continuation_m5_m1";
  symbol: string;
  stage: "WAITING_M5_LEGS" | "WAITING_M5_BREAKOUT" | "WAITING_M1_ENTRY";
  pending_setups_count: number;
  server_now_utc: string;
  setup_status_reason: SetupStatusReason;
  setup_invalidated_at?: string;
  current_setup?: LegContinuationCurrentSetup;
};
```

---

## Criterio de aceptación front

1. No quedan referencias a `continuation_level` en `leg_continuation_m5_m1`.
2. UI muestra correctamente `breakout_level` como único nivel.
3. Runtime renderiza sin errores con snapshots nuevos del backend.
4. Estados de diagnóstico se pueden inspeccionar con `setup_status_reason`.
