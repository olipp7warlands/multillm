"""PolicyService (S1-8): paso 2 del pipeline de docs/ARQUITECTURA.md, antes
de DLP y del proveedor — visibilidad de modelo (tenant_model_access +
min_role), saldo (wallet + allocation de división del periodo, solo en
modo reseller) y rate limit (contadores Postgres por ventana). Denegar
aquí es más barato que denegar después de un escaneo DLP.

`list_visible_models`/`get_chat_context` (S1-12): mismos datos de
visibilidad/saldo que `check()`, pero para hidratar la UI del chat
(`ModelSelector`/`ContextBar`) en vez de para autorizar una petición.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import tenant_session
from app.services import dlp

_ROLE_RANK = {"user": 0, "division_admin": 1, "admin": 2, "owner": 3}


def is_role_sufficient(role: str, min_role: str | None) -> bool:
    """Única fuente de verdad para el ranking de roles — usada por
    `check()` y por `list_visible_models` (S1-12), para no duplicar
    `_ROLE_RANK` en otro sitio."""
    if min_role is None:
        return True
    return _ROLE_RANK[role] >= _ROLE_RANK[min_role]


# Límites de rate limit por ventana fija de 1 minuto. El backlog pide el
# mecanismo ("contadores Postgres por ventana, usuario y tenant"), no cifras
# concretas — no hay todavía un plan/tarifa por tenant documentado en docs/,
# así que estos son valores provisionales conservadores. Mover a config por
# tenant cuando exista esa decisión de producto (no antes: YAGNI).
RATE_LIMIT_USER_PER_MINUTE = 60
RATE_LIMIT_TENANT_PER_MINUTE = 600


class PolicyDeniedError(Exception):
    """Denegación de PolicyService: `reason` es uno de model_not_enabled |
    no_balance | rate_limited (los tres motivos de docs/ARQUITECTURA.md).
    El audit_event ya se escribió antes de que esto se propague."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class PolicyCheckResult:
    model_source: str  # reseller|byok — GatewayService (S1-10) decide la credencial con esto


async def _audit_denial(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: str,
    reason: str,
    model_id: str,
) -> None:
    await session.execute(
        text("""
            INSERT INTO audit_events
                (tenant_id, actor_user_id, actor_role, event_type, subject)
            VALUES (
                :tenant_id, :actor_user_id, :actor_role, 'policy_denied',
                jsonb_build_object(
                    'reason', CAST(:reason AS text), 'model_id', CAST(:model_id AS text)
                )
            )
        """),
        {
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "actor_role": actor_role,
            "reason": reason,
            "model_id": model_id,
        },
    )


async def _bump_rate_limit(
    session: AsyncSession,
    *,
    tenant_id: str,
    scope: str,
    scope_id: str,
    window_start: datetime,
) -> int:
    """Incremento atómico (INSERT ... ON CONFLICT DO UPDATE, sin necesidad
    de SELECT ... FOR UPDATE) — devuelve el contador YA incluyendo esta
    petición, así que la que hace saltar el límite es la primera rechazada."""
    result = await session.execute(
        text("""
            INSERT INTO rate_limit_counters
                (tenant_id, scope, scope_id, window_start, request_count)
            VALUES (:tenant_id, :scope, :scope_id, :window_start, 1)
            ON CONFLICT (tenant_id, scope, scope_id, window_start)
            DO UPDATE SET request_count = rate_limit_counters.request_count + 1
            RETURNING request_count
        """),
        {
            "tenant_id": tenant_id,
            "scope": scope,
            "scope_id": scope_id,
            "window_start": window_start,
        },
    )
    return result.scalar_one()


