"""S1-8 · PolicyService: visibilidad de modelo, saldo, rate limit."""

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import jwt
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import policy as policy_module
from app.services.auth import AuthenticatedIdentity, register_tenant
from app.services.policy import PolicyDeniedError
from app.services.policy import check as policy_check

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def _app_backend_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


def _sign_test_jwt(supabase_user_id: str, email: str) -> str:
    payload = {
        "sub": supabase_user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")


@pytest.fixture(scope="module")
async def policy_tenant():
    supabase_user_id = str(uuid.uuid4())
    identity = AuthenticatedIdentity(
        supabase_user_id=supabase_user_id, email="policy-owner@example.com"
    )
    slug = f"policy-test-{uuid.uuid4().hex[:8]}"
    result = await register_tenant(
        slug=slug,
        tenant_name="Policy Test Co",
        billing_mode="reseller",
        owner_name="Policy Owner",
        identity=identity,
    )
    return {
        "slug": slug,
        "supabase_user_id": supabase_user_id,
        "tenant_id": result.tenant_id,
        "user_id": result.user_id,
        "division_id": result.division_id,
    }


async def _get_model_id(slug: str) -> uuid.UUID:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        return await conn.fetchval("SELECT id FROM models WHERE slug = $1", slug)
    finally:
        await conn.close()


async def _set_tenant_model_access(
    tenant_id: str,
    model_id,
    *,
    enabled: bool = True,
    min_role: str | None = None,
    source: str = "reseller",
) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO tenant_model_access (tenant_id, model_id, enabled, min_role, source)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (tenant_id, model_id) DO UPDATE
                    SET enabled = $3, min_role = $4, source = $5
                """,
                uuid.UUID(tenant_id),
                model_id,
                enabled,
                min_role,
                source,
            )
    finally:
        await conn.close()


async def _set_wallet_balance(tenant_id: str, balance: int) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO wallets (tenant_id, balance_cached) VALUES ($1, $2)
                ON CONFLICT (tenant_id) DO UPDATE SET balance_cached = $2
                """,
                uuid.UUID(tenant_id),
                balance,
            )
    finally:
        await conn.close()


