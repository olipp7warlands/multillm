# AIhub · Modelo de datos (fuente de verdad para migración 001)

Reglas globales:
- Toda tabla tenant-scoped: `tenant_id UUID NOT NULL` + índice + política RLS
  con la plantilla canónica `CASE WHEN` (ver sección abajo) — **la forma
  ingenua `USING (tenant_id = current_setting('app.tenant_id')::uuid)` queda
  PROHIBIDA**, no es solo un ejemplo simplificado.
- Tablas marcadas **[global]** no llevan RLS de tenant (catálogo y operador).
- `ledger_entries` y `audit_events`: trigger que rechaza UPDATE y DELETE.
- PKs: UUID v7 (orden temporal). Timestamps: `timestamptz`, UTC.

## Plantilla canónica de política RLS (obligatoria, verificada en SP-3)

Entre transacciones, el pooler de Supabase deja el GUC custom `app.tenant_id`
en `''` (cadena vacía) en vez de `NULL` — y Postgres NO garantiza el orden de
evaluación de un `AND`, así que un guard tipo `AND current_setting(...) <> ''`
antes del cast **no protege** del cast inseguro. La única forma que garantiza
el orden es un `CASE WHEN`. Toda tabla tenant-scoped usa esta plantilla,
sustituyendo `<tabla>`:

```sql
ALTER TABLE <tabla> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <tabla> FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON <tabla>
USING (
    tenant_id = (
        CASE
            WHEN current_setting('app.tenant_id', true) IS NULL
                 OR current_setting('app.tenant_id', true) = ''
            THEN NULL
            ELSE current_setting('app.tenant_id', true)::uuid
        END
    )
);
```

Sin `SET LOCAL app.tenant_id` dentro de la transacción → 0 filas, sin error
(nunca `InvalidTextRepresentationError`). Detalle y repro en `docs/spike.md`
(SP-3, Hallazgo 4).

## Gestión del rol `app_backend` (limitación de Supabase, ver SP-3)

El `postgres` de Supabase **no es superuser real** (`rolsuper=false`, solo
`rolbypassrls=true` + `CREATEROLE`). Con Postgres 16+ esto significa que
`postgres` puede `CREATE ROLE app_backend ... NOSUPERUSER NOBYPASSRLS` una
única vez, pero **no puede** volver a tocar esos atributos en un `ALTER ROLE`
posterior (ni siquiera como no-op) ni hacer `DROP ROLE`/`DROP OWNED BY` sobre
un rol que no posee como miembro, aunque lo haya creado él mismo.
Consecuencia para la migración 001 y cualquier rotación posterior:
- Crear `app_backend` con `CREATE ROLE ... IF NOT EXISTS`-equivalente
  (comprobar existencia antes con `SELECT 1 FROM pg_roles`, no un `DROP`+
  `CREATE`) fijando `NOSUPERUSER NOBYPASSRLS` en ese único `CREATE`.
- Rotaciones de password: **solo** `ALTER ROLE app_backend WITH PASSWORD
  '...'` (sin repetir `NOSUPERUSER`/`NOBYPASSRLS` en el mismo `ALTER` —
  Postgres lo rechaza viniendo de un `postgres` no-superuser).

## Los tenants NUNCA se borran físicamente

`tenants.status` incluye `suspended` precisamente para esto: dar de baja un
tenant es un `UPDATE status='suspended'`, nunca un `DELETE`. Esto está
reforzado en tres capas independientes (defensa en profundidad, no una
sola comprobación):

1. `app_backend` no tiene privilegio `DELETE` sobre `tenants` (el `GRANT`
   solo da `SELECT, INSERT, UPDATE`) — el backend no puede ni intentarlo.
2. Un trigger `BEFORE DELETE` dedicado (`reject_tenant_delete`) rechaza
   cualquier `DELETE FROM tenants` incondicionalmente, venga de quien venga.
3. Aun sin las dos anteriores, `tenant_id` en cascada (`ON DELETE CASCADE`)
   más los triggers de inmutabilidad de `ledger_entries`/`audit_events`
   also bloquean el borrado **en cuanto el tenant tiene alguna fila en esas
   dos tablas** — el intento de cascada choca con el trigger de
   inmutabilidad y aborta toda la transacción.

**Por qué existen las tres capas y no basta con la (3):** se comprobó en
vivo que un tenant recién creado, sin ninguna fila todavía en
`ledger_entries` ni `audit_events`, **sí se borra físicamente sin
problema** por esa vía — el cascade no encuentra nada que lo bloquee. La
protección "incidental" de la capa 3 no es una garantía real por sí sola;
la capa 2 (trigger dedicado en `tenants`) es la que hace la regla
universal, independientemente de la actividad que tenga el tenant.

