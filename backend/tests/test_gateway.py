"""S1-10 · GatewayService + streaming: pipeline completo policy -> DLP ->
hold -> litellm streaming -> transacción final. `litellm.acompletion` se
mockea siempre (sin llamadas reales a proveedores en tests)."""

import json
import logging
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import jwt
import litellm
import pytest
from dotenv import load_dotenv

from app.services import dlp, metering
from app.services.auth import AuthenticatedIdentity, register_tenant
from app.services.gateway import DLPBlockedError, DLPMaskedError, prepare_stream, stream_chat
from app.services.gateway import encrypt_provider_key as gw_encrypt_provider_key
from app.services.gateway import settings as gateway_settings
from app.services.policy import PolicyDeniedError

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
    dlp.init_engine()


async def _register_tenant(email_prefix: str) -> dict:
    supabase_user_id = str(uuid.uuid4())
    identity = AuthenticatedIdentity(
        supabase_user_id=supabase_user_id, email=f"{email_prefix}@example.com"
    )
    slug = f"{email_prefix}-{uuid.uuid4().hex[:8]}"
    result = await register_tenant(
        slug=slug,
        tenant_name="Gateway Test Co",
        billing_mode="reseller",
        owner_name="Gateway Owner",
        identity=identity,
    )
    return {
        "slug": slug,
        "supabase_user_id": supabase_user_id,
        "tenant_id": result.tenant_id,
        "user_id": result.user_id,
        "division_id": result.division_id,
    }


@pytest.fixture
async def gateway_tenant():
    return await _register_tenant("gateway-test")


async def _get_model_id(slug: str) -> uuid.UUID:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        return await conn.fetchval("SELECT id FROM models WHERE slug = $1", slug)
    finally:
        await conn.close()


async def _set_tenant_model_access(
    tenant_id: str, model_id, *, source: str, min_role: str | None = None
) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO tenant_model_access (tenant_id, model_id, enabled, min_role, source)
                VALUES ($1, $2, true, $3, $4)
                ON CONFLICT (tenant_id, model_id) DO UPDATE
                    SET enabled = true, min_role = $3, source = $4
                """,
                uuid.UUID(tenant_id),
                model_id,
                min_role,
                source,
            )
    finally:
        await conn.close()


async def _set_wallet_balance(tenant_id: str, balance) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO wallets (tenant_id, balance_cached) VALUES ($1, $2)
                ON CONFLICT (tenant_id) DO UPDATE SET balance_cached = $2, reserved_amount = 0
                """,
                uuid.UUID(tenant_id),
                balance,
            )
    finally:
        await conn.close()


async def _read_wallet(tenant_id: str) -> dict:
    # set_config(..., true) es SET LOCAL: solo vale dentro de la MISMA
    # transacción — fuera de ella, la lectura ve 0 filas (regla 7, CLAUDE.md).
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            row = await conn.fetchrow(
                "SELECT balance_cached, reserved_amount FROM wallets WHERE tenant_id = $1",
                uuid.UUID(tenant_id),
            )
        return dict(row) if row else {}
    finally:
        await conn.close()


