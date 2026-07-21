"""MeteringService (grueso en S1-10, endurecido en S1-11): resuelve la
tarifa vigente de un modelo y calcula créditos/coste real a partir del
`usage` exacto devuelto por el proveedor — nunca por estimación (regla
fail-closed, docs/ARQUITECTURA.md). GatewayService solo llama a
`calculate_credits` cuando el stream terminó CON `usage`; si no llega,
trata el caso como `provider_error` sin pasar por aquí.

Nota sobre tokens de "thinking" (S1-11, verificando el hallazgo 2 del
spike SP-1, docs/spike.md): `usage.completion_tokens` — el campo que
`gateway.stream_chat()` captura como `tokens_out` — YA incluye los
tokens de razonamiento oculto en el esquema que litellm normaliza (son un
desglose DENTRO de `completion_tokens`, vía
`completion_tokens_details.reasoning_tokens`, no una cifra aparte). Esta
función ya factura esos tokens correctamente sin necesitar ningún cambio;
no "arreglar" esto sin evidencia nueva de que el desglose cambió.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MissingExchangeRateError(Exception):
    """Fail-closed explícito (S1-11): sustituye al `KeyError` genérico de
    S1-10 — mismo comportamiento (nunca datos parciales), tipo localizable
    en vez de una excepción que podría confundirse con un bug interno."""

    def __init__(self, model_id: str):
        self.model_id = model_id
        super().__init__(f"faltan tarifas vigentes para el modelo {model_id}")


@dataclass(frozen=True)
class ModelRates:
    """Tarifas vigentes (`valid_to IS NULL`) para un modelo, una fila por
    unidad. Ambas deben existir — si falta alguna, KeyError explícito en
    vez de calcular con datos parciales."""

    in_rate_id: str
    in_provider_cost_eur: Decimal
    in_credit_price: Decimal
    out_rate_id: str
    out_provider_cost_eur: Decimal
    out_credit_price: Decimal


@dataclass(frozen=True)
class MeteringResult:
    credits: Decimal
    provider_cost_eur: Decimal
    exchange_rate_id: (
        str  # referencia informativa (fila 1k_tokens_in) — ver docstring de calculate_credits
    )


async def current_rates(session: AsyncSession, *, model_id: str) -> ModelRates:
    rows = (
        (
            await session.execute(
                text("""
                    SELECT id, unit, provider_cost_eur, credit_price
                    FROM exchange_rates
                    WHERE model_id = :model_id
                      AND unit IN ('1k_tokens_in', '1k_tokens_out')
                      AND valid_to IS NULL
                """),
                {"model_id": model_id},
            )
        )
        .mappings()
        .all()
    )
    by_unit = {row["unit"]: row for row in rows}
    if "1k_tokens_in" not in by_unit or "1k_tokens_out" not in by_unit:
        raise MissingExchangeRateError(model_id)

    in_row = by_unit["1k_tokens_in"]
    out_row = by_unit["1k_tokens_out"]
    return ModelRates(
        in_rate_id=str(in_row["id"]),
        in_provider_cost_eur=in_row["provider_cost_eur"],
        in_credit_price=in_row["credit_price"],
        out_rate_id=str(out_row["id"]),
        out_provider_cost_eur=out_row["provider_cost_eur"],
        out_credit_price=out_row["credit_price"],
    )


def calculate_credits(*, tokens_in: int, tokens_out: int, rates: ModelRates) -> MeteringResult:
    """Créditos y coste real a partir de usage exacto. `exchange_rate_id`
    de la fila `1k_tokens_in` se guarda como referencia de "qué versión de
    tarifa estaba vigente" — decisión propia documentada (S1-10): el
    esquema solo tiene un FK único en `requests`/`ledger_entries` aunque el
    cálculo combine dos filas (entrada y salida), así que se elige una como
    puntero informativo sin que afecte al cálculo, que sí usa ambas."""
    credits = (Decimal(tokens_in) / 1000) * rates.in_credit_price + (
        Decimal(tokens_out) / 1000
    ) * rates.out_credit_price
    provider_cost_eur = (Decimal(tokens_in) / 1000) * rates.in_provider_cost_eur + (
        Decimal(tokens_out) / 1000
    ) * rates.out_provider_cost_eur
    return MeteringResult(
        credits=credits, provider_cost_eur=provider_cost_eur, exchange_rate_id=rates.in_rate_id
    )
