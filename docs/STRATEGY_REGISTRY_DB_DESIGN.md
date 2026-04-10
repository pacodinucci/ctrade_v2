# Strategy Registry En Base De Datos (Diseño)

## 1. Objetivo
Definir una arquitectura para mover el **registro de estrategias** desde código a base de datos, habilitando:

1. Estrategias existentes (nativas, en Python) sin romper comportamiento actual.
2. Estrategias creadas desde cliente (declarativas) sin necesidad de deploy por cada nueva estrategia.
3. Contrato común para validación, runtime, dry-run, versionado y auditoría.

Este documento es la fuente de verdad para implementar la modificación en etapas.

---

## 2. Estado Actual (Resumen)
Actualmente:

1. El registro está en [`app/strategies/registry.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\strategies\registry.py).
2. Cada estrategia define:
   - Metadata (`id`, `name`, params).
   - Validación/normalización de params.
   - Factory de runtime (`create_runtime`).
   - Cálculo de plan (`build_plan`) para dry-run.
3. `BotManager` resuelve la estrategia por `strategy_id` y ejecuta su runtime.
4. Las estrategias activas son clases Python concretas.

Limitación principal: para agregar una estrategia nueva hay que tocar código y desplegar.

---

## 3. Objetivos Funcionales De La Nueva Arquitectura

1. Permitir estrategias creadas y versionadas desde el cliente.
2. Mantener soporte total para estrategias nativas actuales.
3. Separar claramente:
   - Definición de estrategia (datos).
   - Ejecución de estrategia (runtime).
4. Tener versionado inmutable por trazabilidad.
5. Permitir activar/desactivar versiones de estrategia.
6. Compartir lógica transversal (tools, storage runtime, guardrails).
7. Asegurar validación estricta y seguridad (sin código arbitrario de usuario).

---

## 4. No Objetivos (Por Ahora)

1. No ejecutar Python/JS arbitrario subido por usuarios.
2. No reemplazar de inmediato todas las estrategias nativas.
3. No introducir backtesting complejo en esta fase inicial.
4. No cambiar la semántica core de lifecycle (`start/pause/stop`) de bots.

---

## 5. Arquitectura Propuesta (Híbrida)

### 5.1 Tipos De Estrategia

1. `native`
   - Lógica en código Python (como hoy).
   - DB guarda metadata/versionado/referencias.
2. `declarative`
   - Lógica en `runtime_spec` (JSON/DSL).
   - Motor runtime interpreta/compila ese spec usando herramientas permitidas.

### 5.2 Resolución En Runtime

1. Bot carga su `strategy_version_id`.
2. Se obtiene `strategy_definition` + `strategy_version`.
3. Según `engine_type`:
   - `native` -> factory Python conocida.
   - `declarative` -> compilador/engine declarativo.
4. Ambos caminos consumen la misma infraestructura:
   - `MarketDataHub`
   - `InternalEventBus`
   - ejecución de órdenes vía `cTraderClient`
   - logging en DB

---

## 6. Modelo De Datos (DB)

## 6.1 Tabla `strategy_definitions`
Representa la estrategia “conceptual” (identidad estable).

Campos sugeridos:

1. `id` UUID PK
2. `key` TEXT UNIQUE NOT NULL
3. `name` TEXT NOT NULL
4. `description` TEXT NULL
5. `engine_type` TEXT NOT NULL CHECK (`native` | `declarative`)
6. `status` TEXT NOT NULL CHECK (`draft` | `active` | `archived`)
7. `owner_user_id` TEXT NULL (NULL para estrategias globales)
8. `created_at` TIMESTAMP NOT NULL
9. `updated_at` TIMESTAMP NOT NULL

Índices:

1. UNIQUE (`key`)
2. INDEX (`owner_user_id`, `status`)
3. INDEX (`engine_type`, `status`)

## 6.2 Tabla `strategy_versions`
Versiones inmutables de cada definición.

Campos sugeridos:

1. `id` UUID PK
2. `strategy_definition_id` UUID FK -> `strategy_definitions(id)`
3. `version` INT NOT NULL
4. `params_schema` JSONB NOT NULL
5. `runtime_spec` JSONB NOT NULL
6. `risk_limits` JSONB NOT NULL DEFAULT `'{}'::jsonb`
7. `is_active` BOOL NOT NULL DEFAULT FALSE
8. `checksum` TEXT NOT NULL
9. `change_log` TEXT NULL
10. `created_by` TEXT NULL
11. `created_at` TIMESTAMP NOT NULL

Restricciones:

1. UNIQUE (`strategy_definition_id`, `version`)
2. (Recomendado) trigger para garantizar **una sola versión activa** por definición.
3. Inmutabilidad lógica: no editar `runtime_spec` ni `params_schema` de versiones creadas.

Índices:

1. INDEX (`strategy_definition_id`, `is_active`)
2. INDEX (`created_at DESC`)

## 6.3 Bot: referencia a versión
Extender tabla de bots (actual `public."Bot"`):

1. `strategyVersionId` UUID NULL FK -> `strategy_versions(id)`
2. `strategyEngineType` TEXT NULL (`native` | `declarative`) opcional para consultas rápidas

Notas:

1. Durante transición convivirá con columnas actuales (`strategy`, `strategyParams`).
2. El objetivo final es que cada bot apunte a una versión concreta.

## 6.4 Tabla opcional `strategy_tools_catalog`
Catálogo de herramientas permitidas para estrategias declarativas.

Campos:

1. `tool_key` TEXT PK
2. `category` TEXT NOT NULL (`signal`, `entry`, `risk`, `filter`, `execution`, `time`)
3. `input_schema` JSONB NOT NULL
4. `config_schema` JSONB NOT NULL
5. `status` TEXT NOT NULL (`active`, `deprecated`)
6. `created_at`, `updated_at`

---

## 7. Contrato Declarativo (`runtime_spec`)

`runtime_spec` es JSON y define comportamiento sin código arbitrario.

## 7.1 Estructura base propuesta

```json
{
  "timeframes": ["H4", "M15"],
  "signal": {
    "tool": "three_plus_one_reversal",
    "config": {
      "source_tf": "H4",
      "doji_points": 9
    }
  },
  "filters": [
    {
      "tool": "friday_cutoff",
      "config": {
        "hour": 10,
        "tz": "UTC"
      }
    }
  ],
  "entry": {
    "tool": "close_break_trigger",
    "config": {
      "entry_tf": "M15",
      "window_bars": 16
    }
  },
  "risk": {
    "tool": "fixed_points",
    "config": {
      "sl_points_param": "sl_points",
      "tp_points_param": "tp_points"
    }
  },
  "position_rules": {
    "max_open_positions_per_symbol": 1,
    "cooldown_sec": 0
  },
  "exit_rules": []
}
```

## 7.2 `params_schema` (ejemplo)

```json
{
  "type": "object",
  "required": ["volume", "sl_points", "tp_points"],
  "properties": {
    "volume": { "type": "number", "minimum": 100000 },
    "sl_points": { "type": "integer", "minimum": 1, "default": 100 },
    "tp_points": { "type": "integer", "minimum": 1, "default": 200 }
  },
  "additionalProperties": false
}
```

Reglas:

1. `params_values` del bot deben validar contra `params_schema`.
2. Se completan defaults declarados.
3. Se normalizan tipos antes de ejecutar runtime.

---

## 8. APIs Propuestas

## 8.1 Definiciones y versiones

1. `GET /strategies`
   - Lista definiciones + versión activa + metadata.
2. `POST /strategies`
   - Crea definición (`draft`).
3. `GET /strategies/{strategy_id}`
   - Detalle definición + versiones.
4. `POST /strategies/{strategy_id}/versions`
   - Crea nueva versión inmutable.
5. `POST /strategies/{strategy_id}/activate/{version}`
   - Activa una versión.
6. `POST /strategies/{strategy_id}/archive`
   - Archiva definición.

## 8.2 Validación y pruebas

1. `POST /strategies/{strategy_id}/validate`
   - Valida `params_schema`, `runtime_spec`, guardrails y compatibilidad de tools.
2. `POST /strategies/{strategy_id}/dry-run`
   - Simula plan de trade con side/entry.

## 8.3 Bots

1. `POST /bots`
   - Acepta `strategyVersionId` (nuevo) y opcionalmente `strategy` legacy durante transición.
2. `PATCH /bots/{bot_id}`
   - Permite cambio de estrategia por `strategyVersionId`.
3. `GET /bots/{bot_id}`
   - Devuelve `strategyVersionId`, `strategyDefinitionKey`, `engineType`.

---

## 9. Runtime Unificado (Diseño)

## 9.1 Contrato mínimo de runtime

Todos los runtimes deben exponer:

1. `required_timeframes` (si aplica, default H4/M15)
2. Handlers por timeframe usados:
   - `on_h4_close`, `on_m15_close`, `on_m5_close`, `on_m1_close`
3. Opcionales:
   - `on_start`
   - `shutdown`
   - `get_runtime_state`

## 9.2 Herramientas compartidas

Mover lógica común a módulos reutilizables:

1. `SignalTools`
2. `EntryTools`
3. `RiskTools`
4. `PositionTools`
5. `TimeTools`
6. `ValidationTools`

Regla de diseño:

1. Unificar contrato de tools.
2. No forzar que todas las estrategias tengan la misma implementación interna.

---

## 10. Seguridad Y Guardrails

1. Prohibido ejecutar código arbitrario de usuario.
2. Solo tools del catálogo permitido.
3. Validación estricta de schema JSON antes de persistir versiones.
4. Límite de riesgo por estrategia/cuenta/usuario:
   - `max_sl_points`
   - `max_volume`
   - `max_open_positions_per_symbol`
5. Rechazo de estrategias sin `dry-run` válido.
6. Auditoría de cambios y activaciones (quién/cuándo/qué versión).

---

## 11. Migración Por Fases

## Fase 0 - Preparación

1. Crear tablas nuevas (`strategy_definitions`, `strategy_versions`).
2. Crear índices/constraints.
3. No cambiar comportamiento runtime.

## Fase 1 - Espejado de nativas

1. Registrar estrategias actuales como `engine_type=native` en DB.
2. Crear versión 1 por cada estrategia existente.
3. `registry.py` sigue siendo runtime source-of-truth para `native`.

## Fase 2 - Bots con `strategyVersionId`

1. Añadir columna `strategyVersionId` en `Bot`.
2. Backfill desde `strategy` actual hacia versión activa equivalente.
3. APIs aceptan ambos contratos (nuevo y legacy).

## Fase 3 - Resolver por DB primero

1. `BotManager` consulta DB para definición/versión.
2. Para `native`, mapea `definition.key` a factory Python existente.
3. `registry.py` queda como fallback interno/controlado.

## Fase 4 - Primer declarative MVP

1. Implementar 1 estrategia declarativa simple.
2. Validación + dry-run + ejecución controlada.
3. Monitorear estabilidad.

## Fase 5 - Consolidación

1. Extraer más lógica compartida a tools comunes.
2. Reducir dependencia del registry estático en código.
3. Mantener nativas necesarias por performance/complejidad.

---

## 12. Compatibilidad Hacia Atrás

Durante transición:

1. `POST /bots` legacy (`strategy` + `strategyParams`) sigue funcionando.
2. Internamente se resuelve a `strategyVersionId` cuando sea posible.
3. `GET /strategies` puede mezclar fuente DB + nativas legacy al principio.

Deuda temporal aceptada:

1. Doble contrato de creación de bots.
2. Doble fuente parcial de metadata hasta cerrar migración.

---

## 13. Criterios De Aceptación

1. Se puede crear estrategia (definición + versión) desde API sin deploy.
2. Se puede activar versión y crear bots con esa versión.
3. Bots existentes siguen funcionando sin cambios de comportamiento.
4. `dry-run` funciona tanto para `native` como `declarative`.
5. Hay trazabilidad completa de versión usada por cada bot.
6. Guardrails de validación y riesgo bloquean configuraciones inválidas.

---

## 14. Riesgos Técnicos

1. Complejidad de DSL si se intenta cubrir demasiado en v1.
2. Incompatibilidad entre estrategia declarativa y latencia runtime si el engine es muy genérico.
3. Riesgo de “abstracción prematura” en tools compartidas.
4. Migrações de DB incompletas (bots sin versión asociada).

Mitigación:

1. MVP declarativo mínimo y vertical.
2. Feature flags por `engine_type`.
3. Pruebas de contrato y migración.
4. Rollback plan: fallback a `native`.

---

## 15. Preguntas Abiertas (Para Resolver Antes De Implementar)

1. ¿Qué nivel de edición de estrategias tendrá el cliente final en v1 (solo params o lógica completa declarativa)?
2. ¿Las estrategias son globales, por usuario, o ambas?
3. ¿Se permite activar varias versiones por entorno (dev/prod)?
4. ¿Cómo se modelan permisos de edición/publicación?
5. ¿Qué subconjunto exacto de tools entra en el MVP?

---

## 16. Checklist De Implementación (Para Usar En Próxima Instrucción)

1. Crear migraciones SQL de tablas/columnas nuevas.
2. Implementar repositorio DB para estrategias y versiones.
3. Agregar endpoints CRUD/versionado/activación.
4. Integrar `BotManager` con resolución por `strategyVersionId`.
5. Mantener fallback legacy.
6. Añadir validación de `params_schema` y `runtime_spec`.
7. Implementar `dry-run` unificado.
8. Añadir logs y auditoría de cambios de estrategia.
9. Escribir tests:
   - unitarios de validación/compilación
   - integración API
   - migración/backfill
10. Ejecutar rollout por fases con feature flag.

---

## 17. Referencias Del Código Actual

1. Registry actual: [`app/strategies/registry.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\strategies\registry.py)
2. Orquestación runtime: [`app/bots/manager.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\bots\manager.py)
3. Persistencia bots: [`app/db/repository.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\db\repository.py)
4. API bots/strategies: [`app/api/routes.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\api\routes.py)
5. Market runtime:
   - [`app/services/market_data_hub.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\services\market_data_hub.py)
   - [`app/services/internal_event_bus.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\services\internal_event_bus.py)
   - [`app/services/bar_builder.py`](C:\Users\franc\OneDrive\Escritorio\Proyectos\ctrade_v2\app\services\bar_builder.py)

---

## 18. Decisión Arquitectónica Recomendada

Adoptar el modelo híbrido:

1. **DB como source-of-truth de metadata/versiones**.
2. **Nativas en código** durante transición.
3. **Declarativas** para nuevas estrategias generadas por cliente.

Esto permite evolucionar sin romper producción y habilita el roadmap de “estrategias desde cliente”.
