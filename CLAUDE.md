# AIhub — módulo de Ownia

AI Gateway white-label multitenant: las empresas dan a sus empleados acceso gobernado
a modelos de IA externos (chat multi-modelo) con su propia marca. Cada petición pasa
DLP → policy → gateway → ledger de créditos → auditoría inmutable.

## Documentación (leer bajo demanda, no está en contexto)
- @docs/ARQUITECTURA.md — pipeline, servicios, garantías de seguridad
- @docs/MODELO_DATOS.md — esquema completo de PostgreSQL (fuente de verdad)
- @docs/BACKLOG.md — tickets con checkboxes; marcar al completar

## Stack (SIN Docker — todo local ligero + cloud)
- DB y Auth: **Supabase** (Postgres cloud con RLS + Supabase Auth). En dev se conecta
  al proyecto cloud directamente; no hay Postgres local.
- Backend: FastAPI + SQLAlchemy 2 async (asyncpg) + Alembic. LiteLLM y Presidio
  van **como librerías Python en el propio proceso**, no como servicios aparte.
- Frontend: Next.js 16 App Router + TypeScript strict + Tailwind
- Deploy: **Railway** (backend y frontend). Sin Redis en Fase 1: holds y rate
  limiting sobre Postgres (row locking); se añade Upstash/Railway Redis si hace falta.

## Comandos
- Backend: `cd backend && uvicorn app.main:app --reload` · tests: `pytest`
- Frontend: `cd frontend && npm run dev` · typecheck: `npm run typecheck`
- Migraciones: `cd backend && alembic upgrade head` / `alembic revision -m "..."`
- Subdominios en local: usar `http://<slug>.lvh.me:3000` (resuelve a 127.0.0.1)

## Convenciones
- UI en español · código, tablas y comentarios en inglés · commits en español
- Backend: servicios en `backend/app/services/`, un módulo por responsabilidad
  (tenant_resolver, policy, dlp, gateway, metering, ledger, audit, branding, onboarding)
- Frontend: componentes usan SOLO design tokens CSS (var(--...)) — nunca colores
  hardcodeados; el theming por tenant depende de ello
- Sin `any` en TypeScript · sin `# type: ignore` sin justificación en comentario

## Reglas innegociables (no relajar nunca, ni en tests)
1. El backend se conecta a Supabase con un rol PROPIO **sin** bypassrls
   (`app_backend`), nunca con el service role para queries de negocio.
   TODA tabla tenant-scoped lleva `tenant_id` + política RLS
   (`current_setting('app.tenant_id')`). Tabla nueva → añadirla a `tests/test_rls.py`.
2. `ledger_entries` y `audit_events` son inmutables por TRIGGER de PostgreSQL
   (no UPDATE/DELETE). No sortear los triggers jamás.
3. Las API keys (BYOK y master) nunca en logs, nunca completas en respuestas
   (solo `key_last4`), descifrado solo dentro de `gateway` service.
4. Todo consumo escribe ledger + requests + audit en UNA transacción.
5. Escrituras de créditos: siempre a través de `LedgerService`, nunca INSERT directo.
6. Con asyncpg + pooler de Supabase: `statement_cache_size=0` y `SET LOCAL
   app.tenant_id` SIEMPRE dentro de la transacción (nunca SET a secas).
   Conexión SIEMPRE vía pooler (el host de conexión directa de Supabase es
   IPv6-only). Tras rotar credenciales de `app_backend`, reintento con
   backoff corto — el pooler no reconoce la password nueva de forma
   instantánea (ver `docs/spike.md`, SP-3).
7. Toda query a tablas tenant-scoped va vía `tenant_session()`; una query
   que devuelve vacío inesperadamente = sospechar SET LOCAL ausente antes
   que datos ausentes (visto dos veces en S1-4, mismo patrón).

## Flujo de trabajo
- Trabajar ticket a ticket desde @docs/BACKLOG.md, en orden; marcar `[x]` al terminar
- Cada ticket termina con sus tests pasando y `alembic upgrade head` limpio
- Si una decisión no está en docs/, preguntar antes de asumir
- Los resúmenes de ticket (lo verificado, gotchas encontrados) se escriben en
  español, igual que los commits — inline en el propio checkbox de
  @docs/BACKLOG.md. No existe ni existirá un directorio `tasks/`: no crear
  `tasks/todo.md` ni `tasks/lessons.md`, las notas van siempre en BACKLOG.md
