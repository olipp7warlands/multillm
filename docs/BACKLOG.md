# AIhub · Backlog Fase 1
Trabajar en orden. Marcar `[x]` al completar. Cada ticket termina con tests en verde.
DoD global: `alembic upgrade head` limpio · `pytest` verde · `npm run typecheck` verde.

## SPIKE (previo, timebox 2-3 días)
- [x] **SP-1 · litellm como librería.** Script que hace streaming con
      litellm.acompletion() contra 2 proveedores con keys distintas por llamada,
      y verifica que el `usage` final (tokens exactos) llega en streaming.
      Medir latencia añadida vs SDK nativo. Salida: `docs/spike.md`.
      Verificado en vivo con Anthropic y Gemini. Hallazgo crítico: el usage en
      streaming NO llega salvo que se pase `stream_options={"include_usage": true}`
      — para AMBOS proveedores, no solo para los normalizados a OpenAI. Sin overhead
      sistemático de litellm frente a las SDKs nativas. Detalle en `docs/spike.md`.
- [x] **SP-2 · Presidio en proceso.** pip presidio-analyzer/anonymizer + spaCy
      es_core_news_md; benchmark p95 de un prompt de 500 tokens con 200 términos
      custom en un recognizer. Objetivo: p95 < 150 ms y arranque del engine < 15 s.
      Salida en `docs/spike.md` (si el arranque pesa, precargar en lifespan de FastAPI).
      Arranque OK (1-3 s). **p95 real ~290-300 ms, NO cumple el objetivo de 150 ms**
      en este equipo de desarrollo — ver diagnóstico y decisión en `docs/spike.md`;
      revalidar en specs de Railway antes de S1-9.
- [x] **SP-3 · Supabase RLS + pooler.** Contra el proyecto Supabase: crear rol
      `app_backend` sin bypassrls, una tabla con política por `app.tenant_id`,
      y verificar desde asyncpg (statement_cache_size=0) que SET LOCAL dentro de
      transacción aísla correctamente a través del pooler. Salida en `docs/spike.md`.
      Verificado en session y transaction mode. **Hallazgo crítico para S1-2**: toda
      política RLS con `current_setting('app.tenant_id')` debe usar `CASE WHEN`
      (nunca `AND ... <> ''` antes del cast) — entre transacciones el pooler deja el
      GUC en `''` en vez de `NULL`, y Postgres no garantiza el orden de evaluación de
      un `AND`. También: host directo IPv6-only (usar solo el pooler), y `postgres`
      no es superuser real en Supabase (limita qué se puede `ALTER`/`DROP` sobre
      roles ya creados). Detalle completo en `docs/spike.md`.

## SPRINT 1 — "Un tenant puede nacer y chatear"

### Fundación
- [x] **S1-1 · Scaffold del monorepo.** `backend/` (FastAPI, SQLAlchemy async, Alembic,
      pytest, ruff; litellm y presidio como dependencias) y `frontend/` (Next.js 16,
      TS strict, Tailwind). `.env` apunta al proyecto Supabase. Ambos arrancan en
      local con un comando cada uno; `/health` verifica conexión a Supabase.
      Configs de deploy para Railway (railway.json o Procfile por servicio).
      Verificado: `pytest` (1 passed), `ruff check` limpio, `alembic upgrade head`
      limpio contra `DATABASE_URL_ADMIN`, `npm run typecheck`/`build` limpios,
      `/health` responde `{"status":"ok","supabase":"connected"}` con conexión real.
      `DATABASE_URL` del backend usa `app_backend` vía pooler (modo transaction).
- [x] **S1-2 · Migración 001.** TODO el esquema de @docs/MODELO_DATOS.md aunque haya
      tablas sin UI todavía. RLS en toda tabla tenant-scoped. Triggers de inmutabilidad
      en ledger_entries y audit_events. Seed: providers + models + exchange_rates demo.
      AC: test que intenta UPDATE/DELETE en ledger y audit_events y comprueba que falla.
      Verificado: `alembic upgrade head` y `downgrade base` limpios contra Supabase (21
      tablas, 16 políticas RLS con la plantilla `CASE WHEN`, 2 triggers de inmutabilidad),
      seed cargado (3 providers, 3 models, 6 exchange_rates), `pytest` en verde incluyendo
      `test_immutability.py` (UPDATE/DELETE fallan en ledger_entries y audit_events, hasta
      conectando como `postgres`), y smoke test de RLS real en `users`. `app_backend` se
      gestiona de forma idempotente (create-if-not-exists, nunca DROP+CREATE).
      **Endurecido tras revisión**: el cascade `tenant_id` + triggers de ledger/audit
      solo bloqueaban el borrado físico de un tenant SI ya tenía filas ahí (comprobado:
      un tenant sin actividad SÍ se borraba). Añadido trigger dedicado `reject_tenant_delete`
      (bloquea DELETE en `tenants` siempre, no toca UPDATE) + `app_backend` sin grant de
      DELETE en `tenants` — tres capas independientes, ver `docs/MODELO_DATOS.md`. Cubierto
      por `test_tenant_hard_delete.py` (incluye el caso límite sin filas hijas).
