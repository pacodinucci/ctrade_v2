# Optimización de market data para 27 bots simultáneos en cTrader Open API

## Objetivo

Mantener la ejecución de **27 bots simultáneos**, uno por cada par de divisas, **sin depender de bots dentro de la UI de cTrader** y evitando caer en **rate limit**, **timeouts de autenticación** y **polling redundante**.

La idea central es pasar de este modelo incorrecto:

- 27 bots
- 27 consumidores directos de cTrader
- polling independiente por bot
- requests repetidas por símbolo/timeframe/cuenta

A este modelo correcto:

- **1 sola conexión cTrader compartida**
- **1 sola autenticación de app y de cuenta**
- **1 capa centralizada de market data**
- **27 bots como consumidores internos del backend**
- **streaming + cache + agregación local de velas**

---

## Problema actual

Actualmente el sistema presenta síntomas compatibles con una arquitectura demasiado acoplada a la API:

- múltiples bots intentando consultar mercado en paralelo
- timeouts esperando `Account AUTH`
- exceso de polling
- repetición de requests históricas y/o de estado
- riesgo alto de caer en límites de la Open API

Aunque cada bot opere un instrumento distinto, el problema de fondo no es la cantidad de bots como entidad lógica, sino la forma en que acceden a los datos.

---

## Aclaración clave: 27 instrumentos distintos

Sí, cada uno de los 27 bots usa un instrumento distinto.

Eso **sí aumenta el volumen de market data**, pero **no cambia la arquitectura recomendada**.

### Lo que sí significa

- hay que manejar **27 símbolos activos**
- hay que mantener **cache por símbolo**
- hay que bootstrapear histórico por símbolo
- el volumen de ticks/eventos será mayor

### Lo que NO significa

- no hace falta 1 cliente por bot
- no hace falta 1 autenticación por bot
- no hace falta polling independiente por bot
- no hace falta pedir M1/M5/M15 a cTrader todo el tiempo para cada símbolo

Conclusión:

> **27 instrumentos distintos = 27 fuentes lógicas de mercado**
>
> **No = 27 conexiones / 27 pollers / 27 sesiones independientes**

---

## Principio de diseño

Los bots **no deben hablar directamente con cTrader**.

Los bots deben consumir datos desde una capa interna del backend.

### Regla operativa

Se debe aplicar esta regla:

- **1 request o suscripción por dato único**
- **N consumidores internos reutilizando ese dato**

Nunca:

- **N requests iguales o equivalentes por N bots**

---

## Arquitectura propuesta

## 1. `CTraderConnectionManager`

Responsable de mantener la sesión única con cTrader.

### Responsabilidades

- abrir la conexión
- realizar app auth
- realizar account auth
- exponer estado interno de la sesión
- manejar reconnects y reintentos
- bloquear el uso de mercado/trading hasta que el estado sea `READY`

### Estado sugerido

- `DISCONNECTED`
- `CONNECTING`
- `APP_AUTHENTICATED`
- `ACCOUNT_AUTHENTICATED`
- `READY`
- `RECONNECTING`
- `ERROR`

### Regla importante

Ningún bot debe procesar mercado ni pedir datos si la conexión no está en `READY`.

---

## 2. `MarketDataHub`

Capa centralizada de recepción y distribución de datos de mercado.

### Responsabilidades

- suscribirse una sola vez a cada símbolo activo
- recibir ticks / spot events
- mantener el último bid/ask por símbolo
- publicar eventos internos
- abastecer el cache que consumen las estrategias

### Idea principal

Si hay 27 bots con 27 símbolos distintos, el `MarketDataHub` administra esas 27 suscripciones.

Los bots no se suscriben individualmente.

---

## 3. `SymbolStateCache`

Cache en memoria por símbolo.

### Estructura sugerida por símbolo

- `symbol`
- `last_bid`
- `last_ask`
- `last_tick_time`
- buffer de velas `M1`
- buffer de velas `M5`
- buffer de velas `M15`
- flags de bootstrap completo
- metadata de sincronización

### Objetivo

Convertir a cTrader en la fuente de entrada y al cache interno en la fuente de consumo para los bots.

---

## 4. `BarBuilder`

Responsable de construir velas localmente.

### Responsabilidades

