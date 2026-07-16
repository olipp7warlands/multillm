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
- [ ] **S1-3 · Test cross-tenant en CI.** `tests/test_rls.py`: crea 2 tenants, inserta
      datos en cada uno, verifica que con `app.tenant_id` del tenant A no se lee NADA
      del B en ninguna tabla tenant-scoped (introspección de tablas: si aparece una
      tabla nueva sin política, el test falla en rojo).
- [ ] **S1-4 · TenantResolver + sesión RLS.** Middleware: subdominio → tenant (404 si
      no existe, 403 si suspended); `SET LOCAL app.tenant_id` por transacción;
      branding+settings en cache de memoria del proceso con invalidación por versión.

### Auth y onboarding
- [ ] **S1-5 · Auth con Supabase.** Frontend usa Supabase Auth (email/password +
      Google). Backend: dependencia FastAPI que verifica el JWT de Supabase y resuelve
      users/memberships propios (mapping con auth.users por supabase_user_id).
      Registro de tenant: crea tenant+owner+división default en una transacción.
      audit_event en login. Middleware de roles.
- [ ] **S1-6 · Onboarding wizard (backend).** Endpoints: validate-key (llamada de test
      real al proveedor, persiste cifrada si válida — envelope encryption D2),
      enable-models (camino reseller), dlp-preset (Estricto|Equilibrado|Solo avisar),
      complete. AC: key inválida → status invalid y NO se persiste en claro jamás.
- [ ] **S1-7 · Onboarding wizard (frontend).** 4 pasos: espacio (nombre, slug, logo,
      color) → modelos (bifurcación BYOK con check verde animado al validar / catálogo
      reseller con precios en créditos) → preset DLP → invitar equipo (emails,
      puede saltarse). Al terminar aterriza en /chat con modelos activos.
      Objetivo UX: < 5 min de key a primer prompt.

### Pipeline de chat
- [ ] **S1-8 · PolicyService.** Visibilidad de modelo por rol/división (tenant_model_access
      + min_role), saldo (wallet + allocation del período), rate limit con contadores
      en Postgres por ventana (usuario y tenant). Denegación → 403 + audit_event.
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

**Demo fin S2 (= demo de venta):** onboarding con marca del cliente → empleados
invitados chateando bajo presupuesto de división → admin audita a una persona
concreta → nosotros viendo el margen del mes en el panel de operador.
