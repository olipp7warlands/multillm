"""LedgerService (grueso en S1-10, carrera dedicada + endurecimiento en
S1-11): única vía de escritura sobre `wallets`/`ledger_entries` (regla 5,
CLAUDE.md — nunca INSERT/UPDATE directo desde otro servicio).

`lock_wallet`/`create_hold`/`release_hold`/`record_consumption` reciben una
`AsyncSession` ya abierta por el llamador (no abren su propia
`tenant_session`) para poder componer con el resto de la transacción del
pipeline de GatewayService. La corrección bajo concurrencia depende de que
el llamador haga `lock_wallet` (SELECT ... FOR UPDATE) antes de
`create_hold`/`record_consumption` — dentro de la MISMA transacción
(verificado con un test de carrera real en S1-11, `tests/test_ledger.py`).

`check_integrity` y `reset_orphaned_holds` sí abren su propia
`tenant_session` por tenant (nunca bypassrls, ni para esto — regla 1,
CLAUDE.md): son funciones de mantenimiento de todo el sistema (job
nocturno de S2-8 / lifespan de la app), no pasos de la transacción de una
petición concreta.
"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine, tenant_session


@dataclass(frozen=True)
class WalletRow:
    id: str
    balance_cached: Decimal
    reserved_amount: Decimal

    @property
    def available(self) -> Decimal:
        return self.balance_cached - self.reserved_amount


async def lock_wallet(session: AsyncSession, *, tenant_id: str) -> WalletRow | None:
    """`SELECT ... FOR UPDATE` sobre la fila de wallet del tenant. `None`
    si el tenant no tiene wallet todavía — 0 disponible, no es un error
    (mismo criterio que PolicyService, S1-8)."""
    row = (
        (
            await session.execute(
                text("""
                    SELECT id, balance_cached, reserved_amount FROM wallets
                    WHERE tenant_id = :tenant_id
                    FOR UPDATE
                """),
                {"tenant_id": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return WalletRow(
        id=str(row["id"]),
        balance_cached=row["balance_cached"],
        reserved_amount=row["reserved_amount"],
    )


async def create_hold(session: AsyncSession, *, wallet_id: str, amount: Decimal) -> None:
    await session.execute(
        text("""
            UPDATE wallets SET reserved_amount = reserved_amount + :amount, updated_at = now()
            WHERE id = :wallet_id
        """),
        {"wallet_id": wallet_id, "amount": amount},
    )


async def release_hold(session: AsyncSession, *, wallet_id: str, amount: Decimal) -> None:
    """Libera un hold sin tocar `balance_cached` — camino de
    `provider_error`/limpieza, nunca se cobra por estimación."""
    await session.execute(
        text("""
            UPDATE wallets SET reserved_amount = reserved_amount - :amount, updated_at = now()
            WHERE id = :wallet_id
        """),
        {"wallet_id": wallet_id, "amount": amount},
    )


async def record_consumption(
    session: AsyncSession,
    *,
    tenant_id: str,
    wallet_id: str,
    hold_amount: Decimal,
    request_id: str,
    credits: Decimal,
    provider_cost_eur: Decimal,
    exchange_rate_id: str,
) -> Decimal:
    """Libera el hold Y descuenta el consumo real en una sola UPDATE,
    dentro de la transacción final del pipeline (el llamador ya hizo
    `lock_wallet` en esta misma transacción). Devuelve `balance_after`
    para el evento SSE final y para `ledger_entries.balance_after`.

    `ts` se pasa como `clock_timestamp()` explícito, NO se deja el
    `DEFAULT now()` de la columna (S1-11, investigado con el test de
    carrera): `now()` en Postgres es la hora de INICIO de la transacción,
    no la del INSERT — bajo concurrencia real, dos transacciones pueden
    capturar su `now()` en un orden que no coincide con el orden real en
    que adquieren el lock de `lock_wallet` y confirman. `balance_after` en
    sí siempre es correcto (se calcula ya dentro de la sección serializada
    por el lock), pero ordenar `ledger_entries` por `ts` para auditarlo
    requiere que `ts` refleje el momento real de escritura.
    `clock_timestamp()`, a diferencia de `now()`, sí cambia en cada
    llamada dentro de la misma transacción — y esta INSERT ya ocurre
    DENTRO de la sección serializada, así que captura el orden real."""
    balance_after = (
        await session.execute(
            text("""
                UPDATE wallets
                SET balance_cached = balance_cached - :credits,
                    reserved_amount = reserved_amount - :hold_amount,
                    updated_at = now()
                WHERE id = :wallet_id
                RETURNING balance_cached
            """),
            {"wallet_id": wallet_id, "credits": credits, "hold_amount": hold_amount},
        )
    ).scalar_one()

    await session.execute(
        text("""
            INSERT INTO ledger_entries
                (tenant_id, wallet_id, ts, type, credits_delta, provider_cost_eur,
                 exchange_rate_id, request_id, balance_after)
            VALUES
                (:tenant_id, :wallet_id, clock_timestamp(), 'consumption', :credits_delta,
                 :provider_cost_eur, :exchange_rate_id, :request_id, :balance_after)
        """),
        {
            "tenant_id": tenant_id,
            "wallet_id": wallet_id,
            "credits_delta": -credits,
            "provider_cost_eur": provider_cost_eur,
            "exchange_rate_id": exchange_rate_id,
            "request_id": request_id,
            "balance_after": balance_after,
        },
    )
    return balance_after


async def _list_tenant_ids() -> list[str]:
    """`tenants` es `[global]` (sin RLS de tenant, docs/MODELO_DATOS.md) —
    lectura directa sobre el engine, sin `tenant_session`."""
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id FROM tenants"))
        return [str(row[0]) for row in rows]


# Acota cuántos tenants se reconcilian a la vez: RLS obliga a iterar tenant
# a tenant (regla 1, CLAUDE.md — nunca bypassrls, ni para mantenimiento), y
# con decenas/cientos de tenants un bucle puramente secuencial se nota de
# verdad en el arranque (medido en local: ~20 s con ~75 tenants, cada uno
# un round-trip de red al pooler de Supabase). Correr en paralelo, acotado
# para no agotar el pool del engine (`app/db.py`, default 5 + 10 overflow)
# compitiendo con tráfico real.
_RECONCILE_CONCURRENCY = 8


@dataclass(frozen=True)
class IntegrityDivergence:
    tenant_id: str
    wallet_id: str
    balance_cached: Decimal
    ledger_sum: Decimal
    difference: Decimal


async def check_integrity(*, tenant_id: str | None = None) -> list[IntegrityDivergence]:
    """Compara `wallets.balance_cached` contra `SUM(ledger_entries.
    credits_delta)` por wallet — pensada para llamarse tanto desde un test
    (con `tenant_id`) como desde el job nocturno de S2-8 (sin argumentos,
    recorre todos los tenants). Alcance explícito: solo esta comparación.
    `reserved_amount` (holds en vuelo) no tiene un ledger propio con el
    que compararlo — es un problema distinto, fuera de este ticket (ver
    `reset_orphaned_holds` para el caso de holds huérfanos por caída de
    proceso, que sí se cubre)."""
    tenant_ids = [tenant_id] if tenant_id else await _list_tenant_ids()
    semaphore = asyncio.Semaphore(_RECONCILE_CONCURRENCY)

    async def _check_one(tid: str) -> list[IntegrityDivergence]:
        async with semaphore, tenant_session(tid) as session:
            rows = (
                await session.execute(
                    text("""
                        SELECT w.id AS wallet_id, w.balance_cached,
                               COALESCE(SUM(le.credits_delta), 0) AS ledger_sum
                        FROM wallets w
                        LEFT JOIN ledger_entries le ON le.wallet_id = w.id
                        WHERE w.tenant_id = :tenant_id
                        GROUP BY w.id, w.balance_cached
                    """),
                    {"tenant_id": tid},
                )
            ).mappings()
            return [
                IntegrityDivergence(
                    tenant_id=tid,
                    wallet_id=str(row["wallet_id"]),
                    balance_cached=row["balance_cached"],
                    ledger_sum=row["ledger_sum"],
                    difference=row["balance_cached"] - row["ledger_sum"],
                )
                for row in rows
                if row["balance_cached"] - row["ledger_sum"] != 0
            ]

    results = await asyncio.gather(*[_check_one(tid) for tid in tenant_ids])
    return [divergence for sublist in results for divergence in sublist]


async def reset_orphaned_holds() -> None:
    """Reconciliación al arranque del proceso (S1-11): sin tabla de holds
    por fila, `reserved_amount` es un contador agregado sin forma de
    expirar un hold individual — si el proceso muere (crash, deploy) con
    un stream en vuelo, ese hold queda sumado para siempre. Un proceso
    recién arrancado, por definición, no tiene ningún stream propio en
    vuelo todavía, así que CUALQUIER `reserved_amount` > 0 en ese momento
    es basura de una instancia anterior — se resetea a 0.

    **Asume instancia única** (cierto en Fase 1 sobre Railway sin
    autoscaling, D4 en docs/ARQUITECTURA.md). Con más de una instancia
    corriendo a la vez, el arranque de una NO implica que las demás no
    tengan streams en vuelo — este reset las pisaría con holds legítimos
    en curso. Escalar a múltiples instancias exige antes migrar a una
    tabla de holds por fila con TTL/expiración propia en vez de este
    contador agregado. Llamada desde el lifespan de FastAPI
    (`app/main.py`), junto a `dlp.init_engine()`.

    Coste: una `tenant_session` por tenant, en paralelo acotado por
    `_RECONCILE_CONCURRENCY` (ver comentario junto a la constante) — sin
    esto, medido en local con ~75 tenants de prueba acumulados: ~20 s de
    arranque solo para esta reconciliación, inaceptable para un cold
    start."""
    tenant_ids = await _list_tenant_ids()
    semaphore = asyncio.Semaphore(_RECONCILE_CONCURRENCY)

    async def _reset_one(tid: str) -> None:
        async with semaphore, tenant_session(tid) as session:
            await session.execute(
                text("""
                    UPDATE wallets SET reserved_amount = 0, updated_at = now()
                    WHERE tenant_id = :tenant_id AND reserved_amount != 0
                """),
                {"tenant_id": tid},
            )

    await asyncio.gather(*[_reset_one(tid) for tid in tenant_ids])
