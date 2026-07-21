import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.services import dlp, gateway, ledger
from app.services.auth import (
    AuthenticatedIdentity,
    CurrentUser,
    get_current_identity,
    get_current_user,
    record_login,
    register_tenant,
    require_role,
)
from app.services.onboarding import (
    complete_onboarding,
    enable_models,
    invite_team,
    list_enabled_models,
    list_models_catalog,
    set_dlp_preset,
    validate_and_store_key,
)
from app.services.policy import PolicyDeniedError, get_chat_context, list_visible_models
from app.services.policy import check as policy_check
from app.services.tenant_resolver import (
    TenantNotFoundError,
    TenantSuspendedError,
    resolve_tenant,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Carga spaCy es_core_news_md (1-3s, SP-2 docs/spike.md) — irrelevante
    # una vez en el arranque, inaceptable si se hiciera por request.
    dlp.init_engine()
    # Reconciliación de holds huérfanos (S1-11): un proceso recién
    # arrancado no puede tener streams propios en vuelo, así que cualquier
    # reserved_amount > 0 es basura de una instancia anterior que murió a
    # mitad de un hold. Asume instancia única — ver docstring en
    # app/services/ledger/__init__.py y la nota junto a D4 en
    # docs/ARQUITECTURA.md.
    await ledger.reset_orphaned_holds()
    yield


app = FastAPI(title="AIhub API", lifespan=lifespan)

# El frontend llama al backend cross-origin (mismo host, puerto distinto —
# ver frontend/lib/api.ts): sin esto el navegador bloquea la petición real
# tras un preflight OPTIONS en 405 y el fetch() nunca llega al backend.
# Cualquier subdominio de BASE_DOMAIN (tenant o sin tenant, cualquier puerto).
_dev_origin_regex = rf"^https?://([a-z0-9-]+\.)?{re.escape(settings.base_domain)}(:\d+)?$"
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_dev_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# --- Onboarding wizard (S1-6) — solo el owner que dio de alta el tenant ---


class ValidateKeyRequest(BaseModel):
    provider_slug: str
    api_key: str


@app.post("/api/onboarding/validate-key")
async def validate_key_endpoint(
    payload: ValidateKeyRequest,
    current_user: CurrentUser = Depends(require_role("owner")),
):
    """Llamada de test real al proveedor; la key se cifra SIEMPRE antes de
    guardarse (regla 3, CLAUDE.md) — nunca se devuelve ni se loguea entera."""
    result = await validate_and_store_key(
        tenant_id=current_user.tenant_id,
        provider_slug=payload.provider_slug,
        api_key=payload.api_key,
        created_by=current_user.id,
    )
    return {"status": result.status, "key_last4": result.key_last4}


class EnableModelsRequest(BaseModel):
    model_ids: list[str]


@app.post("/api/onboarding/enable-models")
async def enable_models_endpoint(
    payload: EnableModelsRequest,
    current_user: CurrentUser = Depends(require_role("owner")),
):
    await enable_models(tenant_id=current_user.tenant_id, model_ids=payload.model_ids)
    return {"enabled": payload.model_ids}


class DlpPresetRequest(BaseModel):
    preset: str  # strict|balanced|warn_only


@app.post("/api/onboarding/dlp-preset")
async def dlp_preset_endpoint(
    payload: DlpPresetRequest,
    current_user: CurrentUser = Depends(require_role("owner")),
):
    mode = await set_dlp_preset(tenant_id=current_user.tenant_id, preset=payload.preset)
    return {"mode": mode}


@app.post("/api/onboarding/complete")
async def complete_onboarding_endpoint(
    current_user: CurrentUser = Depends(require_role("owner")),
):
    await complete_onboarding(
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        actor_role=current_user.role,
    )
    return {"completed": True}


@app.get("/api/onboarding/models-catalog")
async def models_catalog_endpoint(current_user: CurrentUser = Depends(require_role("owner"))):
    """Catálogo [global], para el paso "modelos" (bifurcación reseller)."""
    return {"models": await list_models_catalog()}


class InviteTeamRequest(BaseModel):
    emails: list[str]


@app.post("/api/onboarding/invite-team")
async def invite_team_endpoint(
    payload: InviteTeamRequest,
    current_user: CurrentUser = Depends(require_role("owner")),
):
    """Persiste las invitaciones (división default, rol user) — el envío
    real del email es S2-7, aquí no se manda nada todavía."""
    count = await invite_team(
        tenant_id=current_user.tenant_id,
        emails=payload.emails,
        role="user",
        created_by=current_user.id,
    )
    return {"invited": count}


@app.get("/api/models/enabled")
async def enabled_models_endpoint(current_user: CurrentUser = Depends(get_current_user)):
    """Cualquier miembro autenticado (no solo el owner) — landing de /chat."""
    return {"models": await list_enabled_models(tenant_id=current_user.tenant_id)}


# --- PolicyService (S1-8) ---


class PolicyCheckRequest(BaseModel):
    model_id: str


@app.post("/api/policy/check")
async def policy_check_endpoint(
    payload: PolicyCheckRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Endpoint mínimo para demostrar/probar PolicyService de punta a
    punta — GatewayService (S1-10) es quien lo llama de verdad dentro del
    pipeline de /api/chat/stream."""
    try:
        result = await policy_check(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            role=current_user.role,
            division_id=current_user.division_id,
            model_id=payload.model_id,
        )
    except PolicyDeniedError as e:
        return JSONResponse(status_code=403, content={"reason": e.reason, "detail": e.detail})
    return {"allowed": True, "model_source": result.model_source}


# --- DLPService (S1-9) ---


class DlpAnalyzeRequest(BaseModel):
    prompt: str


@app.post("/api/dlp/analyze")
async def dlp_analyze_endpoint(
    payload: DlpAnalyzeRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Endpoint mínimo para demostrar/probar DLPService de punta a punta —
    GatewayService (S1-10) es quien lo llama de verdad dentro del pipeline
    de /api/chat/stream (409 masked / 422 blocked)."""
    result = await dlp.analyze(
        tenant_id=current_user.tenant_id,
        division_id=current_user.division_id,
        prompt=payload.prompt,
    )
    return {
        "verdict": result.verdict,
        "masked_text": result.masked_text,
        "entities_summary": result.entities_summary,
    }


# --- GatewayService (S1-10) ---


class ChatStreamRequest(BaseModel):
    model_id: str
    message: str
    conversation_id: str | None = None  # None -> se crea una conversación nueva
    confirm_masked: bool = False
    max_tokens: int = 1024  # acotado a gateway.MAX_TOKENS_CEILING


@app.post("/api/chat/stream")
async def chat_stream_endpoint(
    payload: ChatStreamRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Pipeline completo de docs/ARQUITECTURA.md: policy → DLP → hold →
    litellm streaming → transacción final. `prepare_stream` resuelve las
    validaciones previas de forma síncrona (no generador) para poder
    devolver 403/409/422 reales; solo se entra al streaming (200 + SSE) si
    las cuatro pasan."""
    try:
        prepared = await gateway.prepare_stream(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            role=current_user.role,
            division_id=current_user.division_id,
            model_id=payload.model_id,
            prompt=payload.message,
            conversation_id=payload.conversation_id,
            confirm_masked=payload.confirm_masked,
            requested_max_tokens=payload.max_tokens,
        )
    except PolicyDeniedError as e:
        return JSONResponse(status_code=403, content={"reason": e.reason, "detail": e.detail})
    except gateway.DLPBlockedError as e:
        return JSONResponse(
            status_code=422, content={"verdict": "blocked", "entities_summary": e.entities_summary}
        )
    except gateway.DLPMaskedError as e:
        return JSONResponse(
            status_code=409,
            content={
                "verdict": "masked",
                "masked_text": e.masked_text,
                "entities_summary": e.entities_summary,
            },
        )

    return StreamingResponse(gateway.stream_chat(prepared), media_type="text/event-stream")


# --- Soporte de Chat UI (S1-12) ---


@app.get("/api/me")
async def me_endpoint(current_user: CurrentUser = Depends(get_current_user)):
    """Lectura pura de la identidad ya resuelta — a diferencia de
    `/api/auth/login`, NO escribe `audit_event` (no es un login real,
    solo hidratar `/chat` en cada carga; resuelve el TODO dejado en
    `frontend/app/login/page.tsx`)."""
    return {
        "user_id": current_user.id,
        "tenant_id": current_user.tenant_id,
        "email": current_user.email,
        "name": current_user.name,
        "role": current_user.role,
        "division_id": current_user.division_id,
    }


@app.get("/api/chat/models")
async def chat_models_endpoint(current_user: CurrentUser = Depends(get_current_user)):
    """Modelos habilitados para el tenant, agrupables por proveedor, con
    precio y `allowed` por rol — ModelSelector del chat (S1-12)."""
    models = await list_visible_models(tenant_id=current_user.tenant_id, role=current_user.role)
    return {"models": [asdict(m) for m in models]}


@app.get("/api/chat/context")
async def chat_context_endpoint(
    request: Request, current_user: CurrentUser = Depends(get_current_user)
):
    """División, saldo/presupuesto y modo DLP activo — ContextBar del
    chat (S1-12). `billing_mode` sale de `request.state.tenant` (ya
    resuelto por TenantResolver), sin query aparte."""
    tenant = request.state.tenant
    context = await get_chat_context(
        tenant_id=current_user.tenant_id,
        division_id=current_user.division_id,
        billing_mode=tenant.billing_mode,
    )
    return {
        "division_name": context.division_name,
        "billing_mode": context.billing_mode,
        "dlp_mode": context.dlp_mode,
        "wallet_available": str(context.wallet_available)
        if context.wallet_available is not None
        else None,
        "division_allocated": str(context.division_allocated)
        if context.division_allocated is not None
        else None,
        "division_consumed": str(context.division_consumed)
        if context.division_consumed is not None
        else None,
    }
