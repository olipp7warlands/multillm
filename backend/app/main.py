from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import engine
from app.services.tenant_resolver import (
    TenantNotFoundError,
    TenantSuspendedError,
    resolve_tenant,
)

app = FastAPI(title="AIhub API")

# Endpoints que no dependen de un tenant (infra, no negocio) — se sirven sin
# pasar por TenantResolver.
_PUBLIC_PATHS = {"/health"}


@app.middleware("http")
async def tenant_resolver_middleware(request: Request, call_next):
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    host = request.headers.get("host", "")
    try:
        tenant = await resolve_tenant(host)
    except TenantNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "tenant no encontrado"})
    except TenantSuspendedError:
        return JSONResponse(status_code=403, content={"detail": "tenant suspendido"})

    request.state.tenant = tenant
    return await call_next(request)


@app.get("/health")
async def health():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "supabase": "connected"}


@app.get("/api/whoami")
async def whoami(request: Request):
    """Endpoint mínimo para demostrar/probar TenantResolver de punta a
    punta — no es una feature de negocio."""
    tenant = request.state.tenant
    return {
        "tenant_slug": tenant.slug,
        "status": tenant.status,
        "product_name": tenant.product_name,
    }