- formar velas M1 a partir de ticks
- derivar M5 desde M1
- derivar M15 desde M1 o desde M5
- emitir evento `bar_closed(symbol, timeframe, bar)`

### Beneficio

Evita pedir repetidamente a cTrader datos OHLC de múltiples timeframes para cada bot.

Eso reduce muchísimo el uso de requests históricas o consultas periódicas.

---

## 5. `StrategyEngine`

Contiene las 27 instancias lógicas de estrategia.

### Responsabilidades

- registrar un bot por símbolo
- escuchar eventos del `MarketDataHub` o del `BarBuilder`
- ejecutar lógica de entrada/salida
- producir señales

### Importante

Los bots deben ser **consumidores internos de eventos**, no productores de tráfico contra cTrader.

---

## 6. `ExecutionEngine`

Responsable de enviar órdenes y sincronizar estado operativo.

### Responsabilidades

- recibir señales de estrategia
- validar condiciones de riesgo
- serializar órdenes cuando sea necesario
- enviar órdenes a cTrader
- reconciliar fills, SL, TP y cierres

---

## Flujo recomendado

### Paso 1: Inicialización

- levantar `CTraderConnectionManager`
- conectar a cTrader
- autenticar app
- autenticar cuenta
- esperar estado `READY`

### Paso 2: Bootstrap de símbolos

- identificar lista de símbolos activos
- pedir histórico mínimo **una sola vez por símbolo**
- cargar buffers iniciales
- marcar bootstrap como completo

### Paso 3: Suscripción live

- suscribirse a eventos live de los 27 símbolos
- actualizar `SymbolStateCache`
- construir velas localmente

### Paso 4: Ejecución estratégica

- cuando cierre una vela relevante, emitir evento interno
- el bot correspondiente consume ese evento
- si hay señal, enviarla al `ExecutionEngine`

### Paso 5: Monitoreo de cuenta

- sincronizar cuenta, posiciones y PnL con frecuencia controlada
- exponer snapshots cacheados al resto del sistema

---

## Cambio conceptual fundamental

### Diseño incorrecto

Cada bot hace algo así:

- consulta precio
- consulta velas
- consulta cuenta
- consulta posiciones
- repite polling todo el tiempo

### Diseño correcto

El sistema hace esto:

- 1 conexión compartida
- 1 feed de mercado
- 1 cache centralizado
- eventos internos
- bots reaccionando a datos internos ya disponibles

---

## Reglas de optimización

## Regla 1: un solo cliente cTrader

Debe existir **un único `cTraderClient` compartido** por todo el backend.

No debe existir un cliente por bot.

---

## Regla 2: una sola auth de cuenta

La autenticación de cuenta debe ocurrir una sola vez por sesión.

Los bots no deben iniciar flujo de auth.

---

## Regla 3: suscripción por símbolo, no por bot

Para cada símbolo activo:

- una sola suscripción
- múltiples consumidores internos si hace falta

En tu caso actual, como hay un bot por símbolo, igual sigue siendo válido:

- 27 símbolos
- 27 suscripciones lógicas
- 1 capa centralizada

---

## Regla 4: evitar polling para señales basadas en velas

Si la estrategia se activa por cierres de vela o estructuras definidas al cierre:

- no hacer polling frecuente
- usar eventos `bar_closed`

Eso reduce muchísimo la carga.

---

## Regla 5: bootstrap histórico único

El histórico debe pedirse:

- una sola vez por símbolo
- con el mínimo necesario
- preferentemente escalonado en el arranque

No debe volver a pedirse por cada bot ni por cada ciclo.

---

## Regla 6: derivar timeframes localmente

Siempre que sea posible:

- construir M1 desde ticks
- construir M5 desde M1
- construir M15 desde M1 o M5

No pedir todos los timeframes a la API de forma reiterada.

---

## Regla 7: cachear estado de cuenta

Saldo, equity, margen, posiciones y PnL no deben ser consultados a demanda desde cada bot.

Debe existir un `AccountStateCache` interno.

Los bots leen snapshots internos.

---

## Regla 8: separar market data de account polling

Dos capas distintas:

### Market data

- continua
- por streaming / suscripción
- alta frecuencia

### Estado de cuenta

- baja frecuencia relativa
- sincronización controlada
- cacheada