async def check(
    *,
    tenant_id: str,
    user_id: str,
    role: str,
    division_id: str,
    model_id: str,
) -> PolicyCheckResult:
    now = datetime.now(UTC)
    window_start = now.replace(second=0, microsecond=0)

    async with tenant_session(tenant_id) as session:
        access = (
            (
                await session.execute(
                    text("""
                    SELECT enabled, min_role, source FROM tenant_model_access
                    WHERE tenant_id = :tenant_id AND model_id = :model_id
                """),
                    {"tenant_id": tenant_id, "model_id": model_id},
                )
            )
            .mappings()
            .first()
        )

        if access is None or not access["enabled"]:
            await _audit_denial(
                session,
                tenant_id=tenant_id,
                actor_user_id=user_id,
                actor_role=role,
                reason="model_not_enabled",
                model_id=model_id,
            )
            raise PolicyDeniedError("model_not_enabled", "modelo no habilitado para este tenant")

        if not is_role_sufficient(role, access["min_role"]):
            await _audit_denial(
                session,
                tenant_id=tenant_id,
                actor_user_id=user_id,
                actor_role=role,
                reason="model_not_enabled",
                model_id=model_id,
            )
            raise PolicyDeniedError("model_not_enabled", "modelo no habilitado para tu rol")

        if access["source"] == "reseller":
            wallet = (
                (
                    await session.execute(
                        text("""
                            SELECT balance_cached, reserved_amount FROM wallets
                            WHERE tenant_id = :tenant_id
                        """),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .first()
            )
            # Sin fila de wallet: 0 disponible (correcto — sin topup no hay
            # crédito, no un error). Nada crea la fila todavía en S1-5/S1-6;
            # el flujo de topup de S2-2 deberá crearla si no existe.
            available = (wallet["balance_cached"] - wallet["reserved_amount"]) if wallet else 0
            if available <= 0:
                await _audit_denial(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=user_id,
                    actor_role=role,
                    reason="no_balance",
                    model_id=model_id,
                )
                raise PolicyDeniedError("no_balance", "sin saldo suficiente")

            period = now.strftime("%Y-%m")
            allocation = (
                (
                    await session.execute(
                        text("""
                            SELECT allocated_credits, consumed_credits_cached
                            FROM division_allocations
                            WHERE tenant_id = :tenant_id
                              AND division_id = :division_id
                              AND period = :period
                        """),
                        {"tenant_id": tenant_id, "division_id": division_id, "period": period},
                    )
                )
                .mappings()
                .first()
            )
            # Sin fila = sin presupuesto explícito para esta división este mes
            # (todavía no hay UI de asignación, S2-2) — no bloquea. Solo
            # bloquea si SE definió un límite y ya se agotó.
            if (
                allocation is not None
                and allocation["consumed_credits_cached"] >= allocation["allocated_credits"]
            ):
                await _audit_denial(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=user_id,
                    actor_role=role,
                    reason="no_balance",
                    model_id=model_id,
                )
                raise PolicyDeniedError(
                    "no_balance", "presupuesto de la división agotado este periodo"
                )

        user_count = await _bump_rate_limit(
            session, tenant_id=tenant_id, scope="user", scope_id=user_id, window_start=window_start
        )
        if user_count > RATE_LIMIT_USER_PER_MINUTE:
            await _audit_denial(
                session,
                tenant_id=tenant_id,
                actor_user_id=user_id,
                actor_role=role,
                reason="rate_limited",
                model_id=model_id,
            )
            raise PolicyDeniedError(
                "rate_limited", "demasiadas peticiones, prueba de nuevo en un minuto"
            )

        tenant_count = await _bump_rate_limit(
            session,
            tenant_id=tenant_id,
            scope="tenant",
            scope_id=tenant_id,
            window_start=window_start,
        )
        if tenant_count > RATE_LIMIT_TENANT_PER_MINUTE:
            await _audit_denial(
                session,
                tenant_id=tenant_id,
                actor_user_id=user_id,
                actor_role=role,
                reason="rate_limited",
                model_id=model_id,
            )
            raise PolicyDeniedError(
                "rate_limited",
                "límite de peticiones del tenant alcanzado, prueba de nuevo en un minuto",
            )

        return PolicyCheckResult(model_source=access["source"])


@dataclass(frozen=True)
class VisibleModelPrice:
    unit: str
    credit_price: float


@dataclass(frozen=True)
class VisibleModel:
    id: str
    slug: str
    display_name: str
    provider_slug: str
    provider_name: str
    min_role: str | None
    allowed: bool
    prices: list[VisibleModelPrice]


async def list_visible_models(*, tenant_id: str, role: str) -> list[VisibleModel]:
    """Modelos habilitados para ESTE tenant, agrupables por proveedor, con
    precio y visibilidad por rol (S1-12, `ModelSelector` del chat) — a
    diferencia de `onboarding.list_enabled_models` (sin proveedor/precio)
    y `onboarding.list_models_catalog` (catálogo GLOBAL activo, no lo
    habilitado de este tenant), esta combina ambas cosas y añade
    `allowed` vía `is_role_sufficient` (misma regla que usa `check()`,
    no una copia)."""
    async with tenant_session(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text("""
                        SELECT m.id, m.slug, m.display_name, p.slug AS provider_slug,
                               p.name AS provider_name, tma.min_role, er.unit, er.credit_price
                        FROM tenant_model_access tma
                        JOIN models m ON m.id = tma.model_id
                        JOIN providers p ON p.id = m.provider_id
                        LEFT JOIN exchange_rates er
                            ON er.model_id = m.id AND er.valid_to IS NULL
                        WHERE tma.tenant_id = :tenant_id AND tma.enabled = true
                        ORDER BY p.name, m.display_name, er.unit
                    """),
                    {"tenant_id": tenant_id},
                )
            )
            .mappings()
            .all()
        )

    models: dict[str, VisibleModel] = {}
    for row in rows:
        model_id = str(row["id"])
        if model_id not in models:
            models[model_id] = VisibleModel(
                id=model_id,
                slug=row["slug"],
                display_name=row["display_name"],
                provider_slug=row["provider_slug"],
                provider_name=row["provider_name"],
                min_role=row["min_role"],
                allowed=is_role_sufficient(role, row["min_role"]),
                prices=[],
            )
        if row["unit"] is not None:
            models[model_id].prices.append(
                VisibleModelPrice(unit=row["unit"], credit_price=float(row["credit_price"]))
            )

    return list(models.values())