- [x] **S1-3 · Test cross-tenant en CI.** `tests/test_rls.py`: crea 2 tenants, inserta
      datos en cada uno, verifica que con `app.tenant_id` del tenant A no se lee NADA
      del B en ninguna tabla tenant-scoped (introspección de tablas: si aparece una
      tabla nueva sin política, el test falla en rojo).
      Introspección real vía `information_schema`/`pg_policies` (no una lista fija) +
      2 tenants reales con fila propia en las 16 tablas tenant-scoped, todo por la
      conexión de `app_backend`. Cubre también el caso sin `app.tenant_id` (0 filas,
      sin error). Todo en una transacción revertida al final — no hay que lidiar con
      el borrado bloqueado de tenants ni con basura en ledger/audit inmutables.
- [x] **S1-4 · TenantResolver + sesión RLS.** Middleware: subdominio → tenant (404 si
      no existe, 403 si suspended); `SET LOCAL app.tenant_id` por transacción;
      branding+settings en cache de memoria del proceso con invalidación por versión.
      `app/services/tenant_resolver/`: resuelve por `custom_domain` o `slug.BASE_DOMAIN`,
      cachea tenant+branding en memoria (TTL 60s + invalidación explícita por
      `tenant_id`, no por host/slug — hay que llamarla tras cualquier UPDATE de
      tenants/tenant_branding). Usa `tenant_session()` para leer `tenant_branding`
      (tiene RLS); `/health` no pasa por el middleware. Verificado con 4 tests +
      smoke test end-to-end contra `uvicorn` real (404 / 403 / 200 con host real).
      De paso, corregido un bug real de `pytest-asyncio`: el engine async de
      SQLAlchemy es un singleton de proceso, pero el scope de loop por defecto
      (`function`) le hacía perder el event loop entre tests — fijado a `session`
      en `pyproject.toml`.

### Auth y onboarding
- [x] **S1-5 · Auth con Supabase.** Frontend usa Supabase Auth (email/password +
      Google). Backend: dependencia FastAPI que verifica el JWT de Supabase y resuelve
      users/memberships propios (mapping con auth.users por supabase_user_id).
      Registro de tenant: crea tenant+owner+división default en una transacción.
      audit_event en login. Middleware de roles.
      **Google queda pendiente** (necesita Client ID/Secret en el dashboard de
      Supabase, decisión explícita para no bloquear el ticket — ver TODO en
      `frontend/app/signup/page.tsx`). Migración 002 añade `users.supabase_user_id`
      (mapping con `auth.users`, sin FK real — ver `docs/MODELO_DATOS.md`) y retira
      `users.password_hash` (no custodiamos credenciales). Backend: `app/services/auth/`
      (`get_current_user`, `require_role`, `register_tenant`, `record_login`), 16/16
      tests en verde. Frontend: `/login` y `/signup` con Supabase Auth real
      (`@supabase/ssr`), usando los primeros design tokens CSS del proyecto (nunca
      colores hardcodeados). Verificado con JWT firmados a mano en los tests y,
      contra Supabase real, hasta el punto en que el rate limit de email de la
      cuenta lo permitió (confirma que el signup real activa el flujo de
      confirmación por email, que la página ya contempla).
- [x] **S1-6 · Onboarding wizard (backend).** Endpoints: validate-key (llamada de test
      real al proveedor, persiste cifrada si válida — envelope encryption D2),
      enable-models (camino reseller), dlp-preset (Estricto|Equilibrado|Solo avisar),
      complete. AC: key inválida → status invalid y NO se persiste en claro jamás.
      Cifrado en `app/services/gateway` (Fernet sobre `APP_MASTER_KEY`) — `onboarding`
      solo cifra, nunca descifra (regla 3, CLAUDE.md); `decrypt_provider_key` queda
      reservado para GatewayService (S1-10). Verificado con una llamada de test real
      (`ANTHROPIC_API_KEY` de SP-1) y con una key inválida: en AMBOS casos se cifra
      antes de guardar — confirmado que los bytes en `provider_connections.encrypted_key`
      nunca contienen el texto plano, con round-trip de descifrado correcto. DLP preset
      mapea Estricto→block, Equilibrado→mask, Solo avisar→warn. 23/23 tests en verde.
