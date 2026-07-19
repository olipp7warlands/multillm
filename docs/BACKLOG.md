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
- [ ] **S1-9 · DLPService.** Presidio en proceso (engine precargado en lifespan) +
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
- [ ] **S1-10 · GatewayService + streaming.** POST /api/chat/stream (SSE) con el pipeline
      completo de @docs/ARQUITECTURA.md: policy → DLP (409 masked / 422 blocked) → hold
      de créditos en Postgres (reseller) → stream con litellm.acompletion → transacción
      final (metering con usage real + ledger + requests + audit) → último evento con
      credits_charged y saldo.
      AC: provider_error a mitad de stream no deja hold huérfano ni cobra sin usage.
- [ ] **S1-11 · Metering + Ledger.** Exchange rate vigente por (model, unit) en el momento
      del request; apunte consumption con credits_delta, provider_cost_eur, balance_after.
      AC: dos consumos concurrentes no dejan balance_cached inconsistente (test de carrera).
- [ ] **S1-12 · Chat UI.** /chat con ChatWindow (streaming SSE), MessageBubble con
      `modelo · coste en créditos`, ChatInput, ModelSelector (agrupado por proveedor,
      precio /1K, "Solo admins" deshabilitado), ContextBar (División · Créditos
      usados/asignados · DLP activo, actualizada por el último evento SSE),
      DLPInterstitial (409: texto enmascarado con marks + Enviar enmascarado/Editar)
      y pantalla de bloqueo (422). Todo con design tokens, cero colores hardcodeados.

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
- [ ] **S2-8 · Hardening + deploy Railway.** Job de integridad del ledger (SUM vs
      balance_cached) y limpieza de holds expirados (cron de Railway o job interno),
      rate limits afinados, headers de seguridad, test de que ninguna key aparece en
      logs, y despliegue completo en Railway (backend + frontend) con dominio wildcard.
      **Reactivar "Confirm email" en Supabase Auth + configurar SMTP propio antes de
      usuarios reales** — se desactivó temporalmente en S1-5 solo para poder probar el
      ciclo signup→login en desarrollo (el proveedor de email por defecto de Supabase
      tiene un rate limit muy bajo, confirmado en vivo: bloqueó varios signups de
      prueba seguidos incluso con "Confirm email" ya desactivado).

**Demo fin S2 (= demo de venta):** onboarding con marca del cliente → empleados
invitados chateando bajo presupuesto de división → admin audita a una persona
concreta → nosotros viendo el margen del mes en el panel de operador.