## Identidad y tenancy
```sql
tenants [global]
  id, slug UNIQUE,              -- subdominio
  name, status,                 -- active|suspended|trial
  billing_mode,                 -- reseller|byok|mixed
  custom_domain UNIQUE NULL, custom_domain_verified_at NULL,
  created_at

tenant_branding
  tenant_id PK→tenants, product_name, logo_url, favicon_url,
  color_primary, color_accent, email_from_name, updated_at

users
  id, tenant_id, email,         -- UNIQUE (tenant_id, email)
  password_hash NULL, oauth_provider NULL, oauth_subject NULL,
  name, status, last_login_at, created_at

divisions
  id, tenant_id, name, is_default bool, created_at

memberships
  user_id, division_id, tenant_id,
  role                          -- owner|admin|division_admin|user
  UNIQUE (user_id, division_id)

invitations
  id, tenant_id, email, division_id, role, token_hash, expires_at,
  accepted_at NULL, created_by
```

## Catálogo y proveedores
```sql
providers [global]  id, slug, name, api_base
models    [global]  id, provider_id, slug, display_name,
                    modality,           -- text (image/audio/video en F2-F3)
                    status, litellm_model_name

exchange_rates [global]  -- VERSIONADA: nunca UPDATE, siempre INSERT nueva fila
  id, model_id,
  unit,                       -- 1k_tokens_in|1k_tokens_out|image|second_audio
  provider_cost_eur numeric,  -- nuestro coste real
  credit_price numeric,       -- precio en créditos al tenant (margen = diferencia)
  valid_from, valid_to NULL   -- NULL = vigente

tenant_model_access
  tenant_id, model_id, enabled bool,
  min_role NULL,              -- NULL = todos los roles
  source                      -- reseller|byok
  UNIQUE (tenant_id, model_id)

provider_connections          -- solo BYOK
  id, tenant_id, provider_id,
  encrypted_key bytea,        -- envelope encryption (D2)
  key_last4, status,          -- pending|valid|invalid
  validated_at, created_by
```

## Economía de créditos
```sql
wallets
  id, tenant_id UNIQUE, balance_cached numeric,
  reserved_amount numeric DEFAULT 0,   -- reserva contable
  updated_at
  -- verdad = SUM(ledger_entries.credits_delta); job nocturno verifica

ledger_entries   -- INMUTABLE (trigger anti UPDATE/DELETE)
  id, tenant_id, wallet_id, ts,
  type,                       -- topup|consumption|adjustment|discount_conversion
  credits_delta numeric,      -- + o −
  provider_cost_eur NULL,     -- solo consumption → margen visible
  exchange_rate_id NULL FK,   -- tarifa con la que se cobró
  request_id NULL FK,
  operator_user_id NULL, note NULL,   -- solo topup/adjustment manuales
  balance_after numeric       -- verificación de integridad

division_allocations
  id, tenant_id, division_id, period,          -- 'YYYY-MM'
  allocated_credits numeric, consumed_credits_cached numeric
  UNIQUE (tenant_id, division_id, period)
```

## Conversaciones y pipeline
```sql
conversations
  id, tenant_id, user_id, division_id, title, created_at, archived bool

messages
  id, tenant_id, conversation_id, role,        -- user|assistant
  content text NULL,          -- NULL si la división eligió hash-only (D1)
  content_hash NOT NULL, model_id NULL, created_at

requests    -- una fila por petición, llegue o no al proveedor
  id, tenant_id, user_id, division_id, model_id NULL, conversation_id NULL,
  ts, status,                 -- completed|blocked_dlp|blocked_policy|provider_error
  dlp_verdict,                -- clean|masked|blocked
  dlp_entities_summary jsonb, -- tipos y conteos, nunca los valores
  tokens_in int, tokens_out int, latency_ms int,
  credits_charged numeric, provider_cost_eur numeric, exchange_rate_id FK NULL

dlp_dictionaries
  id, tenant_id, division_id NULL,   -- NULL = todo el tenant
  term, category,             -- client|project|code|custom
  created_by, created_at

dlp_settings
  tenant_id, division_id NULL,       -- NULL = default del tenant
  mode,                       -- block|mask|warn
  log_full_prompts bool DEFAULT true -- D1
  UNIQUE (tenant_id, division_id)
```

## Auditoría
```sql
audit_events    -- INMUTABLE (trigger anti UPDATE/DELETE)
  id, tenant_id, ts, actor_user_id NULL, actor_role NULL,
  event_type,   -- request|dlp_block|dlp_mask|login|settings_change|topup|
                -- model_enabled|member_invited|branding_change|...
  subject jsonb, ip inet NULL, user_agent NULL
```

## Visibilidad de auditoría (aplicar en queries, no solo en UI)
- `division_admin`: solo usuarios de sus divisiones
- `admin`/`owner` del tenant: todo su tenant
- operador: cross-tenant, solo desde el panel de operador (rol de DB distinto)