async def _set_division_allocation(
    tenant_id: str, division_id: str, period: str, allocated: int, consumed: int
) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO division_allocations
                    (tenant_id, division_id, period, allocated_credits, consumed_credits_cached)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (tenant_id, division_id, period) DO UPDATE
                    SET allocated_credits = $4, consumed_credits_cached = $5
                """,
                uuid.UUID(tenant_id),
                uuid.UUID(division_id),
                period,
                allocated,
                consumed,
            )
    finally:
        await conn.close()


async def _insert_extra_user(tenant_id: str, division_id: str) -> str:
    """Un segundo usuario del mismo tenant, real en `users` (audit_events.
    actor_user_id tiene FK) — para el test de rate limit, que necesita un
    user_id propio y sin contadores ya gastados por otros tests del módulo."""
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            user_id = await conn.fetchval(
                """
                INSERT INTO users (tenant_id, email, name, supabase_user_id)
                VALUES ($1, $2, 'Extra User', $3) RETURNING id
                """,
                uuid.UUID(tenant_id),
                f"policy-extra-{uuid.uuid4().hex[:8]}@example.com",
                uuid.uuid4(),
            )
            await conn.execute(
                """
                INSERT INTO memberships (user_id, division_id, tenant_id, role)
                VALUES ($1, $2, $3, 'user')
                """,
                user_id,
                uuid.UUID(division_id),
                uuid.UUID(tenant_id),
            )
            return str(user_id)
    finally:
        await conn.close()


async def test_model_not_enabled_denies(policy_tenant):
    model_id = await _get_model_id("gemini-flash")
    with pytest.raises(PolicyDeniedError) as exc_info:
        await policy_check(
            tenant_id=policy_tenant["tenant_id"],
            user_id=policy_tenant["user_id"],
            role="owner",
            division_id=policy_tenant["division_id"],
            model_id=str(model_id),
        )
    assert exc_info.value.reason == "model_not_enabled"


async def test_min_role_denies_lower_role(policy_tenant):
    model_id = await _get_model_id("claude-haiku-4-5")
    await _set_tenant_model_access(
        policy_tenant["tenant_id"], model_id, enabled=True, min_role="admin", source="byok"
    )

    with pytest.raises(PolicyDeniedError) as exc_info:
        await policy_check(
            tenant_id=policy_tenant["tenant_id"],
            user_id=policy_tenant["user_id"],
            role="user",
            division_id=policy_tenant["division_id"],
            model_id=str(model_id),
        )
    assert exc_info.value.reason == "model_not_enabled"

    # con rol suficiente sí pasa — BYOK, sin check de saldo
    result = await policy_check(
        tenant_id=policy_tenant["tenant_id"],
        user_id=policy_tenant["user_id"],
        role="admin",
        division_id=policy_tenant["division_id"],
        model_id=str(model_id),
    )
    assert result.model_source == "byok"


async def test_reseller_without_wallet_denies_no_balance(policy_tenant):
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(
        policy_tenant["tenant_id"], model_id, enabled=True, source="reseller"
    )

    with pytest.raises(PolicyDeniedError) as exc_info:
        await policy_check(
            tenant_id=policy_tenant["tenant_id"],
            user_id=policy_tenant["user_id"],
            role="owner",
            division_id=policy_tenant["division_id"],
            model_id=str(model_id),
        )
    assert exc_info.value.reason == "no_balance"


async def test_reseller_with_balance_allows_and_division_budget_gate(policy_tenant):
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(
        policy_tenant["tenant_id"], model_id, enabled=True, source="reseller"
    )
    await _set_wallet_balance(policy_tenant["tenant_id"], 100)

    result = await policy_check(
        tenant_id=policy_tenant["tenant_id"],
        user_id=policy_tenant["user_id"],
        role="owner",
        division_id=policy_tenant["division_id"],
        model_id=str(model_id),
    )
    assert result.model_source == "reseller"

    # presupuesto de división agotado para el periodo actual -> deniega
    # aunque haya saldo de tenant de sobra
    period = datetime.now(UTC).strftime("%Y-%m")
    await _set_division_allocation(
        policy_tenant["tenant_id"], policy_tenant["division_id"], period, 10, 10
    )

    with pytest.raises(PolicyDeniedError) as exc_info:
        await policy_check(
            tenant_id=policy_tenant["tenant_id"],
            user_id=policy_tenant["user_id"],
            role="owner",
            division_id=policy_tenant["division_id"],
            model_id=str(model_id),
        )
    assert exc_info.value.reason == "no_balance"


async def test_rate_limit_denies_after_threshold(policy_tenant, monkeypatch):
    monkeypatch.setattr(policy_module, "RATE_LIMIT_USER_PER_MINUTE", 2)
    monkeypatch.setattr(policy_module, "RATE_LIMIT_TENANT_PER_MINUTE", 1000)

    model_id = await _get_model_id("gemini-flash")
    await _set_tenant_model_access(
        policy_tenant["tenant_id"], model_id, enabled=True, source="byok"
    )
    fresh_user_id = await _insert_extra_user(
        policy_tenant["tenant_id"], policy_tenant["division_id"]
    )

    for _ in range(2):
        await policy_check(
            tenant_id=policy_tenant["tenant_id"],
            user_id=fresh_user_id,
            role="user",
            division_id=policy_tenant["division_id"],
            model_id=str(model_id),
        )

    with pytest.raises(PolicyDeniedError) as exc_info:
        await policy_check(
            tenant_id=policy_tenant["tenant_id"],
            user_id=fresh_user_id,
            role="user",
            division_id=policy_tenant["division_id"],
            model_id=str(model_id),
        )
    assert exc_info.value.reason == "rate_limited"


async def test_policy_check_endpoint_denies_with_reason(policy_tenant):
    # claude-haiku-4-5 quedó habilitado (min_role=admin, byok) en un test anterior
    model_id = await _get_model_id("claude-haiku-4-5")
    token = _sign_test_jwt(policy_tenant["supabase_user_id"], "policy-owner@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.post(
            "/api/policy/check",
            headers={
                "host": f"{policy_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
            json={"model_id": str(model_id)},
        )
        denied = await client.post(
            "/api/policy/check",
            headers={
                "host": f"{policy_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
            json={"model_id": str(uuid.uuid4())},
        )
    assert allowed.status_code == 200
    assert allowed.json() == {"allowed": True, "model_source": "byok"}
    assert denied.status_code == 403
    assert denied.json()["reason"] == "model_not_enabled"
