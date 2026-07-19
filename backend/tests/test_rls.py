"""S1-3 · Test cross-tenant en CI.

Dos garantías, ambas dirigidas por introspección real del esquema (no una
lista fija en Python) para que una tabla nueva con `tenant_id` y sin
política RLS haga fallar el test en rojo:

1. Toda tabla con columna `tenant_id` tiene RLS activado y al menos una
   política.
2. Con 2 tenants reales y datos reales en TODAS esas tablas: el tenant A no
   lee NADA del tenant B, y sin `app.tenant_id` en la transacción no se lee
   nada en absoluto (sin lanzar error) — la garantía `CASE WHEN` de SP-3.

Todo corre en una única transacción sobre la conexión de `app_backend`
(la del negocio, la misma que usaría el backend real) que se revierte al
final: los datos de prueba nunca se comitean, así que no hay que lidiar con
el borrado de tenants (bloqueado por diseño, ver docs/MODELO_DATOS.md) ni
dejar basura en ledger_entries/audit_events (inmutables).
"""

import os
import uuid
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def _app_backend_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def _tenant_scoped_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT DISTINCT table_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'tenant_id'
        ORDER BY table_name
        """
    )
    return [r["table_name"] for r in rows]


async def test_all_tenant_scoped_tables_have_rls_policy():
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        tables = await _tenant_scoped_tables(conn)
        assert tables, "no se encontró ninguna tabla con columna tenant_id"

        for table in tables:
            has_rls = await conn.fetchval(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
                table,
            )
            assert has_rls, f"{table} tiene tenant_id pero RLS no está activado"

            n_policies = await conn.fetchval(
                "SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = $1",
                table,
            )
            assert n_policies > 0, f"{table} tiene tenant_id pero no tiene ninguna política RLS"
    finally:
        await conn.close()


async def _create_tenant_fixture(
    conn: asyncpg.Connection, slug: str, model_id, provider_id
) -> uuid.UUID:
    """Crea un tenant con una fila en cada tabla tenant-scoped, como
    app_backend (respeta RLS: hay que fijar app.tenant_id antes de insertar,
    porque una política sin WITH CHECK usa el USING también para el INSERT)."""
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug, name, billing_mode) VALUES ($1, $1, 'reseller') RETURNING id",
        slug,
    )
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))

    await conn.execute(
        "INSERT INTO tenant_branding (tenant_id, product_name) VALUES ($1, $2)",
        tenant_id,
        slug,
    )
    user_id = await conn.fetchval(
        "INSERT INTO users (tenant_id, email, name, supabase_user_id) "
        "VALUES ($1, $2, 'User', $3) RETURNING id",
        tenant_id,
        f"{slug}@example.com",
        uuid.uuid4(),
    )
    division_id = await conn.fetchval(
        "INSERT INTO divisions (tenant_id, name) VALUES ($1, 'Default') RETURNING id",
        tenant_id,
    )
    await conn.execute(
        "INSERT INTO memberships (user_id, division_id, tenant_id, role) "
        "VALUES ($1, $2, $3, 'owner')",
        user_id,
        division_id,
        tenant_id,
    )
    await conn.execute(
        """
        INSERT INTO invitations (tenant_id, email, division_id, role, token_hash,
                                 expires_at, created_by)
        VALUES ($1, $2, $3, 'user', 'hash', now() + interval '1 day', $4)
        """,
        tenant_id,
        f"invite-{slug}@example.com",
        division_id,
        user_id,
    )
    await conn.execute(
        "INSERT INTO tenant_model_access (tenant_id, model_id, enabled, source) "
        "VALUES ($1, $2, true, 'reseller')",
        tenant_id,
        model_id,
    )
    await conn.execute(
        """
        INSERT INTO provider_connections (tenant_id, provider_id, encrypted_key,
                                          key_last4, created_by)
        VALUES ($1, $2, $3, 'abcd', $4)
        """,
        tenant_id,
        provider_id,
        b"fake-encrypted",
        user_id,
    )
    wallet_id = await conn.fetchval(
        "INSERT INTO wallets (tenant_id, balance_cached) VALUES ($1, 100) RETURNING id",
        tenant_id,
    )
    await conn.execute(
        "INSERT INTO division_allocations (tenant_id, division_id, period, allocated_credits) "
        "VALUES ($1, $2, '2026-07', 50)",
        tenant_id,
        division_id,
    )
    conversation_id = await conn.fetchval(
        "INSERT INTO conversations (tenant_id, user_id, division_id, title) "
        "VALUES ($1, $2, $3, 'Conv') RETURNING id",
        tenant_id,
        user_id,
        division_id,
    )
    await conn.execute(
        "INSERT INTO messages (tenant_id, conversation_id, role, content_hash) "
        "VALUES ($1, $2, 'user', 'hash')",
        tenant_id,
        conversation_id,
    )
    await conn.execute(
        "INSERT INTO requests (tenant_id, user_id, division_id, ts, status, dlp_verdict) "
        "VALUES ($1, $2, $3, now(), 'completed', 'clean')",
        tenant_id,
        user_id,
        division_id,
    )
    await conn.execute(
        """
        INSERT INTO ledger_entries (tenant_id, wallet_id, type, credits_delta, balance_after)
        VALUES ($1, $2, 'topup', 100, 100)
        """,
        tenant_id,
        wallet_id,
    )
    await conn.execute(
        "INSERT INTO dlp_dictionaries (tenant_id, division_id, term, category, created_by) "
        "VALUES ($1, $2, 'ClienteX', 'client', $3)",
        tenant_id,
        division_id,
        user_id,
    )
    await conn.execute(
        "INSERT INTO dlp_settings (tenant_id, division_id, mode) VALUES ($1, NULL, 'mask')",
        tenant_id,
    )
    await conn.execute(
        "INSERT INTO audit_events (tenant_id, event_type) VALUES ($1, 'login')",
        tenant_id,
    )
    await conn.execute(
        """
        INSERT INTO rate_limit_counters (tenant_id, scope, scope_id, window_start, request_count)
        VALUES ($1, 'user', $2, date_trunc('minute', now()), 1)
        """,
        tenant_id,
        user_id,
    )
    return tenant_id


async def test_cross_tenant_isolation_all_tables():
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    outer = conn.transaction()
    await outer.start()
    try:
        model_id = await conn.fetchval("SELECT id FROM models LIMIT 1")
        provider_id = await conn.fetchval("SELECT id FROM providers LIMIT 1")
        assert model_id and provider_id, "faltan datos de seed (providers/models) para el fixture"

        tables = await _tenant_scoped_tables(conn)

        suffix = uuid.uuid4().hex[:8]
        tenant_a = await _create_tenant_fixture(conn, f"rls-ci-a-{suffix}", model_id, provider_id)
        tenant_b = await _create_tenant_fixture(conn, f"rls-ci-b-{suffix}", model_id, provider_id)

        for table in tables:
            # sin contexto de tenant (simula el reset del pooler entre transacciones,
            # ver docs/spike.md SP-3 Hallazgo 4) -> 0 filas, SIN error
            await conn.execute("SELECT set_config('app.tenant_id', '', true)")
            rows_none = await conn.fetch(f"SELECT tenant_id FROM {table}")
            assert rows_none == [], f"{table}: sin contexto de tenant debería devolver 0 filas"

            # tenant A ve su propia fila y NADA de B
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_a))
            rows_a = await conn.fetch(f"SELECT tenant_id FROM {table}")
            assert len(rows_a) >= 1, f"{table}: tenant A no ve ni su propia fila"
            assert all(r["tenant_id"] == tenant_a for r in rows_a), (
                f"{table}: tenant A ve filas de otro tenant"
            )

            # tenant B ve su propia fila y NADA de A
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_b))
            rows_b = await conn.fetch(f"SELECT tenant_id FROM {table}")
            assert len(rows_b) >= 1, f"{table}: tenant B no ve ni su propia fila"
            assert all(r["tenant_id"] == tenant_b for r in rows_b), (
                f"{table}: tenant B ve filas de otro tenant"
            )
    finally:
        await outer.rollback()
        await conn.close()
