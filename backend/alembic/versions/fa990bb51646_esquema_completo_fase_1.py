"""esquema completo fase 1

Revision ID: fa990bb51646
Revises:
Create Date: 2026-07-16 18:40:22.148730

Fuente de verdad: docs/MODELO_DATOS.md. Notas de implementación:
- Enums de la doc (p.ej. tenants.status) se implementan como TEXT + CHECK,
  no como tipos ENUM nativos de Postgres (más simples de evolucionar sin
  ALTER TYPE). `audit_events.event_type` NO lleva CHECK porque la doc lo
  marca como lista abierta ("...").
- `memberships` y `tenant_model_access` no llevan `id` propio — la doc los
  lista sin él y su UNIQUE es en la práctica su clave natural, así que esa
  combinación es la PRIMARY KEY.
- `dlp_settings` SÍ lleva un `id` propio pese a que la doc no lo lista:
  `division_id` es nullable (NULL = default del tenant) y una PRIMARY KEY
  no admite NULL en ninguna columna, así que (tenant_id, division_id) no
  puede ser la PK ahí. La unicidad real ("como mucho un default por tenant,
  como mucho una fila por división") se aplica con DOS índices únicos
  parciales, no con un UNIQUE normal — un UNIQUE(tenant_id, division_id)
  normal no evita varias filas con division_id NULL para el mismo tenant,
  porque en Postgres NULL nunca es igual a NULL.
- Reglas de ON DELETE (no vienen especificadas en la doc, criterio propio):
  `tenant_id` → CASCADE (tenant = raíz del agregado). FKs de composición
  real (la fila no tiene sentido sin el padre: messages→conversations,
  division_allocations→divisions) → CASCADE. El resto de FK NOT NULL
  (referencias a usuarios/divisiones/modelos desde otra tabla) → RESTRICT,
  para no perder datos por una cascada implícita. FKs NULLable → SET NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa990bb51646"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


TENANT_SCOPED_TABLES = [
    "tenant_branding",
    "users",
    "divisions",
    "memberships",
    "invitations",
    "tenant_model_access",
    "provider_connections",
    "wallets",
    "ledger_entries",
    "division_allocations",
    "conversations",
    "messages",
    "requests",
    "dlp_dictionaries",
    "dlp_settings",
    "audit_events",
]

IMMUTABLE_TABLES = ["ledger_entries", "audit_events"]


def _apply_tenant_rls(table_name: str) -> None:
    """Plantilla canónica de docs/MODELO_DATOS.md — CASE WHEN obligatorio,
    la forma ingenua con AND queda prohibida (ver SP-3, docs/spike.md)."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table_name}
        USING (
            tenant_id = (
                CASE
                    WHEN current_setting('app.tenant_id', true) IS NULL
                         OR current_setting('app.tenant_id', true) = ''
                    THEN NULL
                    ELSE current_setting('app.tenant_id', true)::uuid
                END
            )
        )
    """)


def upgrade() -> None:
    # --- función de generación de PKs UUID v7 (orden temporal), regla global ---
    op.execute("""
        CREATE OR REPLACE FUNCTION uuid_generate_v7()
        RETURNS uuid
        LANGUAGE plpgsql
        VOLATILE
        AS $$
        DECLARE
            unix_ts_ms bytea;
            uuid_bytes bytea;
        BEGIN
            unix_ts_ms = substring(
                int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                FROM 3
            );
            uuid_bytes = uuid_send(gen_random_uuid());
            uuid_bytes = overlay(uuid_bytes placing unix_ts_ms from 1 for 6);
            uuid_bytes = set_byte(
                uuid_bytes, 6, (b'0111' || get_byte(uuid_bytes, 6)::bit(4))::bit(8)::int
            );
            uuid_bytes = set_byte(
                uuid_bytes, 8, (b'10' || get_byte(uuid_bytes, 8)::bit(6))::bit(8)::int
            );
            RETURN encode(uuid_bytes, 'hex')::uuid;
        END
        $$;
    """)

    # --- rol app_backend: gestión idempotente, nunca DROP+CREATE (ver SP-3) ---
    op.execute("""
        DO $$
        DECLARE
            generated_password text := gen_random_uuid()::text || gen_random_uuid()::text;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_backend') THEN
                EXECUTE format(
                    'CREATE ROLE app_backend LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS',
                    generated_password
                );
                RAISE NOTICE 'app_backend creado con password aleatoria — rotarla con'
                    ' ALTER ROLE y guardarla en .env antes de usarla (ver'
                    ' docs/MODELO_DATOS.md)';
            END IF;
        END
        $$;
    """)

    # --- función de inmutabilidad (ledger_entries, audit_events) ---
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_update_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% es inmutable: % no permitido', TG_TABLE_NAME, TG_OP;
        END
        $$;
    """)

    # --- tenants NUNCA se borran físicamente (se suspenden por status) ---
    # Con tenant_id → CASCADE + los triggers de ledger_entries/audit_events,
    # un DELETE FROM tenants con actividad ya falla como efecto colateral del
    # cascade — pero SOLO si ese tenant ya tiene alguna fila en esas dos
    # tablas. Comprobado en vivo: un tenant recién creado sin ninguna fila en
    # ledger_entries/audit_events SÍ se borra físicamente sin problema, el
    # cascade no encuentra nada que lo bloquee. Esa protección incidental no
    # es una garantía real, así que se añade un trigger explícito — solo
    # bloquea DELETE (a diferencia de reject_update_delete, tenants SÍ se
    # actualiza: status, billing_mode, etc.).
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_tenant_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'tenants no se borran físicamente — usar status=''suspended''';
        END
        $$;
    """)

    # ================= catálogo y operador [global] =================
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="trial"),
        sa.Column("billing_mode", sa.Text, nullable=False),
        sa.Column("custom_domain", sa.Text, nullable=True, unique=True),
        sa.Column("custom_domain_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("status IN ('active','suspended','trial')", name="ck_tenants_status"),
        sa.CheckConstraint(
            "billing_mode IN ('reseller','byok','mixed')", name="ck_tenants_billing_mode"
        ),
    )

    op.create_table(
        "providers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("api_base", sa.Text, nullable=True),
    )

    op.create_table(
        "models",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("providers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("modality", sa.Text, nullable=False, server_default="text"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("litellm_model_name", sa.Text, nullable=False),
        sa.CheckConstraint(
            "modality IN ('text','image','audio','video')", name="ck_models_modality"
        ),
        # no viene en la doc explícitamente, pero un slug duplicado por proveedor
        # sería un bug de catálogo, no un caso de uso real:
        sa.UniqueConstraint("provider_id", "slug", name="uq_models_provider_slug"),
    )

    op.create_table(
        "exchange_rates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("provider_cost_eur", sa.Numeric, nullable=False),
        sa.Column("credit_price", sa.Numeric, nullable=False),
        sa.Column(
            "valid_from",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "unit IN ('1k_tokens_in','1k_tokens_out','image','second_audio')",
            name="ck_exchange_rates_unit",
        ),
    )
    op.create_index("ix_exchange_rates_model_id", "exchange_rates", ["model_id"])

    # ================= identidad y tenancy =================
    op.create_table(
        "tenant_branding",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("product_name", sa.Text, nullable=True),
        sa.Column("logo_url", sa.Text, nullable=True),
        sa.Column("favicon_url", sa.Text, nullable=True),
        sa.Column("color_primary", sa.Text, nullable=True),
        sa.Column("color_accent", sa.Text, nullable=True),
        sa.Column("email_from_name", sa.Text, nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("oauth_provider", sa.Text, nullable=True),
        sa.Column("oauth_subject", sa.Text, nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "divisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_divisions_tenant_id", "divisions", ["tenant_id"])

    op.create_table(
        "memberships",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.CheckConstraint(
            "role IN ('owner','admin','division_admin','user')", name="ck_memberships_role"
        ),
        sa.PrimaryKeyConstraint("user_id", "division_id", name="pk_memberships"),
    )
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"])

    op.create_table(
        "invitations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner','admin','division_admin','user')", name="ck_invitations_role"
        ),
    )
    op.create_index("ix_invitations_tenant_id", "invitations", ["tenant_id"])

    # ================= catálogo por tenant =================
    op.create_table(
        "tenant_model_access",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("min_role", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.CheckConstraint("source IN ('reseller','byok')", name="ck_tenant_model_access_source"),
        sa.PrimaryKeyConstraint("tenant_id", "model_id", name="pk_tenant_model_access"),
    )

    op.create_table(
        "provider_connections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("providers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("encrypted_key", sa.LargeBinary, nullable=False),
        sa.Column("key_last4", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','valid','invalid')", name="ck_provider_connections_status"
        ),
    )
    op.create_index("ix_provider_connections_tenant_id", "provider_connections", ["tenant_id"])

    # ================= economía de créditos =================
    op.create_table(
        "wallets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("balance_cached", sa.Numeric, nullable=False, server_default="0"),
        sa.Column("reserved_amount", sa.Numeric, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ledger_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "ts", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("credits_delta", sa.Numeric, nullable=False),
        sa.Column("provider_cost_eur", sa.Numeric, nullable=True),
        sa.Column(
            "exchange_rate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("exchange_rates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "request_id", postgresql.UUID(as_uuid=True), nullable=True
        ),  # FK a requests se añade tras crear requests
        sa.Column(
            "operator_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("balance_after", sa.Numeric, nullable=False),
        sa.CheckConstraint(
            "type IN ('topup','consumption','adjustment','discount_conversion')",
            name="ck_ledger_entries_type",
        ),
    )
    op.create_index("ix_ledger_entries_tenant_id", "ledger_entries", ["tenant_id"])

    op.create_table(
        "division_allocations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.Text, nullable=False),
        sa.Column("allocated_credits", sa.Numeric, nullable=False),
        sa.Column("consumed_credits_cached", sa.Numeric, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "tenant_id", "division_id", "period", name="uq_division_allocations_period"
        ),
    )
    op.create_index("ix_division_allocations_tenant_id", "division_allocations", ["tenant_id"])

    # ================= conversaciones y pipeline =================
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("archived", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])

    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_messages_role"),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])

    op.create_table(
        "requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "ts", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("dlp_verdict", sa.Text, nullable=False),
        sa.Column("dlp_entities_summary", postgresql.JSONB, nullable=True),
        sa.Column("tokens_in", sa.Integer, nullable=True),
        sa.Column("tokens_out", sa.Integer, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("credits_charged", sa.Numeric, nullable=True),
        sa.Column("provider_cost_eur", sa.Numeric, nullable=True),
        sa.Column(
            "exchange_rate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("exchange_rates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('completed','blocked_dlp','blocked_policy','provider_error')",
            name="ck_requests_status",
        ),
        sa.CheckConstraint(
            "dlp_verdict IN ('clean','masked','blocked')", name="ck_requests_dlp_verdict"
        ),
    )
    op.create_index("ix_requests_tenant_id", "requests", ["tenant_id"])

    # ledger_entries.request_id -> requests(id), añadida ahora que requests existe
    op.create_foreign_key(
        "fk_ledger_entries_request_id",
        "ledger_entries",
        "requests",
        ["request_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "dlp_dictionaries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("term", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "category IN ('client','project','code','custom')", name="ck_dlp_dictionaries_category"
        ),
    )
    op.create_index("ix_dlp_dictionaries_tenant_id", "dlp_dictionaries", ["tenant_id"])

    op.create_table(
        "dlp_settings",
        # id propio: ver nota de cabecera (division_id nullable no puede ser parte de una PK)
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("log_full_prompts", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("mode IN ('block','mask','warn')", name="ck_dlp_settings_mode"),
    )
    op.create_index("ix_dlp_settings_tenant_id", "dlp_settings", ["tenant_id"])
    # unicidad real de UNIQUE (tenant_id, division_id) de la doc, con NULL = default
    # del tenant: un UNIQUE normal no basta (NULL != NULL en Postgres), así que se
    # aplican dos índices únicos parciales.
    op.execute(
        "CREATE UNIQUE INDEX uq_dlp_settings_tenant_default "
        "ON dlp_settings (tenant_id) WHERE division_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_dlp_settings_tenant_division "
        "ON dlp_settings (tenant_id, division_id) WHERE division_id IS NOT NULL"
    )

    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ts", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.Text, nullable=True),
        # sin CHECK: la doc lo marca como lista abierta ("...")
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("subject", postgresql.JSONB, nullable=True),
        sa.Column("ip", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])

    # ================= RLS: plantilla canónica en todas las tenant-scoped =================
    for table_name in TENANT_SCOPED_TABLES:
        _apply_tenant_rls(table_name)

    # ================= inmutabilidad: ledger_entries y audit_events =================
    for table_name in IMMUTABLE_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION reject_update_delete();
        """)

    # tenants: solo bloquea DELETE, no UPDATE (ver nota más arriba)
    op.execute("""
        CREATE TRIGGER tenants_no_delete
        BEFORE DELETE ON tenants
        FOR EACH ROW EXECUTE FUNCTION reject_tenant_delete();
    """)

    # ================= permisos de app_backend =================
    op.execute("GRANT USAGE ON SCHEMA public TO app_backend")
    # tenants es de negocio (alta en onboarding, status en suspensión) → INSERT/UPDATE,
    # nunca DELETE (ni falta que hace: el trigger de arriba lo bloquea igualmente).
    # providers/models/exchange_rates son catálogo de operador → solo lectura.
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenants TO app_backend")
    op.execute("GRANT SELECT ON providers, models, exchange_rates TO app_backend")
    for table_name in TENANT_SCOPED_TABLES:
        if table_name in IMMUTABLE_TABLES:
            op.execute(f"GRANT SELECT, INSERT ON {table_name} TO app_backend")
        else:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table_name} TO app_backend")

    # ================= seed: providers + models + exchange_rates demo =================
    op.execute("""
        INSERT INTO providers (slug, name, api_base) VALUES
            ('openai', 'OpenAI', 'https://api.openai.com/v1'),
            ('anthropic', 'Anthropic', 'https://api.anthropic.com'),
            ('google', 'Google', 'https://generativelanguage.googleapis.com')
    """)
    op.execute("""
        INSERT INTO models (provider_id, slug, display_name, modality, status, litellm_model_name)
        SELECT id, 'gpt-4o-mini', 'GPT-4o mini', 'text', 'active', 'gpt-4o-mini'
        FROM providers WHERE slug = 'openai'
    """)
    op.execute("""
        INSERT INTO models (provider_id, slug, display_name, modality, status, litellm_model_name)
        SELECT id, 'claude-haiku-4-5', 'Claude Haiku 4.5', 'text', 'active',
               'anthropic/claude-haiku-4-5-20251001'
        FROM providers WHERE slug = 'anthropic'
    """)
    op.execute("""
        INSERT INTO models (provider_id, slug, display_name, modality, status, litellm_model_name)
        SELECT id, 'gemini-flash', 'Gemini Flash', 'text', 'active', 'gemini/gemini-flash-latest'
        FROM providers WHERE slug = 'google'
    """)
    op.execute("""
        INSERT INTO exchange_rates (model_id, unit, provider_cost_eur, credit_price)
        SELECT id, '1k_tokens_in', 0.00014, 0.03 FROM models WHERE slug = 'gpt-4o-mini'
        UNION ALL
        SELECT id, '1k_tokens_out', 0.00056, 0.08 FROM models WHERE slug = 'gpt-4o-mini'
        UNION ALL
        SELECT id, '1k_tokens_in', 0.0008, 0.12 FROM models WHERE slug = 'claude-haiku-4-5'
        UNION ALL
        SELECT id, '1k_tokens_out', 0.004, 0.5 FROM models WHERE slug = 'claude-haiku-4-5'
        UNION ALL
        SELECT id, '1k_tokens_in', 0.0001, 0.02 FROM models WHERE slug = 'gemini-flash'
        UNION ALL
        SELECT id, '1k_tokens_out', 0.0004, 0.06 FROM models WHERE slug = 'gemini-flash'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_events CASCADE")
    op.execute("DROP TABLE IF EXISTS dlp_settings CASCADE")
    op.execute("DROP TABLE IF EXISTS dlp_dictionaries CASCADE")
    op.execute("DROP TABLE IF EXISTS requests CASCADE")
    op.execute("DROP TABLE IF EXISTS messages CASCADE")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE")
    op.execute("DROP TABLE IF EXISTS division_allocations CASCADE")
    op.execute("DROP TABLE IF EXISTS ledger_entries CASCADE")
    op.execute("DROP TABLE IF EXISTS wallets CASCADE")
    op.execute("DROP TABLE IF EXISTS provider_connections CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant_model_access CASCADE")
    op.execute("DROP TABLE IF EXISTS invitations CASCADE")
    op.execute("DROP TABLE IF EXISTS memberships CASCADE")
    op.execute("DROP TABLE IF EXISTS divisions CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant_branding CASCADE")
    op.execute("DROP TABLE IF EXISTS exchange_rates CASCADE")
    op.execute("DROP TABLE IF EXISTS models CASCADE")
    op.execute("DROP TABLE IF EXISTS providers CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
    op.execute("DROP FUNCTION IF EXISTS reject_tenant_delete()")
    op.execute("DROP FUNCTION IF EXISTS reject_update_delete()")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7()")
    # el rol app_backend NO se elimina en el downgrade: es infraestructura
    # compartida (ver docs/MODELO_DATOS.md), no un artefacto de esta migración.
