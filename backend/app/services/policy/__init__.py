"""PolicyService (S1-8): paso 2 del pipeline de docs/ARQUITECTURA.md, antes
de DLP y del proveedor — visibilidad de modelo (tenant_model_access +
min_role), saldo (wallet + allocation de división del periodo, solo en
modo reseller) y rate limit (contadores Postgres por ventana). Denegar
aquí es más barato que denegar después de un escaneo DLP.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import tenant_session

_ROLE_RANK = {"user": 0, "division_admin": 1, "admin": 2, "owner": 3}

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

        if access["min_role"] is not None and _ROLE_RANK[role] < _ROLE_RANK[access["min_role"]]:
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
