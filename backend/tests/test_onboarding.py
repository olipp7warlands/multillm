"""S1-6 · Onboarding wizard (backend).

AC clave: una key inválida también se cifra antes de guardarse — nunca se
persiste en claro, ni siquiera cuando la validación falla. Reutiliza
ANTHROPIC_API_KEY del .env (real, con saldo — la misma cuenta validada en
el spike SP-1) para probar el camino "key válida" con una llamada de test
real al proveedor, sin gastar de más (max_tokens=1).
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
from app.services.gateway import decrypt_provider_key
from app.services.onboarding import set_dlp_preset, validate_and_store_key

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
async def onboarding_tenant():
    supabase_user_id = str(uuid.uuid4())
    identity = AuthenticatedIdentity(
        supabase_user_id=supabase_user_id, email="onb-owner@example.com"
    )
    slug = f"onboarding-test-{uuid.uuid4().hex[:8]}"
    result = await register_tenant(
        slug=slug,
        tenant_name="Onboarding Test Co",
        billing_mode="reseller",
        owner_name="Onboarding Owner",
        identity=identity,
    )
    return {
        "slug": slug,
        "supabase_user_id": supabase_user_id,
        "tenant_id": result.tenant_id,
        "user_id": result.user_id,
    }


async def _fetch_provider_connection(tenant_id: str, provider_slug: str):
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            row = await conn.fetchrow(
                """
                SELECT pc.encrypted_key, pc.key_last4, pc.status
                FROM provider_connections pc
                JOIN providers p ON p.id = pc.provider_id
                WHERE pc.tenant_id = $1 AND p.slug = $2
                """,
                uuid.UUID(tenant_id),
                provider_slug,
            )
            return dict(row) if row else None
    finally:
        await conn.close()


async def test_validate_key_valid_encrypts_and_never_stores_plaintext(onboarding_tenant):
    real_key = os.environ["ANTHROPIC_API_KEY"]
    result = await validate_and_store_key(
        tenant_id=onboarding_tenant["tenant_id"],
        provider_slug="anthropic",
        api_key=real_key,
        created_by=onboarding_tenant["user_id"],
    )
    assert result.status == "valid"
    assert result.key_last4 == real_key[-4:]

    row = await _fetch_provider_connection(onboarding_tenant["tenant_id"], "anthropic")
    assert row is not None
    assert row["status"] == "valid"
    # nunca en claro: los bytes cifrados no pueden ser el texto plano ni contenerlo
    assert bytes(row["encrypted_key"]) != real_key.encode("utf-8")
    assert real_key not in bytes(row["encrypted_key"]).decode("utf-8", errors="ignore")
    # pero descifra correctamente (round-trip real, no solo "hay bytes ahí")
    assert decrypt_provider_key(bytes(row["encrypted_key"])) == real_key


async def test_validate_key_invalid_still_never_stores_plaintext(onboarding_tenant):
    fake_key = "sk-esto-no-es-una-key-valida-de-verdad-123456"
    result = await validate_and_store_key(
        tenant_id=onboarding_tenant["tenant_id"],
        provider_slug="openai",
        api_key=fake_key,
        created_by=onboarding_tenant["user_id"],
    )
    assert result.status == "invalid"

    row = await _fetch_provider_connection(onboarding_tenant["tenant_id"], "openai")
    assert row is not None
    assert row["status"] == "invalid"
    assert bytes(row["encrypted_key"]) != fake_key.encode("utf-8")
    assert fake_key not in bytes(row["encrypted_key"]).decode("utf-8", errors="ignore")
    assert decrypt_provider_key(bytes(row["encrypted_key"])) == fake_key


async def test_enable_models_endpoint(onboarding_tenant):
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        model_id = await conn.fetchval("SELECT id FROM models LIMIT 1")
    finally:
        await conn.close()

    token = _sign_test_jwt(onboarding_tenant["supabase_user_id"], "onb-owner@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/onboarding/enable-models",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
            json={"model_ids": [str(model_id)]},
        )
    assert response.status_code == 200

    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", onboarding_tenant["tenant_id"]
            )
            row = await conn.fetchrow(
                "SELECT enabled, source FROM tenant_model_access "
                "WHERE tenant_id = $1 AND model_id = $2",
                uuid.UUID(onboarding_tenant["tenant_id"]),
                model_id,
            )
    finally:
        await conn.close()
    assert row["enabled"] is True
    assert row["source"] == "reseller"


@pytest.mark.parametrize(
    "preset,expected_mode", [("strict", "block"), ("balanced", "mask"), ("warn_only", "warn")]
)
async def test_dlp_preset_mapping(onboarding_tenant, preset, expected_mode):
    mode = await set_dlp_preset(tenant_id=onboarding_tenant["tenant_id"], preset=preset)
    assert mode == expected_mode

    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", onboarding_tenant["tenant_id"]
            )
            stored_mode = await conn.fetchval(
                "SELECT mode FROM dlp_settings WHERE tenant_id = $1 AND division_id IS NULL",
                uuid.UUID(onboarding_tenant["tenant_id"]),
            )
    finally:
        await conn.close()
    assert stored_mode == expected_mode


async def test_complete_onboarding_endpoint(onboarding_tenant):
    token = _sign_test_jwt(onboarding_tenant["supabase_user_id"], "onb-owner@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/onboarding/complete",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"completed": True}

    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", onboarding_tenant["tenant_id"]
            )
            n = await conn.fetchval(
                "SELECT count(*) FROM audit_events "
                "WHERE tenant_id = $1 AND event_type = 'onboarding_completed'",
                uuid.UUID(onboarding_tenant["tenant_id"]),
            )
    finally:
        await conn.close()
    assert n == 1


async def test_models_catalog_endpoint(onboarding_tenant):
    token = _sign_test_jwt(onboarding_tenant["supabase_user_id"], "onb-owner@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/onboarding/models-catalog",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
        )
    assert response.status_code == 200
    models = response.json()["models"]
    assert len(models) >= 3  # los 3 del seed de la migración 001
    for model in models:
        assert model["prices"], f"{model['slug']} sin precios vigentes"
        assert all(p["credit_price"] > 0 for p in model["prices"])


async def test_invite_team_endpoint_creates_invitations(onboarding_tenant):
    token = _sign_test_jwt(onboarding_tenant["supabase_user_id"], "onb-owner@example.com")
    emails = [f"invitee-{uuid.uuid4().hex[:6]}@example.com" for _ in range(2)]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/onboarding/invite-team",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
            json={"emails": emails},
        )
    assert response.status_code == 200
    assert response.json() == {"invited": 2}

    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", onboarding_tenant["tenant_id"]
            )
            rows = await conn.fetch(
                "SELECT email, role FROM invitations WHERE tenant_id = $1 AND email = ANY($2)",
                uuid.UUID(onboarding_tenant["tenant_id"]),
                emails,
            )
    finally:
        await conn.close()
    assert len(rows) == 2
    assert all(r["role"] == "user" for r in rows)


async def test_enabled_models_endpoint_reflects_enable_models(onboarding_tenant):
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", onboarding_tenant["tenant_id"]
            )
            model_id = await conn.fetchval(
                "SELECT id FROM models WHERE id NOT IN "
                "(SELECT model_id FROM tenant_model_access WHERE tenant_id = $1) LIMIT 1",
                uuid.UUID(onboarding_tenant["tenant_id"]),
            )
    finally:
        await conn.close()

    token = _sign_test_jwt(onboarding_tenant["supabase_user_id"], "onb-owner@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get(
            "/api/models/enabled",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
        )
        assert before.status_code == 200
        before_ids = {m["id"] for m in before.json()["models"]}
        assert str(model_id) not in before_ids

        await client.post(
            "/api/onboarding/enable-models",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
            json={"model_ids": [str(model_id)]},
        )

        after = await client.get(
            "/api/models/enabled",
            headers={
                "host": f"{onboarding_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
        )
    after_ids = {m["id"] for m in after.json()["models"]}
    assert str(model_id) in after_ids
