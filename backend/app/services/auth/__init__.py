"""auth (adapter): verifica el JWT de Supabase Auth y resuelve users/
memberships propios — mapping con auth.users por supabase_user_id (D5,
docs/ARQUITECTURA.md). No custodiamos credenciales (ver migración 002):
Supabase Auth es el único mecanismo de login.
"""

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from app.config import settings
from app.db import async_session, tenant_session

_JWT_ALGORITHM = "HS256"
_JWT_AUDIENCE = "authenticated"


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """Lo que dice el JWT de Supabase, antes de mirar si existe un `users`
    local para este tenant — válido para /api/auth/register-tenant, donde
    todavía no existe esa fila."""

    supabase_user_id: str
    email: str | None


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="falta el token de autenticación")
    return header[len("bearer ") :].strip()


def verify_jwt(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[_JWT_ALGORITHM],
            audience=_JWT_AUDIENCE,
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="token inválido o caducado") from e


async def get_current_identity(request: Request) -> AuthenticatedIdentity:
    payload = verify_jwt(_extract_bearer_token(request))
    return AuthenticatedIdentity(supabase_user_id=payload["sub"], email=payload.get("email"))


@dataclass(frozen=True)
class CurrentUser:
    """Identidad de Supabase YA resuelta contra users/memberships del
    tenant actual (request.state.tenant, puesto por TenantResolver)."""

    id: str
    tenant_id: str
    email: str
    name: str
    role: str
    division_id: str


async def get_current_user(request: Request) -> CurrentUser:
    identity = await get_current_identity(request)
    tenant = request.state.tenant

    async with tenant_session(tenant.id) as session:
        result = await session.execute(
            text("""
                SELECT u.id, u.email, u.name, m.role, m.division_id
                FROM users u
                JOIN memberships m ON m.user_id = u.id
                WHERE u.supabase_user_id = :supabase_user_id
                LIMIT 1
            """),
            {"supabase_user_id": identity.supabase_user_id},
        )
        row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=403, detail="usuario sin acceso a este tenant")

    return CurrentUser(
        id=str(row["id"]),
        tenant_id=tenant.id,
        email=row["email"],
        name=row["name"],
        role=row["role"],
        division_id=str(row["division_id"]),
    )


def require_role(*allowed_roles: str):
    """Middleware de roles (S1-5): `Depends(require_role("owner", "admin"))`."""

    async def _check(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="rol insuficiente")
        return current_user

    return _check


@dataclass(frozen=True)
class TenantRegistration:
    tenant_id: str
    user_id: str
    division_id: str


async def register_tenant(
    *,
    slug: str,
    tenant_name: str,
    billing_mode: str,
    owner_name: str,
    identity: AuthenticatedIdentity,
) -> TenantRegistration:
    """tenant + owner + división default, en UNA transacción (S1-5).
    No usa tenant_session() tal cual porque el tenant_id no existe todavía
    al empezar — se fija app.tenant_id EN CUANTO se conoce, dentro de la
    misma transacción, antes de tocar ninguna tabla tenant-scoped."""
    async with async_session() as session, session.begin():
        tenant_id = (
            await session.execute(
                text(
                    "INSERT INTO tenants (slug, name, billing_mode) "
                    "VALUES (:slug, :name, :billing_mode) RETURNING id"
                ),
                {"slug": slug, "name": tenant_name, "billing_mode": billing_mode},
            )
        ).scalar_one()

        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

        division_id = (
            await session.execute(
                text(
                    "INSERT INTO divisions (tenant_id, name, is_default) "
                    "VALUES (:tenant_id, 'Default', true) RETURNING id"
                ),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()

        user_id = (
            await session.execute(
                text("""
                    INSERT INTO users (tenant_id, email, name, supabase_user_id)
                    VALUES (:tenant_id, :email, :name, :supabase_user_id)
                    RETURNING id
                """),
                {
                    "tenant_id": tenant_id,
                    "email": identity.email,
                    "name": owner_name,
                    "supabase_user_id": identity.supabase_user_id,
                },
            )
        ).scalar_one()

        await session.execute(
            text("""
                INSERT INTO memberships (user_id, division_id, tenant_id, role)
                VALUES (:user_id, :division_id, :tenant_id, 'owner')
            """),
            {"user_id": user_id, "division_id": division_id, "tenant_id": tenant_id},
        )

        await session.execute(
            text("""
                INSERT INTO audit_events (tenant_id, actor_user_id, actor_role, event_type)
                VALUES (:tenant_id, :actor_user_id, 'owner', 'tenant_registered')
            """),
            {"tenant_id": tenant_id, "actor_user_id": user_id},
        )

    return TenantRegistration(
        tenant_id=str(tenant_id), user_id=str(user_id), division_id=str(division_id)
    )


async def record_login(current_user: CurrentUser) -> None:
    async with tenant_session(current_user.tenant_id) as session:
        await session.execute(
            text("""
                INSERT INTO audit_events (tenant_id, actor_user_id, actor_role, event_type)
                VALUES (:tenant_id, :actor_user_id, :actor_role, 'login')
            """),
            {
                "tenant_id": current_user.tenant_id,
                "actor_user_id": current_user.id,
                "actor_role": current_user.role,
            },
        )
