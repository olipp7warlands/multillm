# AIhub · Modelo de datos (fuente de verdad para migración 001)

Reglas globales:
- Toda tabla tenant-scoped: `tenant_id UUID NOT NULL` + índice + política RLS
  `USING (tenant_id = current_setting('app.tenant_id')::uuid)`.
- Tablas marcadas **[global]** no llevan RLS de tenant (catálogo y operador).
- `ledger_entries` y `audit_events`: trigger que rechaza UPDATE y DELETE.
- PKs: UUID v7 (orden temporal). Timestamps: `timestamptz`, UTC.

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