---

## Qué NO hacer

## 1. No crear 27 clientes independientes

Eso multiplica:

- conexiones
- auth
- reconnects
- complejidad de estado
- riesgo de timeout

## 2. No hacer polling de velas por bot

Especialmente si cada bot pide M1, M5 y M15 por separado.

## 3. No pedir histórico repetido

El histórico es bootstrap, no combustible continuo.

## 4. No consultar PnL/cuenta desde la lógica de estrategia

La estrategia debe trabajar con snapshots internos.

## 5. No iniciar los bots antes de que la sesión esté lista

Si el sistema no llegó a `ACCOUNT_AUTHENTICATED` / `READY`, los bots deben permanecer bloqueados.

---

## Modelo interno recomendado

## Estructuras principales

### `ConnectionState`

- estado de conexión
- timestamps de última auth
- errores recientes
- número de reconnects

### `SymbolState`

- símbolo
- bid/ask actual
- timestamp último tick
- buffers de velas
- bootstrap status

### `AccountSnapshot`

- balance
- equity
- free margin
- used margin
- open positions
- pending orders
- timestamp última sync

### `BotRuntime`

- botId
- símbolo
- timeframe principal
- estado (`idle`, `armed`, `in_position`, etc.)
- referencia a strategy config

---

## Modelo de eventos internos

Se recomienda usar una cola o bus interno de eventos.

### Eventos sugeridos

- `tick_received(symbol, tick)`
- `bar_closed(symbol, timeframe, bar)`
- `account_snapshot_updated(snapshot)`
- `position_opened(position)`
- `position_closed(position)`
- `order_rejected(order, reason)`
- `connection_state_changed(state)`

### Consumo

- `StrategyEngine` escucha `bar_closed`
- `ExecutionEngine` escucha señales
- `MonitoringService` escucha todo para logs y métricas

---

## Estrategia de arranque recomendada

Para no golpear la API de forma brusca al inicio:

### En vez de esto

- iniciar 27 bots al mismo tiempo
- pedir histórico para todos instantáneamente
- empezar polling apenas arranca la app

### Hacer esto

1. conectar y autenticar
2. bootstrapear histórico por lotes pequeños
3. suscribir live por lotes si hace falta
4. habilitar bots solo cuando su símbolo esté listo

---

## Implicancias para FastAPI

Se recomienda separar claramente servicios de background.

### Servicios sugeridos

- `connection_service`
- `market_data_service`
- `bar_builder_service`
- `account_sync_service`
- `strategy_runtime_service`
- `execution_service`
- `monitoring_service`

### En la app FastAPI

FastAPI debe usarse principalmente para:

- exponer endpoints de control
- consultar estado de bots
- ver snapshots de mercado/cuenta
- activar o desactivar estrategias
- recibir logs/eventos

FastAPI no debería ser el lugar donde cada request HTTP dispara consultas directas a cTrader.

---

## Resultado esperado después de esta optimización

Si se implementa correctamente, el sistema debería lograr:

- eliminar timeouts de auth originados por competencia entre bots
- reducir drásticamente el número de requests redundantes
- evitar rate limit por polling innecesario
- mantener 27 bots activos de forma razonable
- desacoplar market data de lógica estratégica
- mejorar observabilidad y estabilidad del runtime

---

## Conclusión

El hecho de tener **27 bots para 27 instrumentos distintos** **no obliga** a diseñar 27 consumidores directos de cTrader.

La optimización correcta consiste en transformar el backend en un **motor centralizado de market data**, donde:

- cTrader se consulta una sola vez por cada dato realmente necesario
- los datos se cachean y se distribuyen internamente
- las estrategias reaccionan a eventos internos
- la ejecución se desacopla de la adquisición de datos

### Resumen ejecutivo

La solución no es reducir bots.

La solución es:

- **centralizar conexión**
- **centralizar auth**
- **centralizar market data**
- **agregar velas localmente**
- **convertir bots en consumidores internos**

---

## Próximo paso sugerido

Traducir esta arquitectura a implementación concreta en Python/FastAPI con:

- clases
- responsabilidades
- lifecycle de arranque
- manejo de reconnect
- cache en memoria
- eventos internos
- integración con estrategias existentes
