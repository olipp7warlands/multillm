"""S1-5 · Auth con Supabase.

Los JWT de prueba se firman a mano con SUPABASE_JWT_SECRET (mismo secreto
que usa el backend para verificar) — no hace falta un login real contra
Supabase Auth para probar la verificación del token ni el resto del flujo.

Como los tenants no se borran físicamente (docs/MODELO_DATOS.md) y
`register_tenant` siempre inserta un tenant nuevo (es su propósito), cada
ejecución de este archivo deja un tenant de prueba permanente con un slug
único — mismo trade-off ya aceptado en test_rls.py.
"""

import os
import time
import uuid
from pathlib import Path

import asyncpg
import jwt
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.auth import AuthenticatedIdentity, register_tenant

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


async def _request(method: str, path: str, host: str, token: str | None = None, json=None):
    headers = {"host": host}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


async def test_register_tenant_creates_tenant_owner_division_in_one_tx():
    supabase_user_id = str(uuid.uuid4())
    identity = AuthenticatedIdentity(supabase_user_id=supabase_user_id, email="owner@example.com")
    slug = f"auth-test-{uuid.uuid4().hex[:8]}"

    result = await register_tenant(
        slug=slug,
        tenant_name="Auth Test Co",
        billing_mode="reseller",
        owner_name="Owner Test",
        identity=identity,
    )

    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        tenant = await conn.fetchrow(
            "SELECT slug, billing_mode FROM tenants WHERE id = $1", uuid.UUID(result.tenant_id)
        )
        assert tenant["slug"] == slug
        assert tenant["billing_mode"] == "reseller"

        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", result.tenant_id)
            division = await conn.fetchrow(
                "SELECT is_default FROM divisions WHERE id = $1", uuid.UUID(result.division_id)
            )
            assert division["is_default"] is True

            membership = await conn.fetchrow(
                "SELECT role FROM memberships WHERE user_id = $1", uuid.UUID(result.user_id)
            )
            assert membership["role"] == "owner"

            audit = await conn.fetchval(
                "SELECT count(*) FROM audit_events "
                "WHERE tenant_id = $1 AND event_type = 'tenant_registered'",
                uuid.UUID(result.tenant_id),
            )
            assert audit == 1
    finally:
        await conn.close()


@pytest.fixture(scope="module")
async def registered_tenant():
    supabase_user_id = str(uuid.uuid4())
    identity = AuthenticatedIdentity(supabase_user_id=supabase_user_id, email="owner2@example.com")
    slug = f"auth-test-{uuid.uuid4().hex[:8]}"
    result = await register_tenant(
        slug=slug,
        tenant_name="Auth Test Co 2",
        billing_mode="reseller",
        owner_name="Owner Two",
        identity=identity,
    )
    return {
        "slug": slug,
        "supabase_user_id": supabase_user_id,
        "tenant_id": result.tenant_id,
        "division_id": result.division_id,
    }


async def test_login_resolves_membership_and_writes_audit_event(registered_tenant):
    token = _sign_test_jwt(registered_tenant["supabase_user_id"], "owner2@example.com")
    response = await _request(
        "POST", "/api/auth/login", f"{registered_tenant['slug']}.lvh.me:3000", token=token
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "owner"
    assert body["tenant_id"] == registered_tenant["tenant_id"]

    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", registered_tenant["tenant_id"]
            )
            n = await conn.fetchval(
                "SELECT count(*) FROM audit_events WHERE tenant_id = $1 AND event_type = 'login'",
                uuid.UUID(registered_tenant["tenant_id"]),
            )
            assert n == 1
    finally:
        await conn.close()


async def test_admin_ping_allows_owner(registered_tenant):
    token = _sign_test_jwt(registered_tenant["supabase_user_id"], "owner2@example.com")
    response = await _request(
        "GET", "/api/admin/ping", f"{registered_tenant['slug']}.lvh.me:3000", token=token
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "role": "owner"}


async def test_admin_ping_blocks_insufficient_role(registered_tenant):
    plain_user_id = str(uuid.uuid4())
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", registered_tenant["tenant_id"]
            )
            user_id = await conn.fetchval(
                """
                INSERT INTO users (tenant_id, email, name, supabase_user_id)
                VALUES ($1, 'plain@example.com', 'Plain', $2) RETURNING id
                """,
                uuid.UUID(registered_tenant["tenant_id"]),
                uuid.UUID(plain_user_id),
            )
            await conn.execute(
                """
                INSERT INTO memberships (user_id, division_id, tenant_id, role)
                VALUES ($1, $2, $3, 'user')
                """,
                user_id,
                uuid.UUID(registered_tenant["division_id"]),
                uuid.UUID(registered_tenant["tenant_id"]),
            )
    finally:
        await conn.close()

    token = _sign_test_jwt(plain_user_id, "plain@example.com")
    response = await _request(
        "GET", "/api/admin/ping", f"{registered_tenant['slug']}.lvh.me:3000", token=token
    )
    assert response.status_code == 403


async def test_missing_token_is_401(registered_tenant):
    response = await _request(
        "GET", "/api/admin/ping", f"{registered_tenant['slug']}.lvh.me:3000", token=None
    )
    assert response.status_code == 401


async def test_garbage_token_is_401(registered_tenant):
    response = await _request(
        "GET",
        "/api/admin/ping",
        f"{registered_tenant['slug']}.lvh.me:3000",
        token="esto-no-es-un-jwt-valido",
    )
    assert response.status_code == 401
