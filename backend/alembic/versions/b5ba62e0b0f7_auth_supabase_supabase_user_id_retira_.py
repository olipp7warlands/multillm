"""auth supabase: supabase_user_id, retira password_hash

Revision ID: b5ba62e0b0f7
Revises: fa990bb51646
Create Date: 2026-07-16 21:26:55.503688

- `users.supabase_user_id`: mapea con `auth.users.id` de Supabase Auth.
  Sin FK real hacia `auth.users` a propósito — Supabase gestiona ese
  esquema con sus propias migraciones, y crear una FK cruzada nos
  acoplaría a su implementación interna (ver docs/MODELO_DATOS.md).
- `users.password_hash` se retira: con Supabase Auth como único mecanismo
  de login, nosotros no custodiamos credenciales — si el campo existiera,
  alguien podría rellenarlo por error pensando que hace algo.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5ba62e0b0f7"
down_revision: str | None = "fa990bb51646"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("supabase_user_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_unique_constraint("uq_users_supabase_user_id", "users", ["supabase_user_id"])
    op.drop_column("users", "password_hash")


def downgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text, nullable=True))
    op.drop_constraint("uq_users_supabase_user_id", "users", type_="unique")
    op.drop_column("users", "supabase_user_id")
