"""S1-11 · LedgerService (endurecimiento): carrera real sobre el mismo
wallet, integridad reutilizable, orden de balance_after bajo concurrencia,
y reconciliación de holds huérfanos al arranque."""

import asyncio
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import jwt
import litellm
import pytest
from dotenv import load_dotenv

from app.db import async_session
from app.services import dlp, gateway, ledger, metering
from app.services.auth import AuthenticatedIdentity, register_tenant
from app.services.gateway import prepare_stream, stream_chat
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
        tenant_name="Ledger Test Co",
        billing_mode="reseller",
        owner_name="Ledger Owner",
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
    return await _register_tenant("ledger-test")


async def _get_model_id(slug: str) -> uuid.UUID:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        return await conn.fetchval("SELECT id FROM models WHERE slug = $1", slug)
    finally:
        await conn.close()


async def _set_tenant_model_access(tenant_id: str, model_id, *, source: str) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                INSERT INTO tenant_model_access (tenant_id, model_id, enabled, source)
                VALUES ($1, $2, true, $3)
                ON CONFLICT (tenant_id, model_id) DO UPDATE SET enabled = true, source = $3
                """,
                uuid.UUID(tenant_id),
                model_id,
                source,
            )
    finally:
        await conn.close()


async def _set_wallet_balance(tenant_id: str, balance: Decimal) -> None:
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


async def _seed_wallet_with_ledger(tenant_id: str, balance: Decimal) -> None:
    """A diferencia de `_set_wallet_balance`, también deja un apunte
    'topup' en `ledger_entries` por el mismo importe — así el saldo
    sembrado para el test de carrera queda respaldado en el ledger y
    `check_integrity()` puede verificar honestamente que la carrera
    concurrente no introdujo NINGUNA divergencia (no solo que no empeoró
    una que el propio arranque del test ya habría causado)."""
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            wallet_id = await conn.fetchval(
                """
                INSERT INTO wallets (tenant_id, balance_cached) VALUES ($1, $2)
                ON CONFLICT (tenant_id) DO UPDATE SET balance_cached = $2, reserved_amount = 0
                RETURNING id
                """,
                uuid.UUID(tenant_id),
                balance,
            )
            await conn.execute(
                """
                INSERT INTO ledger_entries
                    (tenant_id, wallet_id, type, credits_delta, balance_after)
                VALUES ($1, $2, 'topup', $3, $3)
                """,
                uuid.UUID(tenant_id),
                wallet_id,
                balance,
            )
    finally:
        await conn.close()


async def _set_wallet_reserved(tenant_id: str, reserved: Decimal) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                "UPDATE wallets SET reserved_amount = $2 WHERE tenant_id = $1",
                uuid.UUID(tenant_id),
                reserved,
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


async def _read_wallet(tenant_id: str) -> dict:
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


async def _fetch_ledger_entries_ordered(tenant_id: str) -> list[dict]:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            rows = await conn.fetch(
                """
                SELECT credits_delta, balance_after FROM ledger_entries
                WHERE tenant_id = $1 AND type = 'consumption' ORDER BY ts, id
                """,
                uuid.UUID(tenant_id),
            )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _content_chunk(text_piece: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text_piece))], usage=None
    )


def _usage_chunk(tokens_in: int, tokens_out: int):
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out)
    )


def _install_fake_acompletion(monkeypatch, chunks):
    async def _gen():
        for chunk in chunks:
            yield chunk

    async def fake_acompletion(**kwargs):
        return _gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)


async def _drain(chat) -> list[dict]:
    events = []
    async for raw in stream_chat(chat):
        line = raw.decode().strip()
        events.append(json.loads(line[len("data: ") :]))
    return events


_RACE_PROMPT = "Hola, ¿qué tal el tiempo hoy?"
_RACE_MAX_TOKENS = 100
_RACE_TOKENS_IN = 10
_RACE_TOKENS_OUT = 5


async def _run_concurrent_race(gateway_tenant, monkeypatch, *, n: int, k: int) -> dict:
    """N peticiones concurrentes contra el mismo wallet, con saldo para
    exactamente K (determinista: saldo = K * hold_estimado, calculado con
    la misma `gateway._estimate_hold` que usará `prepare_stream()` de
    verdad)."""
    monkeypatch.setattr(gateway_settings, "openai_api_key", "sk-test-master")
    model_id = await _get_model_id("gpt-4o-mini")
    await _set_tenant_model_access(gateway_tenant["tenant_id"], model_id, source="reseller")
    await _set_dlp_mode(gateway_tenant["tenant_id"], "mask")

    async with async_session() as session:
        rates = await metering.current_rates(session, model_id=str(model_id))
    hold_amount = gateway._estimate_hold(_RACE_PROMPT, rates, _RACE_MAX_TOKENS)
    initial_balance = hold_amount * k
    await _seed_wallet_with_ledger(gateway_tenant["tenant_id"], initial_balance)

    _install_fake_acompletion(
        monkeypatch, [_content_chunk("ok"), _usage_chunk(_RACE_TOKENS_IN, _RACE_TOKENS_OUT)]
    )

    # Dos oleadas deliberadas, no una sola carrera de punta a punta: el
    # hold es conservador por diseño (cubre max_tokens de salida) y el
    # cobro real es mucho menor, así que un flujo que TERMINA libera más
    # margen del que ocupó — un intento tardío podría colarse aprovechando
    # ese margen liberado. Eso es correcto (el hold nunca debe reservar de
    # más tiempo del necesario), pero hace que "cuántos se admiten" deje
    # de ser determinista si se mezcla con el resto del flujo. La carrera
    # que de verdad hay que probar es la de RESERVAR el hold — así que la
    # oleada 1 dispara las N reservas concurrentes contra el MISMO saldo
    # inicial (nadie ha liberado nada todavía) y la oleada 2, ya sin
    # contención, completa los flujos admitidos.
    async def _try_prepare():
        try:
            chat = await prepare_stream(
                tenant_id=gateway_tenant["tenant_id"],
                user_id=gateway_tenant["user_id"],
                role="owner",
                division_id=gateway_tenant["division_id"],
                model_id=str(model_id),
                prompt=_RACE_PROMPT,
                conversation_id=None,
                confirm_masked=False,
                requested_max_tokens=_RACE_MAX_TOKENS,
            )
        except PolicyDeniedError as e:
            return ("denied", e.reason)
        return ("prepared", chat)

    prepare_results = await asyncio.gather(*[_try_prepare() for _ in range(n)])
    prepared = [chat for status, chat in prepare_results if status == "prepared"]
    denied = [reason for status, reason in prepare_results if status == "denied"]

    async def _finish(chat):
        events = await _drain(chat)
        return events[-1]

    done_events = await asyncio.gather(*[_finish(chat) for chat in prepared])

    return {
        "tenant_id": gateway_tenant["tenant_id"],
        "initial_balance": initial_balance,
        "hold_amount": hold_amount,
        "done_events": done_events,
        "denied": denied,
    }


async def test_concurrent_holds_never_overdraft_wallet(gateway_tenant, monkeypatch):
    race = await _run_concurrent_race(gateway_tenant, monkeypatch, n=10, k=4)
    done_events = race["done_events"]
    denied = race["denied"]

    assert len(done_events) == 4
    assert len(denied) == 6
    assert all(reason == "no_balance" for reason in denied)

    credits_per_request = Decimal(done_events[0]["credits_charged"])
    assert all(Decimal(evt["credits_charged"]) == credits_per_request for evt in done_events)
    assert credits_per_request <= race["hold_amount"]  # el hold siempre cubrió el cobro real

    wallet = await _read_wallet(race["tenant_id"])
    assert wallet["balance_cached"] == race["initial_balance"] - credits_per_request * 4
    assert wallet["reserved_amount"] == 0  # ningún hold quedó huérfano

    divergences = await ledger.check_integrity(tenant_id=race["tenant_id"])
    assert divergences == []


async def test_ledger_entries_balance_after_monotonic_under_concurrency(
    gateway_tenant, monkeypatch
):
    race = await _run_concurrent_race(gateway_tenant, monkeypatch, n=10, k=4)
    rows = await _fetch_ledger_entries_ordered(race["tenant_id"])
    assert len(rows) == 4

    running_balance = race["initial_balance"]
    for row in rows:
        running_balance += row["credits_delta"]
        assert row["balance_after"] == running_balance


async def test_check_integrity_detects_divergence(gateway_tenant):
    # balance sin respaldo en el ledger (p.ej. un topup mal registrado) —
    # exactamente lo que este chequeo debe atrapar.
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("42"))
    divergences = await ledger.check_integrity(tenant_id=gateway_tenant["tenant_id"])
    assert len(divergences) == 1
    assert divergences[0].difference == Decimal("42")


async def test_check_integrity_clean_wallet_no_divergence(gateway_tenant):
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("0"))
    divergences = await ledger.check_integrity(tenant_id=gateway_tenant["tenant_id"])
    assert divergences == []


async def test_reset_orphaned_holds_clears_reserved_amount_without_touching_balance(
    gateway_tenant,
):
    await _set_wallet_balance(gateway_tenant["tenant_id"], Decimal("100"))
    await _set_wallet_reserved(gateway_tenant["tenant_id"], Decimal("30"))

    await ledger.reset_orphaned_holds()

    wallet = await _read_wallet(gateway_tenant["tenant_id"])
    assert wallet["reserved_amount"] == 0
    assert wallet["balance_cached"] == Decimal("100")
