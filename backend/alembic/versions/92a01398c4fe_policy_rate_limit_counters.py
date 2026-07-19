"""policy rate limit counters

Revision ID: 92a01398c4fe
Revises: b5ba62e0b0f7
Create Date: 2026-07-19 22:19:20.888312

`rate_limit_counters`: contador de PolicyService (S1-8), no está en
docs/MODELO_DATOS.md porque el backlog pide el mecanismo ("contadores
Postgres por ventana, usuario y tenant") sin especificar el esquema.
Ventana fija de 1 minuto, incremento atómico vía UPSERT (sin necesidad de
SELECT ... FOR UPDATE, ver D4): la fila de la ventana actual se crea con
INSERT ... ON CONFLICT DO UPDATE SET request_count = request_count + 1,
que en Postgres es atómico por sí mismo. `scope_id` es el user_id o el
tenant_id según `scope` — no lleva FK propia porque apunta a dos tablas
distintas según el valor de `scope`.

Sin job de limpieza todavía: las filas de ventanas pasadas se acumulan.
Igual que el TTL+cleanup de holds huérfanos (ver docs/ARQUITECTURA.md),
se puede añadir un job cuando el volumen lo pida — no antes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "92a01398c4fe"
down_revision: str | None = "b5ba62e0b0f7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("scope IN ('user','tenant')", name="ck_rate_limit_counters_scope"),
        sa.PrimaryKeyConstraint(
            "tenant_id", "scope", "scope_id", "window_start", name="pk_rate_limit_counters"
        ),
    )

    # plantilla canónica de docs/MODELO_DATOS.md — CASE WHEN obligatorio
    op.execute("ALTER TABLE rate_limit_counters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rate_limit_counters FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON rate_limit_counters
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

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_counters TO app_backend")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rate_limit_counters CASCADE")
