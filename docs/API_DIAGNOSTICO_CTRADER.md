# Diagnóstico API cTrader (`ctrade_v2`)

Fecha de diagnóstico: 2026-05-02  
Alcance: backend FastAPI + integración Open API cTrader + persistencia de bots/trades + rutas manuales.

## Resumen ejecutivo

El sistema tiene controles importantes (locks por símbolo en `open_trade`, verificación de SL/TP, reconciliación con cTrader), pero **todavía hay riesgos de producción de severidad alta** en idempotencia, consistencia de estado y observabilidad de fallas.  
Los puntos más delicados son:

1. Posible **arranque duplicado de runtime** para un mismo bot por falta de lock transaccional en `start_bot`.
2. Posible **confusión de correlación de órdenes** en alta concurrencia por `clientOrderId` generado con milisegundos.
3. **Chequeo y apertura no atómicos** en rutas manuales (`/manual/open`, `/manual/open-with-points`) con ventana de carrera.
4. Persistencia de trades con `ON CONFLICT` que puede **pisar campos sensibles** en reintentos/replays.
5. Uso extensivo de `except Exception` + `pass`, que puede ocultar fallas operativas reales y dejar estados inconsistentes sin alertar.

---

## Hallazgos críticos (ordenados por severidad)

## P0 - Riesgo inmediato de operación incorrecta / duplicada

### 1) `start_bot` no es idempotente ni está protegido contra carrera concurrente
- Referencias:
  - `app/bots/manager.py:185` (`start_bot`)
  - `app/bots/manager.py:199` (reemplazo directo en `self.bots[bot_id]`)
  - `app/api/routes.py:272` (endpoint `/bots/{bot_id}/start`)
- Riesgo:
  - Dos requests concurrentes de start (frontend duplicado, retry de gateway, doble click) pueden iniciar el mismo bot en paralelo.
  - Se pueden crear/recrear consumidores de eventos en secuencia no determinística y dejar tareas superpuestas por intervalos breves.
  - Resultado posible: duplicación de procesamiento de señales y decisiones de trading inconsistentes.
- Qué falta para verificar al 100%:
  - No hay evidencia de lock por `bot_id` en capa API ni de constraint de idempotencia externo (API gateway / frontend).

### 2) `clientOrderId` vulnerable a colisión temporal bajo concurrencia
- Referencias:
  - `app/broker/ctrader_market_data.py:1157` (registro en `_pending_orders`)
  - Generación por timestamp ms en `open_market_order` (misma función; bloque de creación de `clientOrderId`).
- Riesgo:
  - Si dos órdenes se emiten en el mismo milisegundo, una puede sobrescribir la otra en `_pending_orders`.
  - Puede resolverse mal el `Future`: orden A recibiendo evento de B o timeout falso de una orden ejecutada.
  - Efecto productivo: reconciliación equivocada, tracking errado de `position_id`, potencial cierre de posición incorrecta si falla SL/TP en la orden “equivocada”.
- Qué falta para verificar al 100%:
  - Volumen real de órdenes por segundo y si hay un identificador único adicional impuesto por cTrader.

### 3) Apertura manual con ventana de carrera (`check-then-act`)
- Referencias:
  - `app/api/routes_manual.py:263` y `:445` (`md_has_open_position`)
  - `app/api/routes_manual.py:271` y `:453` (apertura real)
- Riesgo:
  - Dos llamados concurrentes a `/manual/open` o `/manual/open-with-points` pueden pasar ambos el check antes de que exista la posición visible en reconcile.
  - Resultado: doble apertura no deseada para el mismo símbolo.
- Qué falta para verificar al 100%:
  - Si cTrader o cuenta tiene hard-rule externa de “una sola posición por símbolo” (en netting/hedging depende de configuración).

## P1 - Inconsistencias de estado backend/frontend/cTrader

### 4) `create_trade` con `ON CONFLICT` puede sobrescribir metadata y volumen en reintentos
- Referencias:
  - `app/db/repository.py:397` (`create_trade`)
  - `app/db/repository.py:444` (`ON CONFLICT ("positionId")`)
- Riesgo:
  - Ante reintentos o replays, el `UPDATE` pisa `volume`, `source`, `side`, `openedAt` y mergea metadata sin control de versión/evento.
  - Si el segundo write trae datos incompletos o distintos, el registro final puede divergir del evento real de cTrader.
- Impacto:
  - Frontend de historial/registro mostrando datos alterados respecto al broker.

### 5) `close_trade` no exige estado previo `OPEN`
- Referencias:
  - `app/db/repository.py:475` (`close_trade`)
  - `app/db/repository.py:492` (`status = 'CLOSED'`)
- Riesgo:
  - Re-cierres o cierres duplicados pueden volver a actualizar `closedAt`, `closeReason`, `metadata`.
  - Puede “reescribirse historia” de cierre en UI aunque la operación real fue única.

