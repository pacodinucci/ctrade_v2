# Frontend - Mostrar etapa de estrategia por bot (Trading Control Center)

## Objetivo
Mostrar en la grilla principal de bots una columna **ETAPA** para saber en tiempo real en que paso de estrategia esta cada bot activo.

---

## Estado backend (ya disponible)
El endpoint `GET /bots` ya devuelve por cada bot:
- `strategyRuntimeStage` (string o `null`)
- `strategyRuntimeState` (objeto opcional)

Ejemplo real:
- `WAITING_M5_LEGS`
- `WAITING_M5_BREAKOUT`
- `WAITING_M1_ENTRY`

---

## Cambios a hacer en frontend

1. Agregar columna nueva en la tabla:
- Header: `ETAPA`
- Ubicacion sugerida: entre `RUNTIME` y `LAST ERROR` (o donde mejor cierre visualmente).

2. En cada fila, leer:
- `row.strategyRuntimeStage`

3. Si `strategyRuntimeStage` es `null`:
- Mostrar `-` si `runtimeActive=false`.
- Mostrar `Inicializando` si `runtimeActive=true`.

4. Traducir labels para que sean legibles:
- `WAITING_M5_LEGS` -> `Esperando estructura M5`
- `WAITING_M5_BREAKOUT` -> `Esperando breakout M5`
- `WAITING_M1_ENTRY` -> `Esperando entrada M1`
- fallback -> valor crudo del backend

5. Recomendado UX:
- Mostrar como badge/chip con color:
  - gris: `WAITING_M5_LEGS`
  - azul: `WAITING_M5_BREAKOUT`
  - naranja: `WAITING_M1_ENTRY`

---

## Contrato TypeScript sugerido

```ts
export type BotRow = {
  id: string;
  name: string;
  instrument: string;
  strategy: string;
  status: string;
  runtimeActive: boolean;
  strategyRuntimeStage: string | null;
  strategyRuntimeState?: Record<string, unknown>;
  lastError?: string | null;
  updatedAt?: string;
};
```

---

## Helpers sugeridos

```ts
export function mapStageLabel(stage: string | null, runtimeActive: boolean): string {
  if (!stage) return runtimeActive ? "Inicializando" : "-";

  switch (stage) {
    case "WAITING_M5_LEGS":
      return "Esperando estructura M5";
    case "WAITING_M5_BREAKOUT":
      return "Esperando breakout M5";
    case "WAITING_M1_ENTRY":
      return "Esperando entrada M1";
    default:
      return stage;
  }
}

export function mapStageTone(stage: string | null): "neutral" | "info" | "warn" {
  switch (stage) {
    case "WAITING_M5_LEGS":
      return "neutral";
    case "WAITING_M5_BREAKOUT":
      return "info";
    case "WAITING_M1_ENTRY":
      return "warn";
    default:
      return "neutral";
  }
}
```

---

## Ejemplo de render en celda

```tsx
const stageLabel = mapStageLabel(bot.strategyRuntimeStage, !!bot.runtimeActive);
const stageTone = mapStageTone(bot.strategyRuntimeStage);

<StageBadge tone={stageTone}>{stageLabel}</StageBadge>
```

---

## Checklist de validacion

1. Verificar que `GET /bots` trae `strategyRuntimeStage`.
2. Refrescar pantalla de Control Center.
3. Confirmar que bots activos ya muestran ETAPA.
4. Confirmar fallback para bots pausados/detenidos.
5. Validar que no rompa filtros/search de tabla.

---

## Nota importante
No hace falta llamar `GET /bots/{id}` para poblar etapa en la grilla principal.
Con `GET /bots` alcanza.