- [x] **S1-7 · Onboarding wizard (frontend).** 4 pasos en `app/(onboarding)/start/`:
      espacio (nombre, slug, logo, color) → modelos (bifurcación BYOK con check verde
      animado al validar / catálogo reseller con precios en créditos) → preset DLP →
      invitar equipo (emails, puede saltarse). Al terminar llama a
      `/api/onboarding/complete` y redirige a `/chat` (404 esperado — la página es
      S1-12, todavía no existe). Backend: nuevos endpoints de soporte
      `GET /api/onboarding/models-catalog`, `POST /api/onboarding/invite-team`,
      `GET /api/models/enabled`. La sesión de Supabase se guarda en cookie con
      `domain=BASE_DOMAIN` para sobrevivir el salto de host del wizard (sin tenant →
      `<slug>.BASE_DOMAIN`).
      **Verificado con un flujo real de navegador** (Playwright): como el signup real
      contra Supabase chocó con su rate limit de email (ya documentado, ver S2-8), la
      sesión se inyectó como cookie firmando un JWT con `SUPABASE_JWT_SECRET` — mismo
      mecanismo que ya usan los tests de pytest, no un atajo nuevo. Los 4 pasos
      completan de punta a punta (tenant creado → modelo habilitado → preset guardado
      → invitación persistida → `complete` → redirect a `/chat`) sin errores de
      consola ni peticiones fallidas.
      **Encontrado y corregido durante esa verificación** (ninguno estaba cubierto por
      `pytest`/`tsc` porque ambos evitan el navegador real):
      1. `next.config.ts` no declaraba `allowedDevOrigins` — Next 16 bloquea por
         defecto los recursos de dev cross-origin, y la convención de dev del
         proyecto (`<slug>.lvh.me:3000`) es cross-origin por definición. Sin esto la
         app nunca hidrataba en ningún subdominio (React nunca se montaba, cualquier
         formulario hacía un submit nativo GET en vez de llamar al handler).
      2. El backend no tenía `CORSMiddleware` en absoluto — ninguna llamada del
         frontend al backend (puerto distinto) había funcionado nunca desde un
         navegador real; el preflight `OPTIONS` devolvía 405. Añadido con
         `allow_origin_regex` sobre `settings.base_domain` (mismo config ya usado por
         `tenant_resolver`).
      3. `test_onboarding.py::test_enabled_models_endpoint_reflects_enable_models`
         tenía el bug de la regla 7 del CLAUDE.md: la query de setup corría en una
         conexión sin `SET LOCAL app.tenant_id`, así que RLS la dejaba ver siempre 0
         filas de `tenant_model_access` — el test pasaba o fallaba según qué modelo
         hubiera quedado habilitado por un test anterior en el mismo tenant
         (fixture `module`-scoped), no según el comportamiento real del endpoint.
      4. `pattern="[a-z0-9-]+"` en el input de slug rompía en Chrome actual (parsea
         los `pattern` de HTML en modo Unicode-set `v`, donde un `-` sin escapar al
         final de una clase de caracteres es sintaxis inválida) — `SyntaxError` en
         consola en cada carga de `/start`. Corregido a `[a-z0-9\-]+`.
      Objetivo UX: < 5 min de key a primer prompt — pendiente de medir con usuarios
      reales, pero el camino técnico ya no tiene bloqueos.

### Pipeline de chat
- [x] **S1-8 · PolicyService.** Visibilidad de modelo por rol/división (tenant_model_access
      + min_role), saldo (wallet + allocation del período), rate limit con contadores
      en Postgres por ventana (usuario y tenant). Denegación → 403 + audit_event.
      `app/services/policy/check()`: los tres motivos de docs/ARQUITECTURA.md
      (`model_not_enabled` cubre también min_role insuficiente — de cara al usuario
      es el mismo "no ves este modelo"; `no_balance` cubre wallet agotada y, solo en
      modo reseller, presupuesto de división agotado si existe una fila de
      `division_allocations` para el periodo actual — sin fila no bloquea, todavía no
      hay UI de asignación (S2-2); `rate_limited` con ventana fija de 1 minuto vía
      `INSERT ... ON CONFLICT DO UPDATE` atómico, sin `SELECT ... FOR UPDATE`).
      Tabla nueva `rate_limit_counters` (migración `92a01398c4fe`): el backlog pedía
      el mecanismo sin fijar esquema, así que quedó documentada en
      docs/MODELO_DATOS.md igual que el resto. Límites de rate limit (60/min usuario,
      600/min tenant) son un valor provisional — no hay cifra de producto
      documentada, ajustar cuando exista un plan/tarifa real. Endpoint mínimo de
      prueba `POST /api/policy/check`, mismo patrón que `/api/whoami` (S1-4).
      **Gap detectado, fuera de scope de este ticket**: ni `register_tenant` (S1-5)
      ni el wizard (S1-6/S1-7) crean una fila en `wallets` al dar de alta un tenant
      reseller — hoy cualquier tenant nuevo deniega siempre por `no_balance` hasta
      su primer topup. Comportamiento correcto (sin fila = 0 disponible), pero el
      flujo de topup de S2-2 tendrá que crear la fila si no existe, no asumir que ya
      está ahí.
      Verificado: `pytest` 32/32 en verde (6 tests nuevos en `test_policy.py` +
      fixture añadida a `test_rls.py` para la tabla nueva), `ruff check`/`ruff
      format --check` limpios, `alembic upgrade head` y `downgrade base` limpios.