@dataclass(frozen=True)
class ChatContext:
    division_name: str
    billing_mode: str
    dlp_mode: str
    wallet_available: Decimal | None
    division_allocated: Decimal | None
    division_consumed: Decimal | None


async def get_chat_context(*, tenant_id: str, division_id: str, billing_mode: str) -> ChatContext:
    """Snapshot para `ContextBar` (S1-12): división + saldo + presupuesto
    del periodo + modo DLP activo. `wallet_available` es `None` en BYOK
    puro (el concepto de créditos no aplica); `division_allocated`/
    `division_consumed` son `None` si no hay fila de `division_allocations`
    para el periodo actual — mismo criterio "sin fila = sin límite fijado"
    que ya usa `check()` (S2-2 todavía no tiene UI de asignación)."""
    now = datetime.now(UTC)
    period = now.strftime("%Y-%m")

    async with tenant_session(tenant_id) as session:
        division_name = (
            await session.execute(
                text("SELECT name FROM divisions WHERE id = :division_id"),
                {"division_id": division_id},
            )
        ).scalar_one()

        wallet_available: Decimal | None = None
        if billing_mode != "byok":
            wallet = (
                (
                    await session.execute(
                        text("""
                            SELECT balance_cached, reserved_amount FROM wallets
                            WHERE tenant_id = :tenant_id
                        """),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .first()
            )
            wallet_available = (
                (wallet["balance_cached"] - wallet["reserved_amount"]) if wallet else Decimal(0)
            )

        allocation = (
            (
                await session.execute(
                    text("""
                        SELECT allocated_credits, consumed_credits_cached
                        FROM division_allocations
                        WHERE tenant_id = :tenant_id AND division_id = :division_id
                          AND period = :period
                    """),
                    {"tenant_id": tenant_id, "division_id": division_id, "period": period},
                )
            )
            .mappings()
            .first()
        )

    dlp_mode = await dlp.get_mode(tenant_id=tenant_id, division_id=division_id)

    return ChatContext(
        division_name=division_name,
        billing_mode=billing_mode,
        dlp_mode=dlp_mode,
        wallet_available=wallet_available,
        division_allocated=allocation["allocated_credits"] if allocation else None,
        division_consumed=allocation["consumed_credits_cached"] if allocation else None,
    )
