"""S1-11 · MeteringService: fallo cerrado de current_rates() con un error
claro y localizable, nunca datos parciales ni NULL silencioso."""

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv

from app.db import async_session
from app.services import metering
from app.services.metering import MissingExchangeRateError

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def _admin_dsn() -> str:
    return os.environ["DATABASE_URL_ADMIN"].replace("postgresql+asyncpg://", "postgresql://")


async def _insert_model_without_rates(provider_slug: str = "openai") -> str:
    """Modelo temporal SIN ninguna fila de exchange_rates — sin tocar el
    catálogo semilla que comparten el resto de los tests. `app_backend`
    solo tiene SELECT sobre `models`/`providers` (ver GRANTs de la
    migración 001), así que este INSERT va con DATABASE_URL_ADMIN, igual
    que test_immutability.py / test_tenant_hard_delete.py."""
    conn = await asyncpg.connect(_admin_dsn(), statement_cache_size=0, ssl="require")
    try:
        provider_id = await conn.fetchval("SELECT id FROM providers WHERE slug = $1", provider_slug)
        model_id = await conn.fetchval(
            """
            INSERT INTO models
                (provider_id, slug, display_name, modality, status, litellm_model_name)
            VALUES ($1, $2, 'Sin Tarifa (test S1-11)', 'text', 'active', 'test/no-rate-model')
            RETURNING id
            """,
            provider_id,
            f"no-rate-{uuid.uuid4().hex[:8]}",
        )
        return str(model_id)
    finally:
        await conn.close()


async def _delete_model(model_id: str) -> None:
    conn = await asyncpg.connect(_admin_dsn(), statement_cache_size=0, ssl="require")
    try:
        await conn.execute("DELETE FROM models WHERE id = $1", uuid.UUID(model_id))
    finally:
        await conn.close()


async def test_current_rates_fails_closed_when_no_rate_exists():
    model_id = await _insert_model_without_rates()
    try:
        async with async_session() as session:
            with pytest.raises(MissingExchangeRateError) as exc_info:
                await metering.current_rates(session, model_id=model_id)
        assert exc_info.value.model_id == model_id
    finally:
        await _delete_model(model_id)


async def test_current_rates_returns_both_units_for_seeded_model():
    conn = await asyncpg.connect(_admin_dsn(), statement_cache_size=0, ssl="require")
    try:
        model_id = await conn.fetchval("SELECT id FROM models WHERE slug = $1", "gpt-4o-mini")
    finally:
        await conn.close()

    async with async_session() as session:
        rates = await metering.current_rates(session, model_id=str(model_id))
    assert rates.in_credit_price > 0
    assert rates.out_credit_price > 0
    assert rates.in_rate_id != rates.out_rate_id