async def _set_provider_connection(
    tenant_id: str, provider_slug: str, plaintext_key: str, *, created_by: str
) -> None:
    # Sin UNIQUE(tenant_id, provider_id) en el esquema (ver onboarding.py,
    # que hace el mismo SELECT-luego-UPDATE-o-INSERT) — ON CONFLICT no aplica aquí.
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            provider_id = await conn.fetchval(
                "SELECT id FROM providers WHERE slug = $1", provider_slug
            )
            encrypted = gw_encrypt_provider_key(plaintext_key)
            existing_id = await conn.fetchval(
                "SELECT id FROM provider_connections WHERE tenant_id = $1 AND provider_id = $2",
                uuid.UUID(tenant_id),
                provider_id,
            )
            if existing_id is not None:
                await conn.execute(
                    """
                    UPDATE provider_connections
                    SET encrypted_key = $1, key_last4 = $2, status = 'valid', validated_at = now()
                    WHERE id = $3
                    """,
                    encrypted,
                    plaintext_key[-4:],
                    existing_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO provider_connections
                        (tenant_id, provider_id, encrypted_key, key_last4, status, validated_at,
                         created_by)
                    VALUES ($1, $2, $3, $4, 'valid', now(), $5)
                    """,
                    uuid.UUID(tenant_id),
                    provider_id,
                    encrypted,
                    plaintext_key[-4:],
                    uuid.UUID(created_by),
                )
    finally:
        await conn.close()


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


async def _fetch_one(tenant_id: str, query: str, *args):
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            return await conn.fetchrow(query, uuid.UUID(tenant_id), *args)
    finally:
        await conn.close()


async def _count(tenant_id: str, table: str) -> int:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            return await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE tenant_id = $1", uuid.UUID(tenant_id)
            )  # noqa: S608
    finally:
        await conn.close()


def _content_chunk(text_piece: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text_piece))], usage=None
    )


def _usage_chunk(tokens_in: int, tokens_out: int):
    return SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out)
    )


def _install_fake_acompletion(
    monkeypatch, chunks, *, raise_after: int | None = None, captured_calls=None
):
    async def _gen():
        for i, chunk in enumerate(chunks):
            if raise_after is not None and i == raise_after:
                raise RuntimeError("boom del proveedor a mitad de stream")
            yield chunk

    async def fake_acompletion(**kwargs):
        if captured_calls is not None:
            captured_calls.append(kwargs)
        return _gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)


async def _drain(chat) -> list[dict]:
    events = []
    async for raw in stream_chat(chat):
        line = raw.decode().strip()
        assert line.startswith("data: ")
        events.append(json.loads(line[len("data: ") :]))
    return events


async def test_clean_reseller_happy_path(gateway_tenant, monkeypatch):
    monkeypatch.setattr(gateway_settings, "openai_api_key", "sk-test-master")
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("1000"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    _install_fake_acompletion(
        monkeypatch, [_content_chunk("Hola"), _content_chunk(" mundo"), _usage_chunk(10, 5)]
    )

    chat = await prepare_stream(
        tenant_id=gateway_tenant["tenant_id"],
        user_id=gateway_tenant["user_id"],
        role="owner",
        division_id=gateway_tenant["division_id"],
        model_id=str(model_id),
        prompt="Hola, ¿qué tal el tiempo hoy?",
        conversation_id=None,
        confirm_masked=False,
        requested_max_tokens=100,
    )
    events = await _drain(chat)

    done = events[-1]
    assert done["type"] == "done"
    assert done["conversation_id"] is not None
    assert Decimal(done["credits_charged"]) > 0
    assert "".join(e["content"] for e in events if e["type"] == "chunk") == "Hola mundo"

    row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT status, dlp_verdict, tokens_in, tokens_out, credits_charged "
        "FROM requests WHERE tenant_id = $1",
    )
    assert row["status"] == "completed"
    assert row["dlp_verdict"] == "clean"
    assert row["tokens_in"] == 10
    assert row["tokens_out"] == 5
    assert row["credits_charged"] == Decimal(done["credits_charged"])

    wallet = await _read_wallet(gateway_tenant["tenant_id"])
    assert wallet["reserved_amount"] == 0  # hold liberado
    assert wallet["balance_cached"] == Decimal("1000") - row["credits_charged"]

    ledger_count = await _count(gateway_tenant["tenant_id"], "ledger_entries")
    assert ledger_count == 1


async def test_masked_without_confirm_persists_nothing(gateway_tenant, monkeypatch):
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("1000"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    before = await _count(gateway_tenant["tenant_id"], "requests")

    with pytest.raises(DLPMaskedError) as exc_info:
        await prepare_stream(
            tenant_id=gateway_tenant["tenant_id"],
            user_id=gateway_tenant["user_id"],
            role="owner",
            division_id=gateway_tenant["division_id"],
            model_id=str(model_id),
            prompt="Soy Juan Perez, mi email es juan.perez@example.com.",
            conversation_id=None,
            confirm_masked=False,
            requested_max_tokens=100,
        )
    assert "<PERSONA_1>" in exc_info.value.masked_text
    assert "Juan Perez" not in exc_info.value.masked_text

    after = await _count(gateway_tenant["tenant_id"], "requests")
    assert after == before


async def test_masked_confirmed_sends_masked_text_but_stores_original(gateway_tenant, monkeypatch):
    monkeypatch.setattr(gateway_settings, "openai_api_key", "sk-test-master")
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("1000"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    captured_calls: list[dict] = []
    _install_fake_acompletion(
        monkeypatch, [_content_chunk("ok"), _usage_chunk(8, 3)], captured_calls=captured_calls
    )

    original_prompt = "Soy Juan Perez, mi email es juan.perez@example.com."
    chat = await prepare_stream(
        tenant_id=gateway_tenant["tenant_id"],
        user_id=gateway_tenant["user_id"],
        role="owner",
        division_id=gateway_tenant["division_id"],
        model_id=str(model_id),
        prompt=original_prompt,
        conversation_id=None,
        confirm_masked=True,
        requested_max_tokens=100,
    )
    await _drain(chat)

    # regla interno/externo: al proveedor SIEMPRE la versión enmascarada
    sent_content = captured_calls[0]["messages"][0]["content"]
    assert "Juan Perez" not in sent_content
    assert "juan.perez@example.com" not in sent_content
    assert "<PERSONA_1>" in sent_content

    # lo persistido internamente es SIEMPRE el original
    row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT content FROM messages WHERE tenant_id = $1 AND role = 'user'",
    )
    assert row["content"] == original_prompt


async def test_blocked_prompt_persists_requests_row_and_audit(gateway_tenant):
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("1000"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "block")

    with pytest.raises(DLPBlockedError):
        await prepare_stream(
            tenant_id=gateway_tenant["tenant_id"],
            user_id=gateway_tenant["user_id"],
            role="owner",
            division_id=gateway_tenant["division_id"],
            model_id=str(model_id),
            prompt="Mi teléfono es 612 345 678",
            conversation_id=None,
            confirm_masked=False,
            requested_max_tokens=100,
        )

    row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT status, dlp_verdict FROM requests WHERE tenant_id = $1",
    )
    assert row["status"] == "blocked_dlp"
    assert row["dlp_verdict"] == "blocked"

    audit_row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT event_type FROM audit_events WHERE tenant_id = $1 AND event_type = 'dlp_block'",
    )
    assert audit_row is not None


async def test_no_balance_denies_before_calling_provider(gateway_tenant, monkeypatch):
    monkeypatch.setattr(gateway_settings, "openai_api_key", "sk-test-master")
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    # saldo positivo (pasa el check de PolicyService) pero muy por debajo
    # de lo que costaría el hold estimado para max_tokens=100
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("0.0001"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    called = False

    async def fake_acompletion(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("no debería llamarse al proveedor sin saldo para el hold")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(PolicyDeniedError) as exc_info:
        await prepare_stream(
            tenant_id=gateway_tenant["tenant_id"],
            user_id=gateway_tenant["user_id"],
            role="owner",
            division_id=gateway_tenant["division_id"],
            model_id=str(model_id),
            prompt="Hola, ¿qué tal el tiempo hoy?",
            conversation_id=None,
            confirm_masked=False,
            requested_max_tokens=100,
        )
    assert exc_info.value.reason == "no_balance"
    assert called is False


async def test_byok_happy_path_never_touches_ledger(gateway_tenant, monkeypatch):
    model_id = await _get_model_id("claude-haiku-4-5")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="byok")
    await _set_provider_connection(
        gateway_tenant["tenant_id"],
        "anthropic",
        "sk-ant-real-key-1234",
        created_by=gateway_tenant["user_id"],
    )
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    _install_fake_acompletion(monkeypatch, [_content_chunk("hola"), _usage_chunk(6, 2)])

    ledger_before = await _count(gateway_tenant["tenant_id"], "ledger_entries")

    chat = await prepare_stream(
        tenant_id=gateway_tenant["tenant_id"],
        user_id=gateway_tenant["user_id"],
        role="owner",
        division_id=gateway_tenant["division_id"],
        model_id=str(model_id),
        prompt="Hola, ¿qué tal el tiempo hoy?",
        conversation_id=None,
        confirm_masked=False,
        requested_max_tokens=100,
    )
    await _drain(chat)

    row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT status, credits_charged, provider_cost_eur FROM requests WHERE tenant_id = $1",
    )
    assert row["status"] == "completed"
    assert row["credits_charged"] is None
    assert row["provider_cost_eur"] is None

    ledger_after = await _count(gateway_tenant["tenant_id"], "ledger_entries")
    assert ledger_after == ledger_before


async def test_provider_error_mid_stream_releases_hold_without_charging(
    gateway_tenant, monkeypatch
):
    monkeypatch.setattr(gateway_settings, "openai_api_key", "sk-test-master")
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("1000"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    _install_fake_acompletion(monkeypatch, [_content_chunk("hola")], raise_after=0)

    chat = await prepare_stream(
        tenant_id=gateway_tenant["tenant_id"],
        user_id=gateway_tenant["user_id"],
        role="owner",
        division_id=gateway_tenant["division_id"],
        model_id=str(model_id),
        prompt="Hola, ¿qué tal el tiempo hoy?",
        conversation_id=None,
        confirm_masked=False,
        requested_max_tokens=100,
    )
    wallet_with_hold = await _read_wallet(gateway_tenant["tenant_id"])
    assert wallet_with_hold["reserved_amount"] > 0

    ledger_before = await _count(gateway_tenant["tenant_id"], "ledger_entries")
    events = await _drain(chat)

    assert events[-1]["type"] == "error"

    row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT status, credits_charged FROM requests WHERE tenant_id = $1",
    )
    assert row["status"] == "provider_error"
    assert row["credits_charged"] is None

    wallet_after = await _read_wallet(gateway_tenant["tenant_id"])
    assert wallet_after["reserved_amount"] == 0
    assert wallet_after["balance_cached"] == Decimal("1000")  # nunca se cobró

    ledger_after = await _count(gateway_tenant["tenant_id"], "ledger_entries")
    assert ledger_after == ledger_before


async def test_finalize_failure_after_successful_stream_releases_hold(gateway_tenant, monkeypatch):
    """Endurecimiento S1-11: un fallo INESPERADO durante la transacción
    final (aquí, tarifa de cambio ausente) después de que el streaming ya
    completó con éxito no debe dejar el generador colgado ni el hold
    huérfano — se trata igual que un provider_error real."""
    monkeypatch.setattr(gateway_settings, "openai_api_key", "sk-test-master")
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("1000"))
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    _install_fake_acompletion(monkeypatch, [_content_chunk("hola"), _usage_chunk(10, 5)])

    # `current_rates` se llama dos veces en un flujo reseller feliz: una
    # para estimar el hold (prepare_stream, antes del streaming) y otra en
    # la transacción final (_finalize_success). Deja pasar la primera tal
    # cual (si no, ni siquiera se llega a reservar el hold) y falla la
    # segunda — así se aísla el fallo al punto que este test quiere probar.
    real_current_rates = metering.current_rates
    call_count = 0

    async def _boom(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await real_current_rates(*args, **kwargs)
        raise metering.MissingExchangeRateError(str(model_id))

    monkeypatch.setattr(metering, "current_rates", _boom)

    chat = await prepare_stream(
        tenant_id=gateway_tenant["tenant_id"],
        user_id=gateway_tenant["user_id"],
        role="owner",
        division_id=gateway_tenant["division_id"],
        model_id=str(model_id),
        prompt="Hola, ¿qué tal el tiempo hoy?",
        conversation_id=None,
        confirm_masked=False,
        requested_max_tokens=100,
    )
    ledger_before = await _count(gateway_tenant["tenant_id"], "ledger_entries")
    events = await _drain(chat)

    assert events[-1]["type"] == "error"

    row = await _fetch_one(
        gateway_tenant["tenant_id"],
        "SELECT status, credits_charged FROM requests WHERE tenant_id = $1",
    )
    assert row["status"] == "provider_error"
    assert row["credits_charged"] is None

    wallet_after = await _read_wallet(gateway_tenant["tenant_id"])
    assert wallet_after["reserved_amount"] == 0
    assert wallet_after["balance_cached"] == Decimal("1000")  # nunca se cobró

    ledger_after = await _count(gateway_tenant["tenant_id"], "ledger_entries")
    assert ledger_after == ledger_before  # ni el intento fallido de finalize dejó rastro


async def test_no_secrets_or_original_masked_text_in_logs(gateway_tenant, monkeypatch, caplog):
    """Regla 3 (CLAUDE.md): ni la key BYOK descifrada ni el prompt original
    de un flujo masked-confirmado deben aparecer en ningún log."""
    model_id = await _get_model_id("claude-haiku-4-5")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="byok")
    plaintext_key = "sk-ant-super-secret-key-999"
    await _set_provider_connection(
        gateway_tenant["tenant_id"],
        "anthropic",
        plaintext_key,
        created_by=gateway_tenant["user_id"],
    )
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    _install_fake_acompletion(monkeypatch, [_content_chunk("ok"), _usage_chunk(4, 2)])

    original_prompt = "Soy Juan Perez, mi email es juan.perez@example.com."
    with caplog.at_level(logging.DEBUG):
        chat = await prepare_stream(
            tenant_id=gateway_tenant["tenant_id"],
            user_id=gateway_tenant["user_id"],
            role="owner",
            division_id=gateway_tenant["division_id"],
            model_id=str(model_id),
            prompt=original_prompt,
            conversation_id=None,
            confirm_masked=True,
            requested_max_tokens=100,
        )
        await _drain(chat)

    log_text = caplog.text
    assert plaintext_key not in log_text
    assert "Juan Perez" not in log_text
    assert "juan.perez@example.com" not in log_text
