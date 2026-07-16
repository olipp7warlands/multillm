from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.db import engine
from app.services.auth import (
    AuthenticatedIdentity,
    CurrentUser,
    get_current_identity,
    get_current_user,
    record_login,
    register_tenant,
    require_role,
)
from app.services.tenant_resolver import (
    TenantNotFoundError,
    TenantSuspendedError,
    resolve_tenant,
)

app = FastAPI(title="AIhub API")

# Endpoints que no dependen de un tenant (infra, o el propio alta de tenant
# nuevo — por definición no hay tenant que resolver todavía) — se sirven sin
# pasar por TenantResolver.
_PUBLIC_PATHS = {"/health", "/api/auth/register-tenant"}


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


class RegisterTenantRequest(BaseModel):
    slug: str
    tenant_name: str
    billing_mode: str
    owner_name: str


@app.post("/api/auth/register-tenant")
async def register_tenant_endpoint(
    payload: RegisterTenantRequest,
    identity: AuthenticatedIdentity = Depends(get_current_identity),
):
    """Alta de tenant nuevo: crea tenant + owner + división default en una
    transacción (S1-5). El usuario ya se registró en Supabase Auth en el
    frontend; aquí solo se verifica su JWT y se crea el resto."""
    result = await register_tenant(
        slug=payload.slug,
        tenant_name=payload.tenant_name,
        billing_mode=payload.billing_mode,
        owner_name=payload.owner_name,
        identity=identity,
    )
    return {
        "tenant_id": result.tenant_id,
        "user_id": result.user_id,
        "division_id": result.division_id,
    }


@app.post("/api/auth/login")
async def login_endpoint(current_user: CurrentUser = Depends(get_current_user)):
    """El frontend la llama justo después de que Supabase Auth confirme el
    login, para dejar el audit_event y devolver el rol/división resueltos."""
    await record_login(current_user)
    return {
        "user_id": current_user.id,
        "tenant_id": current_user.tenant_id,
        "email": current_user.email,
        "name": current_user.name,
        "role": current_user.role,
        "division_id": current_user.division_id,
    }


@app.get("/api/admin/ping")
async def admin_ping(current_user: CurrentUser = Depends(require_role("owner", "admin"))):
    """Endpoint mínimo para demostrar/probar el middleware de roles."""
    return {"ok": True, "role": current_user.role}
