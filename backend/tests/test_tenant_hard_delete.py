import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def _admin_dsn() -> str:
    return os.environ["DATABASE_URL_ADMIN"].replace("postgresql+asyncpg://", "postgresql://")


async def test_tenant_delete_fails_even_without_any_child_rows():
    """Decisión de diseño (docs/MODELO_DATOS.md): los tenants no se borran
    físicamente nunca, solo se suspenden (status='suspended'). Un tenant
    recién creado SIN ninguna fila en ledger_entries/audit_events no
    dispararía el choque cascade-vs-inmutabilidad (comprobado: sin el
    trigger dedicado, ese caso se borra sin más) — por eso el bloqueo real
    es `reject_tenant_delete`, no una consecuencia lateral del resto de
    triggers. Este test prueba justo ese caso límite."""
    conn = await asyncpg.connect(_admin_dsn(), statement_cache_size=0, ssl="require")
    outer = conn.transaction()
    await outer.start()
    try:
        tenant_id = await conn.fetchval(
            "INSERT INTO tenants (slug, name, billing_mode) "
            "VALUES ($1, 'Test', 'reseller') RETURNING id",
            f"test-hard-delete-{uuid.uuid4().hex[:8]}",
        )
        # sin wallet, sin ledger_entries, sin audit_events: cero filas hijas
        sp = conn.transaction()
        await sp.start()
        with pytest.raises(asyncpg.PostgresError, match="no se borran físicamente"):
            await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await sp.rollback()

        # el tenant sigue existiendo (el DELETE no coló nada parcial)
        still_there = await conn.fetchval("SELECT count(*) FROM tenants WHERE id = $1", tenant_id)
        assert still_there == 1
    finally:
        await outer.rollback()
        await conn.close()


async def test_tenant_delete_fails_with_ledger_activity_too():
    """Con actividad en ledger_entries, el DELETE también falla — por el
    trigger dedicado (que dispara primero), reforzado por el choque de
    cascada con la inmutabilidad de ledger_entries como segunda capa."""
    conn = await asyncpg.connect(_admin_dsn(), statement_cache_size=0, ssl="require")
    outer = conn.transaction()
    await outer.start()
    try:
        tenant_id = await conn.fetchval(
            "INSERT INTO tenants (slug, name, billing_mode) "
            "VALUES ($1, 'Test', 'reseller') RETURNING id",
            f"test-hard-delete-activity-{uuid.uuid4().hex[:8]}",
        )
        wallet_id = await conn.fetchval(
            "INSERT INTO wallets (tenant_id) VALUES ($1) RETURNING id", tenant_id
        )
        await conn.execute(
            """
            INSERT INTO ledger_entries (tenant_id, wallet_id, type, credits_delta, balance_after)
            VALUES ($1, $2, 'topup', 100, 100)
            """,
            tenant_id,
            wallet_id,
        )

        sp = conn.transaction()
        await sp.start()
        with pytest.raises(asyncpg.PostgresError, match="no se borran físicamente"):
            await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await sp.rollback()
    finally:
        await outer.rollback()
        await conn.close()