- [x] **S1-9 · DLPService.** Presidio en proceso (engine precargado en lifespan) +
      recognizers custom desde dlp_dictionaries (cache en memoria con versión en DB,
      invalidación al editar). Veredicto
      según dlp_settings de la división (block|mask|warn). Placeholders estilo
      <CLIENTE_1>, <IMPORTE_1>, <PERSONA_1>. dlp_entities_summary SOLO tipos y conteos.
      **Optimización de registro de recognizers (arrastrado de SP-2, `docs/spike.md`):**
      registrar solo entidades relevantes para DLP de prompts de chat — quitar los
      predefinidos sin uso real (Crypto, IP, MAC, licencia médica); no duplicar
      PhoneRecognizer (sustituirlo por uno con `supported_regions=["ES"]`, no añadirlo
      encima del que carga por defecto en región US); NIF/NIE ya cubiertos por
      EsNifRecognizer/EsNieRecognizer predefinidos, sin recognizer custom propio.
      AC: revalidar el benchmark de p95 en las specs reales de Railway (en el spike,
      en un portátil de desarrollo con 2-3x de varianza entre corridas, p95 quedó en
      ~290-300 ms frente al objetivo de 150 ms) antes de dar el rendimiento por bueno;
      si Railway confirma el mismo orden de magnitud, evaluar `es_core_news_sm` o
      solapar el análisis DLP con PolicyService en vez de ejecutarlos en serie.
      `app/services/dlp/`: `init_engine()` construye el `AnalyzerEngine` sobre
      `es_core_news_md` vía `NlpEngineProvider` (la forma por defecto de
      `AnalyzerEngine()` sin `nlp_engine` explícito descarga y usa un modelo inglés
      por sorpresa — detectado al prototipar, no es solo un detalle de config) y
      poda los cuatro recognizers del ticket + sustituye `PhoneRecognizer`, tal como
      pide el AC. **Más allá de esos cuatro** (decisión propia, mismo espíritu de "solo
      lo relevante"): cada llamada a `analyze()` pasa una whitelist explícita de
      `entities=` (PERSON, LOCATION, EMAIL_ADDRESS, PHONE_NUMBER, IBAN_CODE,
      CREDIT_CARD, ES_NIF, ES_NIE) — `AnalyzerEngine` filtra por `entities` ANTES de
      ejecutar cada recognizer, no después, así que esto también deja fuera a
      `UrlRecognizer` y las etiquetas AGE/DATE_TIME/ID/NRP/ORGANIZATION que
      `SpacyRecognizer` añade con este modelo, sin tocar el registry global — menos
      recognizers corriendo por request ayuda directamente al problema de p95 del
      spike. Diccionario del tenant/división: un `PatternRecognizer` ad-hoc por
      categoría presente (`dlp_dictionaries.category` → `DLP_DICT_CLIENT`/`_PROJECT`/
      `_CODE`/`_CUSTOM`), pasado vía `ad_hoc_recognizers` en cada llamada — no se toca
      el registry compartido con datos de un tenant. Placeholders numerados
      (`<PERSONA_1>`, `<PERSONA_2>`...) vía `AnonymizerEngine` con un operador
      `"custom"` por tipo (mismo valor exacto reutiliza el mismo número); los
      solapamientos entre recognizers (visto en el prototipo: `IBAN_CODE` y
      `LOCATION` casando el mismo IBAN) los resuelve la estrategia por defecto de
      `AnonymizerEngine` (`MERGE_SIMILAR_OR_CONTAINED`) — no hay lógica de
      solapamiento a mano. `IMPORTE_1` del ejemplo del ticket no tiene recognizer
      detrás: no hay ninguno de importes/dinero en el spike ni en el alcance
      documentado — placeholder ilustrativo, no un entity type real todavía.
      Caché de `dlp_settings`/`dlp_dictionaries`: mismo patrón TTL (60s) +
      `invalidate_dlp_cache(tenant_id)` que `tenant_resolver` — sin columna de
      versión porque, a diferencia de `tenant_branding.updated_at`, no hay ninguna
      en el esquema que sirva de marca; para S2-1 (CRUD de diccionarios) basta con
      llamar a esa función tras escribir. **Decisión propia sin AC explícito**: sin
      fila de `dlp_settings` para un tenant, el modo por defecto es `block`
      (fail-closed), no el más permisivo — mismo principio que el fail-closed de
      MeteringService (docs/ARQUITECTURA.md); un DLP sin configurar no debe
      equivaler a sin protección. **Fuera de alcance a propósito**: `analyze()` no
      escribe `requests` ni `audit_events` — esas escrituras transaccionales son de
      GatewayService (S1-10). Endpoint mínimo de prueba `POST /api/dlp/analyze`.
      **Pendiente, tal como pide el AC**: el benchmark de p95 en specs reales de
      Railway NO se ha revalidado (no hay despliegue Railway todavía) — sigue el
      número del spike (~290-300 ms en portátil de desarrollo) como advertencia
      abierta para S1-10/despliegue, ninguna mitigación (es_core_news_sm, solapar con
      PolicyService) se aplicó todavía porque el AC pide medir antes de decidir.
      Verificado: `pytest` 39/39 en verde (7 tests nuevos en `test_dlp.py`, incluye
      solapamiento, reutilización de placeholder, modo warn, fail-closed sin config,
      y el endpoint HTTP), `ruff check`/`ruff format --check` limpios, y arranque
      real de `uvicorn` (no solo los tests, que no disparan el lifespan de FastAPI
      con `ASGITransport`) para confirmar que `init_engine()` no rompe el startup.
- [x] **S1-10 · GatewayService + streaming.** POST /api/chat/stream (SSE) con el pipeline
      completo de @docs/ARQUITECTURA.md: policy → DLP (409 masked / 422 blocked) → hold
      de créditos en Postgres (reseller) → stream con litellm.acompletion → transacción
      final (metering con usage real + ledger + requests + audit) → último evento con
      credits_charged y saldo.
      AC: provider_error a mitad de stream no deja hold huérfano ni cobra sin usage.
      Dos fases en `app/services/gateway/` para poder devolver códigos HTTP reales antes
      de comprometer la respuesta a streaming: `prepare_stream()` (función normal, no
      generador) resuelve policy → DLP → hold y lanza excepción si algo falla — el
      endpoint las traduce a 403/409/422 ANTES de crear el `StreamingResponse`; solo
      entonces arranca `stream_chat()` (el generador SSE real). Sin `sse-starlette` en
      el proyecto: formato `data: {json}\n\n` a mano.
      **Regla interno/externo (aclarada en revisión de este ticket, ahora también en
      `docs/ARQUITECTURA.md`)**: con veredicto `masked` y `confirm_masked=true`, hacia
      el proveedor externo SIEMPRE viaja `masked_text` — el original jamás sale de
      nuestra infraestructura. Lo persistido en `messages.content` (RLS + flag
      `log_full_prompts` de la división) es SIEMPRE el original — son dos destinos con
      reglas distintas. `audit_events.subject` nunca lleva texto de prompt.
      Arrancados `app/services/ledger/` y `app/services/metering/` (vacíos hasta ahora):
      lo mínimo para que S1-10 funcione correctamente bajo concurrencia
      (`SELECT ... FOR UPDATE` sobre `wallets` antes de crear/liberar hold o registrar
      consumo, regla 5 de CLAUDE.md — nunca INSERT/UPDATE directo desde otro servicio).
      S1-11 es quien añade el test de carrera dedicado y puede endurecer esta base.
      Decisiones propias documentadas en el código (sin AC explícito en el backlog):
      estimación del hold en dos tramos con la tarifa que corresponde a cada uno
      (`1k_tokens_in` para el prompt, `1k_tokens_out` para `max_tokens` — la de salida
      es la cara); `exchange_rate_id` de `requests`/`ledger_entries` referencia la fila
      `1k_tokens_in` como puntero informativo de "qué versión de tarifa estaba vigente"
      (el cálculo real usa ambas filas, el esquema solo permite un FK único);
      `conversation_id` ausente → se crea la conversación al completar con éxito
      (título = primeros ~60 caracteres del prompt original), nunca en el camino de
      error. D6 (thinking sin límite) resuelto en código, no en esquema: mapeo mínimo
      por proveedor (`reasoning_effort=disable` para Gemini, hallazgo del spike SP-1).
      **Hallazgo de seguridad real, corregido en el camino** (no introducido por este
      ticket, pero expuesto por su test de logging): Presidio, en DEBUG, logueaba el
      texto original del prompt vía su logger interno (`lemma_context_aware_enhancer`,
      contexto de entidades) — nada que ver con el logging propio de la app, pero un
      leak real si alguna vez se sube el logging raíz a DEBUG. Corregido en
      `dlp.init_engine()` (S1-9): loggers `presidio-analyzer`/`presidio-anonymizer`
      fijados a WARNING de forma explícita, independiente de la config de logging raíz.
      Verificado: `pytest` 47/47 en verde (8 tests nuevos en `test_gateway.py`, incluye
      feliz reseller, masked sin confirmar, masked confirmado con captura del texto que
      recibe litellm, blocked, no_balance por hold antes de llamar al proveedor, BYOK
      sin tocar el ledger, provider_error a mitad de stream con hold liberado y sin
      cobro, y el test de logging de la regla 3), `ruff check`/`ruff format --check`
      limpios en todos los archivos tocados, `alembic upgrade head` sin cambios (el
      esquema de S1-2 ya cubría todo lo necesario, ninguna migración nueva), arranque
      real de `uvicorn` con `/health` respondiendo. **Gap preexistente detectado, fuera
      de scope** (no tocado): `backend/app/services/onboarding/__init__.py:301` tiene
      una línea de 101 caracteres que `ruff check` marca en el repo completo — no es de
      este ticket, no se ha modificado ese archivo.
- [x] **S1-11 · Metering + Ledger.** Exchange rate vigente por (model, unit) en el momento
      del request; apunte consumption con credits_delta, provider_cost_eur, balance_after.
      AC: dos consumos concurrentes no dejan balance_cached inconsistente (test de carrera).
      Endurecimiento de lo mínimo que dejó S1-10 (ambos servicios ya existían, funcionales
      pero sin el test de carrera dedicado ni varios gaps anotados en el código/docs).
      **Carrera real** (`tests/test_ledger.py`): N=10 peticiones concurrentes contra el
      mismo wallet con saldo para K=4 (K calculado con `gateway._estimate_hold` real, no
      a ojo). Diseñada en DOS oleadas deliberadas, no una carrera de punta a punta: el
      hold es conservador por diseño (cubre `max_tokens` de salida) y el cobro real es
      bastante menor, así que un flujo que TERMINA libera más margen del que ocupó — un
      intento tardío puede colarse aprovechando ese margen (comportamiento correcto,
      pero no determinista si se deja mezclado con el resto del flujo). La oleada 1
      dispara las N reservas de hold concurrentes contra el MISMO saldo inicial (ahí está
      la contención real, con `SELECT ... FOR UPDATE`), la oleada 2 completa los K
      flujos admitidos sin más contención. Verificado: exactamente K completan, N−K
      deniegan por `no_balance`, nunca se sobregira, `reserved_amount` vuelve a 0,
      `ledger.check_integrity()` no reporta divergencias.
      **`ledger.check_integrity()`** (nueva, reutilizable): compara `wallets.
      balance_cached` contra `SUM(ledger_entries.credits_delta)` por wallet — con
      `tenant_id` (como en el test) o sin argumentos (recorre todos los tenants, listos
      para el cron de S2-8). Nunca bypassrls, ni para esto: recorre `tenants` (`[global]`,
      sin RLS) y abre su propia `tenant_session` por tenant. Alcance explícito: solo
      `balance_cached` vs ledger: `reserved_amount` no tiene un ledger propio con el que
      compararlo, documentado como problema distinto (fuera de este ticket).
      **`balance_after` bajo concurrencia**: investigado y corregido, no solo
      documentado. `ledger_entries.ts` con `DEFAULT now()` captura la hora de INICIO de
      la transacción, no la del INSERT — bajo carga real, el orden de `now()` puede no
      coincidir con el orden real en que las transacciones adquieren el lock de
      `lock_wallet` y confirman (`balance_after` en sí siempre es correcto, calculado ya
      dentro de la sección serializada por el lock; el riesgo es solo al ORDENAR por
      `ts`). Fix de una línea en `ledger.record_consumption()`: `ts = clock_timestamp()`
      explícito en el INSERT en vez del `DEFAULT` de la columna — sin migración.
      Verificado con `ORDER BY ts, id` (id como desempate, UUID v7) tras la carrera:
      cada `balance_after` es exactamente el anterior más su propio `credits_delta`.
      **`current_rates()` fail-closed**: ya fallaba cerrado desde S1-10 (nunca NULL
      silencioso); sustituido el `KeyError` genérico por `MissingExchangeRateError`
      propia, localizable. **Gap real detectado al revisar esto**: si `current_rates()`
      (o cualquier otro paso de `_finalize_success`) fallaba DESPUÉS de que el streaming
      ya completó con éxito, la excepción se propagaba sin control fuera del generador
      de `gateway.stream_chat()` — hold huérfano, sin evento SSE de error limpio (el
      único try/except de `stream_chat()` cubría la llamada a litellm, no la
      finalización). Corregido: `_finalize_success(...)` ahora tiene su propio
      try/except dentro del generador; cualquier fallo ahí se trata igual que un
      `provider_error` real (`_finalize_provider_error()`, que a su vez libera el hold
      en su propio try/except — best-effort, para que un fallo incluso EN la limpieza
      no deje el generador colgado). Cubierto por un test nuevo en `test_gateway.py`
      que mockea `metering.current_rates` para fallar solo en su segunda llamada (la de
      finalización, no la de estimación del hold).
      **Decisión documentada, no un gap abierto**: `docs/ARQUITECTURA.md` (línea junto a
      la regla fail-closed) dejaba abierto si convenía un `status` propio en `requests`
      para distinguir provider_error real de fail-closed-sin-usage. Decisión: NO —
      evita una migración sin necesidad de producto documentada (YAGNI, mismo criterio
      que otras decisiones del proyecto); con el endurecimiento de arriba las tres
      causas ya comparten tratamiento uniforme y se pueden distinguir por
      `audit_events.subject` si algún día hace falta en métricas.
      **Verificado, sin cambio de código**: el hallazgo 2 del spike SP-1 (tokens de
      "thinking" son coste real) — `usage.completion_tokens` (capturado como
      `tokens_out`) YA incluye los tokens de razonamiento en el esquema que litellm
      normaliza (desglose DENTRO de `completion_tokens`, no una cifra aparte); ya se
      factura bien sin tocar nada. Nota dejada en `metering/__init__.py` para que nadie
      "arregle" esto sin evidencia nueva.
      **Hold huérfano por caída de proceso** (gap detectado en revisión del plan, no
      parte del pedido original): `reserved_amount` es un contador agregado sin tabla de
      holds por fila — si el proceso muere con un stream en vuelo, ese hold queda
      sumado para siempre, y el "job de limpieza de holds expirados" que S2-8 prometía
      no se podía construir sobre este diseño (no hay fila que expirar). Resuelto para
      F1 con `ledger.reset_orphaned_holds()`: reconciliación al arranque (lifespan de
      `main.py`, junto a `dlp.init_engine()`) que resetea `reserved_amount` a 0 en todos
      los wallets — correcto porque un proceso recién nacido no puede tener streams
      propios en vuelo. **Asume instancia única** (cierto en F1/Railway sin
      autoscaling) — documentado explícitamente en el código y junto a D4 en
      `docs/ARQUITECTURA.md`; escalar a múltiples instancias exige antes migrar a una
      tabla de holds por fila con TTL. Alcance de S2-8 actualizado para retirar esa
      limpieza (ya no aplica en F1) y anotar que escalar es lo que la reintroduciría.
      **Encontrado y corregido durante la verificación** (no en el plan original):
      arranque real de `uvicorn` con los ~75 tenants ya acumulados en la BD de
      desarrollo tardó ~20-25 s solo en `reset_orphaned_holds()` (un round-trip de red
      al pooler de Supabase por tenant, secuencial). Paralelizado (`asyncio.gather`,
      acotado a 8 concurrentes para no agotar el pool del engine — mismo tratamiento
      aplicado a `check_integrity()` por consistencia) — arranque real bajó a ~14 s.
      Documentado en el docstring como algo a revisar de nuevo si el número de tenants
      llega a los miles.
      Verificado: `pytest` 55/55 en verde (7 tests nuevos en `test_ledger.py` +
      `test_metering.py`, 1 test nuevo en `test_gateway.py`), `ruff check`/`format
      --check` limpios en todo lo tocado, `alembic upgrade head` sin cambios (ninguna
      migración nueva), arranque real de `uvicorn` cronometrado antes y después del
      fix de paralelización, carrera repetida 3 veces seguidas sin flakiness.
- [x] **S1-12 · Chat UI.** /chat con ChatWindow (streaming SSE), MessageBubble con
      `modelo · coste en créditos`, ChatInput, ModelSelector (agrupado por proveedor,
      precio /1K, "Solo admins" deshabilitado), ContextBar (División · Créditos
      usados/asignados · DLP activo, actualizada por el último evento SSE),
      DLPInterstitial (409: texto enmascarado con marks + Enviar enmascarado/Editar)
      y pantalla de bloqueo (422). Todo con design tokens, cero colores hardcodeados.
      **No solo frontend**: `frontend/app/login/page.tsx` tenía un TODO explícito
      ("guardar el perfil resuelto en el estado de la app") y ni "modelos
      habilitados con proveedor+precio+min_role" ni "contexto de división
      (créditos, DLP activo)" tenían un endpoint que los sirviera combinados.
      Tres GET nuevos, de solo lectura, sin migración: `GET /api/me` (identidad
      resuelta sin side-effect de audit_event, a diferencia de `/api/auth/login`),
      `GET /api/chat/models` (`policy.list_visible_models`, JOIN
      `tenant_model_access`+`models`+`providers`+`exchange_rates` acotado al
      tenant, con `allowed` por rol) y `GET /api/chat/context`
      (`policy.get_chat_context`: división, saldo, presupuesto del periodo, modo
      DLP). Refactor mínimo en `policy.py`: `is_role_sufficient(role, min_role)`
      extraída de `check()` — única fuente de verdad del ranking de roles,
      reusada por `list_visible_models`. `dlp.get_mode()` nuevo wrapper público
      de `_get_mode` (S1-9) para leer el modo DLP sin analizar ningún prompt.
      **Regla interno/externo verificada de punta a punta en vivo** (no solo por
      código): con DLP en modo mask, el modelo real respondió citando
      literalmente `<PERSONA_1>`/`<EMAIL_1>` (confirma que lo enviado al
      proveedor fue el texto enmascarado), mientras que `messages.content`
      guardó el prompt original — exactamente la regla fijada en S1-10.
      Componentes nuevos en `frontend/components/` (plano, sin subcarpeta
      `chat/` — sin precedente en el repo): `ChatWindow`, `MessageBubble`,
      `ChatInput`, `ModelSelector`, `ContextBar`, `DLPInterstitial`,
      `DLPBlockedBanner`. `frontend/lib/sse.ts` nuevo: parser SSE a mano sobre
      `response.body` (`EventSource` no sirve con POST+headers), sin dependencia
      npm nueva — simétrico al `data: {json}\n\n` que ya emite `gateway._sse()`.
      Ruta `frontend/app/(app)/chat/` nueva (grupo `(app)` vacío hasta ahora,
      pensado para que S2-x añada admin ahí). **Sin `/chat/[id]`**: no hay
      endpoint de historial ni se pidió — cada carga de `/chat` empieza
      conversación nueva, la continuidad es solo vía el `conversation_id` que
      devuelve el evento `done`, dentro de la misma sesión de navegador.
      `ModelSelector` muestra "requiere rol {min_role real}" en vez del "Solo
      admins" literal del ticket — `min_role` puede ser cualquiera de los
      cuatro roles, no solo admin, y un texto fijo mentiría en los otros casos.
      **Bug real encontrado y corregido en la verificación** (no un artefacto
      del entorno de prueba): `crypto.randomUUID()` exige un contexto seguro
      (HTTPS o `localhost`) — la convención de dev del proyecto,
      `http://<slug>.lvh.me:3000`, NO lo es (aunque resuelva a loopback), así
      que cualquier envío real en dev habría reventado con
      `crypto.randomUUID is not a function`. Sustituido por un generador de IDs
      simple (`Date.now()` + contador) — estos IDs solo sirven para `key` de
      React, no hace falta que sean criptográficamente aleatorios.
      **Verificación en navegador**: Playwright con Chromium limpio (sin perfil
      de usuario ni extensiones) en vez de Claude in Chrome — el navegador
      personal tiene la extensión Phantom Wallet, que marca `lvh.me` como
      phishing (falso positivo con dominios `.me`) y la propia herramienta de
      automatización bloqueaba la interacción con esa página de aviso; se optó
      por no tocar la configuración de una extensión de seguridad del navegador
      personal para pruebas de desarrollo. El signup real de Supabase seguía
      bloqueado por el rate limit de email ya documentado (S2-8) — el
      wizard/signup en sí ya se verificó en S1-7, así que para `/chat`
      específicamente el tenant se aprovisionó directamente
      (`register_tenant()` + JWT firmado con `SUPABASE_JWT_SECRET`, mismo
      mecanismo que ya usan los tests de pytest) e inyectado como cookie de
      sesión de Supabase (formato `sb-<project-ref>-auth-token`,
      `base64-<base64url(JSON)>`, replicado desde `@supabase/ssr`) — sin
      fabricar nada que el propio backend no acepte ya por diseño (`verify_jwt`
      solo comprueba firma/claims, no que Supabase lo haya emitido de verdad).
      De paso, un puerto 3000/3001 ya ocupados por servidores de OTROS
      proyectos ajenos (no tocados) obligó a mover el frontend de verificación
      a un puerto libre. Flujo real completo probado con dos tenants (DLP
      mask y DLP block) contra Anthropic de verdad (`claude-haiku-4-5`,
      prompts cortos, créditos reales descontados y verificados en
      `ContextBar` tras cada mensaje): carga inicial, mensaje limpio
      streaming, interstitial enmascarado + confirmación, restricción de
      modelo por rol, y pantalla de bloqueo — capturas en el historial de la
      sesión.
      Verificado: `npm run typecheck`/`lint`/`build` limpios, `pytest` 55/55 en
      verde, `ruff check`/`format --check` limpios (mismo lint preexistente y
      ajeno en `onboarding/__init__.py:301`, no tocado), `alembic upgrade head`
      sin cambios (sin migración nueva).

**Demo fin S1:** registro → pegar key → check verde → chatear → el DLP enmascara un
cliente → coste en créditos bajo la respuesta.

## SPRINT 2 — "Gobernanza y caja registradora"

- [ ] **S2-1 · Admin DLP.** CRUD de diccionarios (término, categoría, división) +
      política por división (modo + log_full_prompts + retención). UI /admin/dlp.
      Cambios → audit_event settings_change.
- [ ] **S2-2 · Wallet y allocations.** /admin/wallet: saldo, extracto (ledger paginado
      con filtros), asignación mensual por división con barras de consumo,
      reasignación entre divisiones (dos apuntes atómicos). API + UI.
- [ ] **S2-3 · Métricas.** /admin/metrics: créditos del mes, peticiones, usuarios
      activos, incidencias DLP, consumo por división. Queries agregadas sobre requests.
- [ ] **S2-4 · Auditoría por persona.** /admin/audit: selector empleado + filtros
      (rango, modelo, veredicto) + tabla quién/qué/cuándo con filas de incidencia
      destacadas + export CSV (streaming, no en memoria). Visibilidad: division_admin
      su gente, admin todo. Los intentos bloqueados aparecen aunque no salieran.
- [ ] **S2-5 · Branding completo.** /admin/branding: logo, colores, product_name,
      email_from_name; tokens CSS inyectados en SSR; CNAME custom con verificación TXT
      y TLS on-demand (Caddy con endpoint de autorización).
- [ ] **S2-6 · Panel de operador.** Host/rol propios: lista de tenants (+suspender),
      topup manual con nota (audit_event), CRUD de exchange_rates (solo INSERT de
      versión nueva), vista de márgenes (créditos cobrados vs coste real por
      tenant/modelo/mes). 2FA para operadores.
- [ ] **S2-7 · Invitaciones.** Flujo completo: email con token, aceptar (alta en
      Supabase Auth si no existe), cae en su división y rol. Google login ya viene
      de S1-5 vía Supabase.
- [ ] **S2-8 · Hardening + deploy Railway.** Job de integridad del ledger (cron
      llamando a `ledger.check_integrity()`, ya construida en S1-11 — este ticket
      solo la engancha a un cron y decide qué hacer con las divergencias que
      reporte), rate limits afinados, headers de seguridad, test de que ninguna
      key aparece en logs, y despliegue completo en Railway (backend + frontend)
      con dominio wildcard.
      ~~Limpieza de holds expirados~~ — retirado de este ticket: S1-11 resolvió
      el caso de holds huérfanos por caída de proceso con una reconciliación al
      arranque (`ledger.reset_orphaned_holds()`, ver nota junto a D4 en
      `docs/ARQUITECTURA.md`), que asume instancia única (cierto en F1). Si algún
      día se escala a múltiples instancias, ESE es el momento de reintroducir
      un mecanismo de limpieza dedicado — pero sobre una tabla de holds por fila
      con TTL, no sobre el contador agregado actual.
      **Pendiente de revisar aquí**: coste de arranque de `reset_orphaned_holds`
      con muchos tenants — ya paralelizado en S1-11 (`asyncio.gather`, tope de 8)
      tras medir ~20-25 s en secuencial con ~75 tenants de prueba, pero sigue
      siendo O(nº de tenants) en el camino crítico del arranque. Valorar mover a
      una tarea de fondo post-startup (el proceso empieza a servir tráfico antes
      de que termine la reconciliación) o documentar explícitamente como
      excepción de mantenimiento aceptada a la escala de F1 — decisión pendiente,
      no tomada todavía.
      **Reactivar "Confirm email" en Supabase Auth + configurar SMTP propio antes de
      usuarios reales** — se desactivó temporalmente en S1-5 solo para poder probar el
      ciclo signup→login en desarrollo (el proveedor de email por defecto de Supabase
      tiene un rate limit muy bajo, confirmado en vivo: bloqueó varios signups de
      prueba seguidos incluso con "Confirm email" ya desactivado).

**Demo fin S2 (= demo de venta):** onboarding con marca del cliente → empleados
invitados chateando bajo presupuesto de división → admin audita a una persona
concreta → nosotros viendo el margen del mes en el panel de operador.