### 6) `restore_from_storage` depende de estado DB y no valida estado real en cTrader
- Referencias:
  - `app/bots/manager.py:334` (`restore_from_storage`)
  - `app/bots/manager.py:342` (`status != "RUNNING"`)
  - `app/db/repository.py:55` (`list_active_bots` trae todos no borrados)
- Riesgo:
  - Si DB dice RUNNING pero runtime previo murió en estado parcial, el bot revive sin reconciliar posiciones reales activas/pendientes.
  - Puede reanudar lógica sobre señales “frescas” sin contexto de exposición real.

### 7) Múltiples `except Exception` con supresión silenciosa
- Referencias representativas:
  - `app/main.py:43-44`, `55-56`
  - `app/api/routes.py:639-640`, `646-647`
  - `app/services/market_data_hub.py:279-284`
  - `app/services/internal_event_bus.py:41-46`
- Riesgo:
  - Errores de entrega de eventos, unsubscribe, alerting o persistencia quedan ocultos.
  - Frontend puede ver “todo ok” mientras se pierden eventos/bar closures o logs.

## P2 - Riesgos operativos y de mantenibilidad

### 8) Dependencia fuerte en estado in-memory para eventos/colas
- Referencias:
  - `app/services/internal_event_bus.py`
  - `app/services/market_data_hub.py`
- Riesgo:
  - Reinicios pierden colas/estado de listeners; no hay replay durable.
  - Apto para single-instance, frágil para escalado horizontal.

### 9) Polling de velas y builder de barras potencialmente desalineados
- Referencias:
  - `app/services/ctrader_client.py` (`_poll_candles`)
  - `app/services/market_data_hub.py` (`BarBuilder` en `_pump_symbol`)
- Riesgo:
  - Conviven dos fuentes de “bar close” (trendbars pull y ticks stream) según timeframe/flujo.
  - Posible discrepancia temporal en bordes de vela (especialmente M1/M5/M15) si reloj/latencia difiere.
- Qué falta para verificar:
  - Contrato único de origen de verdad para cierre de vela por estrategia.

### 10) Logs por `print` en producción sin estructura uniforme
- Referencias:
  - múltiples en `routes_manual.py`, `ctrader_client.py`, `ctrader_market_data.py`, `ctrader.py`
- Riesgo:
  - Difícil correlación por request/bot/order en incidentes.
  - Sin trace id consistente, RCA (root cause analysis) lento.

---

## Riesgos explícitos de operaciones duplicadas/incorrectas

1. Doble start de bot por carrera (`/bots/{id}/start`).
2. Doble open manual por carrera (`/manual/open*`).
3. Correlación errónea de execution event por colisión de `clientOrderId`.
4. Reescritura accidental de `BotTradeLog` en `ON CONFLICT`.
5. Re-cierre lógico de trade ya cerrado sin guard de estado.

---

## Riesgos de inconsistencias Frontend vs Backend vs cTrader

1. Backend marca RUNNING por DB, pero exposición real en cTrader no reconciliada al restaurar runtime.
2. Registro de trade puede quedar distinto al evento real por upsert agresivo.
3. Fallas silenciadas en event bus/hub pueden omitir eventos que frontend interpreta como “no hubo señal”.
4. Close price calculado por precio instantáneo y no por fill confirmado del broker (aproximación, no ejecución real).

---

## Información faltante para validar completamente

1. Modo de cuenta cTrader (netting vs hedging) y reglas exactas por símbolo.
2. Contrato de idempotencia en frontend/gateway (retry headers, request-id, debounce real).
3. Esquema Prisma/DB completo (constraints/triggers adicionales fuera de `repository.py`).
4. Métricas reales de concurrencia de órdenes/start requests.
5. Política de despliegue (single instance vs múltiples réplicas).

---

## Recomendaciones con prioridad y orden de implementación

1. **P0**: Agregar idempotencia + lock por `bot_id` en `start_bot`/`pause_bot`/`stop_bot` y en endpoints manuales de apertura por símbolo.
2. **P0**: Reemplazar `clientOrderId` por UUID/ULID criptográficamente único, conservar mapping y trazabilidad.
3. **P1**: Endurecer `BotTradeLog`:
   - `close_trade` con `WHERE status='OPEN'`.
   - `create_trade` sin sobrescribir campos críticos si ya existe evento más confiable.
4. **P1**: Implementar reconciliación explícita startup-runtime con cTrader (posiciones abiertas + estado bots).
5. **P1**: Reducir `except Exception: pass`; convertir en logs estructurados con severidad + contexto.
6. **P2**: Definir un único origen de “bar close” por estrategia/timeframe para evitar drift.
7. **P2**: Instrumentación observabilidad (request_id, bot_id, position_id, client_order_id en todos los logs).

---

## Veredicto

El backend **no está en estado “seguro por defecto” para alta concurrencia**.  
Puede funcionar correctamente en condiciones normales, pero bajo retries, latencia o paralelismo hay caminos reales para duplicaciones e inconsistencias con cTrader.

Se recomienda tratar los puntos P0 como bloqueantes antes de aumentar tráfico o automatización en producción.
