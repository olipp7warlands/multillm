"""S1-4 · TenantResolver + sesión RLS.

Los tenants de prueba son fijos (slug estable, reutilizado y actualizado
con UPDATE en cada run) en vez de crearse nuevos cada vez: como los
tenants no se borran físicamente nunca (docs/MODELO_DATOS.md), crear uno
nuevo por ejecución dejaría filas permanentes acumulándose sin límite en
`tenants`. Reutilizar una fila fija y forzar su `status` con UPDATE antes
de cada test es idempotente y no depende del orden de ejecución.
"""

import os
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.tenant_resolver import invalidate_tenant_cache

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

TEST_SLUG = "test-resolver-fixed"


def _app_backend_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def _set_test_tenant_status(status: str) -> None:
    conn = await asyncpg.connect(_app_backend_dsn(), statement_cache_size=0, ssl="require")
    try:
        tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE slug = $1", TEST_SLUG)
        if tenant_id is None:
            tenant_id = await conn.fetchval(
                "INSERT INTO tenants (slug, name, billing_mode, status) "
                "VALUES ($1, $1, 'reseller', $2) RETURNING id",
                TEST_SLUG,
                status,
            )
        else:
            await conn.execute("UPDATE tenants SET status = $1 WHERE id = $2", status, tenant_id)

        # idempotente: tenant_branding tiene RLS, hace falta el SET LOCAL antes
        # de tocarla (INSERT también se comprueba contra la política, no solo
        # SELECT) — auto-reparable si alguna vez quedó sin fila de branding.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            has_branding = await conn.fetchval(
                "SELECT 1 FROM tenant_branding WHERE tenant_id = $1", tenant_id
            )
            if not has_branding:
                await conn.execute(
                    "INSERT INTO tenant_branding (tenant_id, product_name) "
                    "VALUES ($1, 'Test Resolver')",
                    tenant_id,
                )
    finally:
        await conn.close()
    # el UPDATE es de otra conexión: la caché en memoria del proceso de
    # TenantResolver no se entera sola, hay que invalidarla explícitamente
    # (por tenant_id, no por slug/host — ver docstring de la función).
    invalidate_tenant_cache(str(tenant_id))


async def _get(path: str, host: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers={"host": host})


async def test_active_tenant_resolves_ok():
    await _set_test_tenant_status("active")
    response = await _get("/api/whoami", f"{TEST_SLUG}.lvh.me:3000")
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_slug"] == TEST_SLUG
    assert body["status"] == "active"
    assert body["product_name"] == "Test Resolver"


async def test_unknown_subdomain_is_404():
    response = await _get("/api/whoami", "no-existe-este-tenant-xyz.lvh.me:3000")
    assert response.status_code == 404


async def test_suspended_tenant_is_403():
    await _set_test_tenant_status("suspended")
    try:
        response = await _get("/api/whoami", f"{TEST_SLUG}.lvh.me:3000")
        assert response.status_code == 403
    finally:
        # deja el fixture en un estado neutro para la próxima ejecución
        await _set_test_tenant_status("active")


async def test_health_bypasses_tenant_resolution():
    # /health responde aunque el host no corresponda a ningún tenant
    health_response = await _get("/health", "no-existe-y-no-deberia-importar.lvh.me:3000")
    assert health_response.status_code == 200

    # mientras que un endpoint de negocio con el mismo host sí exige tenant
    whoami_response = await _get("/api/whoami", "no-existe-y-no-deberia-importar.lvh.me:3000")
    assert whoami_response.status_code == 404


@pytest.fixture(autouse=True, scope="module")
def _cleanup_cache():
    yield
    invalidate_tenant_cache()
