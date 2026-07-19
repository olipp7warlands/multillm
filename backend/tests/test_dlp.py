"""S1-9 · DLPService: Presidio en proceso, veredicto por dlp_settings,
placeholders numerados, poda de recognizers (SP-2, docs/spike.md)."""

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
from app.services import dlp
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


@pytest.fixture(scope="module", autouse=True)
def _dlp_engine():
    # Carga spaCy una vez para todo el módulo (1-3s, SP-2) — sin esto,
    # dlp.analyze() lanza RuntimeError (el lifespan de FastAPI no corre con
    # ASGITransport en los tests HTTP tampoco, así que hace falta igual).
    dlp.init_engine()


async def _register_tenant(email_prefix: str) -> dict:
    supabase_user_id = str(uuid.uuid4())
    identity = AuthenticatedIdentity(
        supabase_user_id=supabase_user_id, email=f"{email_prefix}@example.com"
    )
    slug = f"{email_prefix}-{uuid.uuid4().hex[:8]}"
    result = await register_tenant(
        slug=slug,
        tenant_name="DLP Test Co",
        billing_mode="reseller",
        owner_name="DLP Owner",
        identity=identity,
    )
    return {
        "slug": slug,
        "supabase_user_id": supabase_user_id,
        "tenant_id": result.tenant_id,
        "user_id": result.user_id,
        "division_id": result.division_id,
    }


@pytest.fixture(scope="module")
async def dlp_tenant():
    return await _register_tenant("dlp-test")


async def _set_dlp_mode(tenant_id: str, mode: str) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO dlp_settings (tenant_id, division_id, mode) VALUES ($1, NULL, $2)
                ON CONFLICT (tenant_id) WHERE division_id IS NULL DO UPDATE SET mode = $2
                """,
                uuid.UUID(tenant_id),
                mode,
            )
    finally:
        await conn.close()
    dlp.invalidate_dlp_cache(tenant_id)


async def _add_dictionary_term(tenant_id: str, user_id: str, term: str, category: str) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO dlp_dictionaries (tenant_id, division_id, term, category, created_by)
                VALUES ($1, NULL, $2, $3, $4)
                """,
                uuid.UUID(tenant_id),
                term,
                category,
                uuid.UUID(user_id),
            )
    finally:
        await conn.close()
    dlp.invalidate_dlp_cache(tenant_id)


async def test_clean_prompt_no_entities(dlp_tenant):
    await _set_dlp_mode(dlp_tenant["tenant_id"], "mask")
    result = await dlp.analyze(
        tenant_id=dlp_tenant["tenant_id"],
        division_id=dlp_tenant["division_id"],
        prompt="Hola, ¿qué tal el tiempo hoy?",
    )
    assert result.verdict == "clean"
    assert result.masked_text is None
    assert result.entities_summary == {}


async def test_mask_mode_masks_and_numbers_placeholders(dlp_tenant):
    await _set_dlp_mode(dlp_tenant["tenant_id"], "mask")
    prompt = "Soy Juan Perez, mi email es juan.perez@example.com. Juan Perez lo confirma."
    result = await dlp.analyze(
        tenant_id=dlp_tenant["tenant_id"], division_id=dlp_tenant["division_id"], prompt=prompt
    )
    assert result.verdict == "masked"
    assert "<PERSONA_1>" in result.masked_text
    assert "<EMAIL_1>" in result.masked_text
    assert "juan.perez@example.com" not in result.masked_text
    assert "Juan Perez" not in result.masked_text
    # la misma persona mencionada dos veces reutiliza el mismo placeholder,
    # no genera <PERSONA_2>
    assert result.masked_text.count("<PERSONA_1>") == 2
    assert result.entities_summary == {"PERSONA": 1, "EMAIL": 1}


async def test_block_mode_never_returns_masked_text(dlp_tenant):
    await _set_dlp_mode(dlp_tenant["tenant_id"], "block")
    result = await dlp.analyze(
        tenant_id=dlp_tenant["tenant_id"],
        division_id=dlp_tenant["division_id"],
        prompt="Mi teléfono es 612 345 678",
    )
    assert result.verdict == "blocked"
    assert result.masked_text is None
    assert result.entities_summary == {"TELEFONO": 1}


async def test_warn_mode_lets_through_but_reports_entities(dlp_tenant):
    await _set_dlp_mode(dlp_tenant["tenant_id"], "warn")
    result = await dlp.analyze(
        tenant_id=dlp_tenant["tenant_id"],
        division_id=dlp_tenant["division_id"],
        prompt="Vivo en Madrid",
    )
    assert result.verdict == "clean"
    assert result.masked_text is None
    assert result.entities_summary == {"LOCALIZACION": 1}


async def test_custom_dictionary_term_masked_with_category_label(dlp_tenant):
    await _set_dlp_mode(dlp_tenant["tenant_id"], "mask")
    await _add_dictionary_term(
        dlp_tenant["tenant_id"], dlp_tenant["user_id"], "Cliente Zafiro", "client"
    )
    result = await dlp.analyze(
        tenant_id=dlp_tenant["tenant_id"],
        division_id=dlp_tenant["division_id"],
        prompt="Estoy trabajando para Cliente Zafiro esta semana.",
    )
    assert result.verdict == "masked"
    assert "<CLIENTE_1>" in result.masked_text
    assert "Cliente Zafiro" not in result.masked_text
    assert result.entities_summary == {"CLIENTE": 1}


async def test_no_dlp_settings_row_fails_closed_to_block():
    fresh = await _register_tenant("dlp-noconf")
    result = await dlp.analyze(
        tenant_id=fresh["tenant_id"],
        division_id=fresh["division_id"],
        prompt="Mi NIF es 12345678Z",
    )
    assert result.verdict == "blocked"


async def test_dlp_analyze_endpoint(dlp_tenant):
    await _set_dlp_mode(dlp_tenant["tenant_id"], "mask")
    token = _sign_test_jwt(dlp_tenant["supabase_user_id"], "dlp-test@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/dlp/analyze",
            headers={
                "host": f"{dlp_tenant['slug']}.lvh.me:3000",
                "authorization": f"Bearer {token}",
            },
            json={"prompt": "Mi IBAN es ES9121000418450200051332"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "masked"
    assert "<IBAN_1>" in body["masked_text"]
    assert body["entities_summary"] == {"IBAN": 1}
