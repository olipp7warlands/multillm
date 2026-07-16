import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def _admin_dsn() -> str:
    return os.environ["DATABASE_URL_ADMIN"].replace("postgresql+asyncpg://", "postgresql://")


async def test_ledger_and_audit_are_immutable():
    """UPDATE/DELETE en ledger_entries y audit_events deben fallar por trigger,
    incluso conectando como postgres (que tiene privilegios de sobra sobre la
    tabla) — la inmutabilidad la impone el motor, no un GRANT que se pueda
    olvidar. Todo corre en una transacción con savepoints y se revierte al
    final: no se necesita (ni se puede) borrar las filas insertadas."""
    conn = await asyncpg.connect(_admin_dsn(), statement_cache_size=0, ssl="require")
    outer = conn.transaction()
    await outer.start()
    try:
        tenant_id = await conn.fetchval(
            "INSERT INTO tenants (slug, name, billing_mode) "
            "VALUES ($1, 'Test', 'reseller') RETURNING id",
            f"test-immutability-{uuid.uuid4().hex[:8]}",
        )
        wallet_id = await conn.fetchval(
            "INSERT INTO wallets (tenant_id) VALUES ($1) RETURNING id", tenant_id
        )
        entry_id = await conn.fetchval(
            """
            INSERT INTO ledger_entries (tenant_id, wallet_id, type, credits_delta, balance_after)
            VALUES ($1, $2, 'topup', 100, 100)
            RETURNING id
            """,
            tenant_id,
            wallet_id,
        )
        event_id = await conn.fetchval(
            "INSERT INTO audit_events (tenant_id, event_type) VALUES ($1, 'login') RETURNING id",
            tenant_id,
        )

        attempts = [
            "UPDATE ledger_entries SET note = 'hack' WHERE id = $1",
            "DELETE FROM ledger_entries WHERE id = $1",
        ]
        for stmt in attempts:
            sp = conn.transaction()
            await sp.start()
            with pytest.raises(asyncpg.PostgresError, match="inmutable"):
                await conn.execute(stmt, entry_id)
            await sp.rollback()

        attempts = [
            "UPDATE audit_events SET actor_role = 'hack' WHERE id = $1",
            "DELETE FROM audit_events WHERE id = $1",
        ]
        for stmt in attempts:
            sp = conn.transaction()
            await sp.start()
            with pytest.raises(asyncpg.PostgresError, match="inmutable"):
                await conn.execute(stmt, event_id)
            await sp.rollback()
    finally:
        await outer.rollback()
        await conn.close()
