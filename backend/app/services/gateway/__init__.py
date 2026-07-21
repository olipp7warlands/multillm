"""gateway (S1-10): selección y descifrado de credencial (BYOK/master) +
pipeline completo de `POST /api/chat/stream` — policy → DLP → hold de
créditos → streaming litellm → transacción final (metering + ledger +
requests + audit). El cifrado/descifrado de keys BYOK vive aquí porque el
descifrado SOLO puede ocurrir en este servicio (regla 3, CLAUDE.md).

Dos fases, para poder devolver códigos HTTP reales antes de comprometer la
respuesta a streaming:

- `prepare_stream()`: función normal (no generador) que resuelve policy,
  DLP y el hold de créditos. Si algo falla, lanza una excepción ANTES de
  que el endpoint cree el `StreamingResponse` — así 403/409/422 llegan
  como códigos HTTP de verdad, no dentro del SSE.
- `stream_chat()`: generador async que solo arranca una vez las cuatro
  validaciones anteriores pasaron. Aquí sí, un error a mitad de stream
  (`provider_error`) viaja como evento SSE, porque la respuesta ya es 200.

Regla de enmascarado (interno vs externo, aclarada tras revisión de este
ticket): cuando el veredicto DLP es `masked`, hacia el proveedor externo
SIEMPRE viaja `masked_text` — el prompt original JAMÁS sale de nuestra
infraestructura. Lo que se persiste en `messages.content` (tabla con RLS,
sujeto además al flag `log_full_prompts` de la división) es SIEMPRE el
original — es almacenamiento interno nuestro, con una regla distinta a la
del envío externo (ver también la nota equivalente en
docs/ARQUITECTURA.md, pipeline paso 3).
"""

import base64
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import litellm
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import tenant_session
from app.services import dlp, ledger, metering
from app.services.policy import PolicyDeniedError
from app.services.policy import check as policy_check

_logger = logging.getLogger(__name__)

# Tope duro de max_tokens por petición, independiente de lo que pida el
# cliente — acota el coste máximo de una sola llamada (y por tanto la
# estimación del hold). Sin cifra de producto documentada todavía: mismo
# criterio que los límites provisionales de rate limit en S1-8.
MAX_TOKENS_CEILING = 4096

# slug en `providers` -> atributo en Settings con la key master. Ojo:
# el slug en DB es "google", no "gemini" (ver seed de la migración 001).
_PROVIDER_MASTER_KEY_ATTR = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "google": "gemini_api_key",
}

# D6 (docs/ARQUITECTURA.md): ningún modelo del catálogo se llama con
# thinking sin límite. Mapeo mínimo por proveedor — hoy solo Gemini lo
# necesita (hallazgo del spike SP-1); Anthropic/OpenAI van sin extra.
_PROVIDER_THINKING_PARAMS: dict[str, dict] = {
    "google": {"reasoning_effort": "disable"},
}


def _fernet() -> Fernet:
    # APP_MASTER_KEY se genera con `openssl rand -base64 32` (base64 estándar,
    # ver .env.example) — Fernet exige su propia variante url-safe, así que se
    # decodifica a los 32 bytes crudos y se re-codifica en el formato que pide.
    raw_key = base64.b64decode(settings.app_master_key)
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt_provider_key(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_provider_key(ciphertext: bytes) -> str:
    """SOLO se llama desde gateway (regla 3, CLAUDE.md)."""
    try:
        return _fernet().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(
            "no se pudo descifrar la key (master key incorrecta o dato corrupto)"
        ) from e


class DLPBlockedError(Exception):
    """DLP en modo block: ya se escribió `requests`(blocked_dlp) +
    audit_event antes de lanzarse — el endpoint solo traduce a 422."""

    def __init__(self, entities_summary: dict):
        self.entities_summary = entities_summary
        super().__init__("prompt bloqueado por DLP")


class DLPMaskedError(Exception):
    """DLP en modo mask sin `confirm_masked=true` — nada se persiste
    todavía (ni requests ni audit_event); el cliente puede reintentar con
    `confirm_masked=true`, que re-evalúa DLP de forma idempotente."""

    def __init__(self, masked_text: str, entities_summary: dict):
        self.masked_text = masked_text
        self.entities_summary = entities_summary
        super().__init__("el prompt requiere confirmación de enmascarado")


@dataclass(frozen=True)
class PreparedChat:
    """Todo lo que `stream_chat()` necesita, ya validado y con el hold (si
    aplica) reservado. Nada de esto toca la red todavía."""

    tenant_id: str
    user_id: str
    role: str
    division_id: str
    model_id: str
    litellm_model_name: str
    provider_slug: str
    api_key: str
    model_source: str  # reseller|byok
    prompt_original: str
    prompt_for_provider: str
    dlp_verdict: str  # clean|masked
    entities_summary: dict
    conversation_id: str | None
    max_tokens: int
    hold_amount: Decimal | None  # None en BYOK
    wallet_id: str | None  # None en BYOK
    started_at: float


@dataclass(frozen=True)
class _FinalizeResult:
    conversation_id: str
    credits_charged: Decimal | None
    wallet_balance: Decimal | None


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _resolve_model(session: AsyncSession, *, model_id: str) -> tuple[str, str]:
    row = (
        (
            await session.execute(
                text("""
                    SELECT m.litellm_model_name, p.slug AS provider_slug
                    FROM models m JOIN providers p ON p.id = m.provider_id
                    WHERE m.id = :model_id
                """),
                {"model_id": model_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ValueError(f"modelo desconocido: {model_id}")
    return row["litellm_model_name"], row["provider_slug"]


async def _resolve_credential(
    session: AsyncSession, *, tenant_id: str, provider_slug: str, model_source: str
) -> str:
    if model_source == "reseller":
        attr = _PROVIDER_MASTER_KEY_ATTR.get(provider_slug)
        api_key = getattr(settings, attr, None) if attr else None
        if not api_key:
            raise RuntimeError(f"key master no configurada para el proveedor {provider_slug}")
        return api_key

    row = (
        await session.execute(
            text("""
                SELECT pc.encrypted_key FROM provider_connections pc
                JOIN providers p ON p.id = pc.provider_id
                WHERE pc.tenant_id = :tenant_id AND p.slug = :provider_slug AND pc.status = 'valid'
            """),
            {"tenant_id": tenant_id, "provider_slug": provider_slug},
        )
    ).first()
    if row is None:
        raise RuntimeError(f"sin key BYOK válida para el proveedor {provider_slug}")
    return decrypt_provider_key(row[0])


def _estimate_hold(prompt: str, rates: metering.ModelRates, max_tokens: int) -> Decimal:
    """Estimación conservadora en dos tramos, cada uno con SU tarifa: el
    prompt (proxy tosco de tokens: caracteres // 4) a la tarifa de
    entrada, y `max_tokens` (tope de salida de la petición) a la tarifa de
    SALIDA — es la cara, así que es la que hace la estimación conservadora
    de verdad. Decisión propia sin AC explícito en el backlog (documentada
    igual que otros valores provisionales del proyecto, p.ej. los límites
    de rate limit de S1-8): el único objetivo es evitar sobregasto por
    concurrencia entre el momento del hold y el cobro real — el cobro
    real siempre sale de `metering.calculate_credits` con el usage exacto,
    nunca de esta estimación."""
    input_tokens_estimate = Decimal(max(len(prompt) // 4, 1))
    output_tokens_estimate = Decimal(max_tokens)
    return (input_tokens_estimate / 1000) * rates.in_credit_price + (
        output_tokens_estimate / 1000
    ) * rates.out_credit_price


async def _log_full_prompts(session: AsyncSession, *, tenant_id: str, division_id: str) -> bool:
    """Fila de división si existe, si no la del tenant (division_id NULL);
    default `true` si no hay ninguna fila (D1: "prompts completos por
    defecto")."""
    row = (
        await session.execute(
            text("""
                SELECT log_full_prompts FROM dlp_settings
                WHERE tenant_id = :tenant_id AND (division_id = :division_id OR division_id IS NULL)
                ORDER BY (division_id IS NULL) ASC
                LIMIT 1
            """),
            {"tenant_id": tenant_id, "division_id": division_id},
        )
    ).first()
    return True if row is None else bool(row[0])


async def prepare_stream(
    *,
    tenant_id: str,
    user_id: str,
    role: str,
    division_id: str,
    model_id: str,
    prompt: str,
    conversation_id: str | None,
    confirm_masked: bool,
    requested_max_tokens: int,
) -> PreparedChat:
    started_at = time.monotonic()
    max_tokens = min(requested_max_tokens, MAX_TOKENS_CEILING)

    policy_result = await policy_check(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        division_id=division_id,
        model_id=model_id,
    )

    dlp_result = await dlp.analyze(tenant_id=tenant_id, division_id=division_id, prompt=prompt)

    if dlp_result.verdict == "blocked":
        async with tenant_session(tenant_id) as session:
            await session.execute(
                text("""
                    INSERT INTO requests
                        (tenant_id, user_id, division_id, model_id, conversation_id, status,
                         dlp_verdict, dlp_entities_summary)
                    VALUES
                        (:tenant_id, :user_id, :division_id, :model_id, :conversation_id,
                         'blocked_dlp', 'blocked', CAST(:entities_summary AS jsonb))
                """),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "division_id": division_id,
                    "model_id": model_id,
                    "conversation_id": conversation_id,
                    "entities_summary": json.dumps(dlp_result.entities_summary),
                },
            )
            await session.execute(
                text("""
                    INSERT INTO audit_events
                        (tenant_id, actor_user_id, actor_role, event_type, subject)
                    VALUES (:tenant_id, :actor_user_id, :actor_role, 'dlp_block',
                        jsonb_build_object('model_id', CAST(:model_id AS text)))
                """),
                {
                    "tenant_id": tenant_id,
                    "actor_user_id": user_id,
                    "actor_role": role,
                    "model_id": model_id,
                },
            )
        raise DLPBlockedError(dlp_result.entities_summary)

    if dlp_result.verdict == "masked" and not confirm_masked:
        raise DLPMaskedError(dlp_result.masked_text, dlp_result.entities_summary)

    # masked+confirmado: SIEMPRE la versión enmascarada hacia el proveedor
    # (ver docstring del módulo). clean: el original, no hay alternativa.
    prompt_for_provider = dlp_result.masked_text if dlp_result.verdict == "masked" else prompt

    async with tenant_session(tenant_id) as session:
        litellm_model_name, provider_slug = await _resolve_model(session, model_id=model_id)
        api_key = await _resolve_credential(
            session,
            tenant_id=tenant_id,
            provider_slug=provider_slug,
            model_source=policy_result.model_source,
        )

        hold_amount: Decimal | None = None
        wallet_id: str | None = None
        if policy_result.model_source == "reseller":
            rates = await metering.current_rates(session, model_id=model_id)
            hold_amount = _estimate_hold(prompt, rates, max_tokens)

            wallet = await ledger.lock_wallet(session, tenant_id=tenant_id)
            available = wallet.available if wallet else Decimal(0)
            if available < hold_amount:
                await session.execute(
                    text("""
                        INSERT INTO audit_events
                            (tenant_id, actor_user_id, actor_role, event_type, subject)
                        VALUES (:tenant_id, :actor_user_id, :actor_role, 'policy_denied',
                            jsonb_build_object(
                                'reason', 'no_balance', 'model_id', CAST(:model_id AS text),
                                'stage', 'hold'
                            ))
                    """),
                    {
                        "tenant_id": tenant_id,
                        "actor_user_id": user_id,
                        "actor_role": role,
                        "model_id": model_id,
                    },
                )
                raise PolicyDeniedError("no_balance", "sin saldo suficiente para reservar el hold")

            wallet_id = wallet.id
            await ledger.create_hold(session, wallet_id=wallet_id, amount=hold_amount)

    return PreparedChat(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        division_id=division_id,
        model_id=model_id,
        litellm_model_name=litellm_model_name,
        provider_slug=provider_slug,
        api_key=api_key,
        model_source=policy_result.model_source,
        prompt_original=prompt,
        prompt_for_provider=prompt_for_provider,
        dlp_verdict=dlp_result.verdict,
        entities_summary=dlp_result.entities_summary,
        conversation_id=conversation_id,
        max_tokens=max_tokens,
        hold_amount=hold_amount,
        wallet_id=wallet_id,
        started_at=started_at,
    )


async def _finalize_success(
    chat: PreparedChat, *, assistant_text: str, tokens_in: int, tokens_out: int, latency_ms: int
) -> _FinalizeResult:
    async with tenant_session(chat.tenant_id) as session:
        conversation_id = chat.conversation_id
        if conversation_id is None:
            title = chat.prompt_original[:60]
            conversation_id = str(
                (
                    await session.execute(
                        text("""
                            INSERT INTO conversations (tenant_id, user_id, division_id, title)
                            VALUES (:tenant_id, :user_id, :division_id, :title)
                            RETURNING id
                        """),
                        {
                            "tenant_id": chat.tenant_id,
                            "user_id": chat.user_id,
                            "division_id": chat.division_id,
                            "title": title,
                        },
                    )
                ).scalar_one()
            )

        credits_charged: Decimal | None = None
        provider_cost_eur: Decimal | None = None
        exchange_rate_id: str | None = None
        wallet = None

        if chat.model_source == "reseller":
            wallet = await ledger.lock_wallet(session, tenant_id=chat.tenant_id)
            rates = await metering.current_rates(session, model_id=chat.model_id)
            metering_result = metering.calculate_credits(
                tokens_in=tokens_in, tokens_out=tokens_out, rates=rates
            )
            credits_charged = metering_result.credits
            provider_cost_eur = metering_result.provider_cost_eur
            exchange_rate_id = metering_result.exchange_rate_id

        request_id = str(
            (
                await session.execute(
                    text("""
                        INSERT INTO requests
                            (tenant_id, user_id, division_id, model_id, conversation_id, status,
                             dlp_verdict, dlp_entities_summary, tokens_in, tokens_out, latency_ms,
                             credits_charged, provider_cost_eur, exchange_rate_id)
                        VALUES
                            (:tenant_id, :user_id, :division_id, :model_id, :conversation_id,
                             'completed', :dlp_verdict, CAST(:entities_summary AS jsonb),
                             :tokens_in, :tokens_out, :latency_ms, :credits_charged,
                             :provider_cost_eur, :exchange_rate_id)
                        RETURNING id
                    """),
                    {
                        "tenant_id": chat.tenant_id,
                        "user_id": chat.user_id,
                        "division_id": chat.division_id,
                        "model_id": chat.model_id,
                        "conversation_id": conversation_id,
                        "dlp_verdict": chat.dlp_verdict,
                        "entities_summary": json.dumps(chat.entities_summary),
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "latency_ms": latency_ms,
                        "credits_charged": credits_charged,
                        "provider_cost_eur": provider_cost_eur,
                        "exchange_rate_id": exchange_rate_id,
                    },
                )
            ).scalar_one()
        )

        wallet_balance: Decimal | None = None
        if chat.model_source == "reseller":
            wallet_balance = await ledger.record_consumption(
                session,
                tenant_id=chat.tenant_id,
                wallet_id=wallet.id,
                hold_amount=chat.hold_amount,
                request_id=request_id,
                credits=credits_charged,
                provider_cost_eur=provider_cost_eur,
                exchange_rate_id=exchange_rate_id,
            )

            period = datetime.now(UTC).strftime("%Y-%m")
            await session.execute(
                text("""
                    UPDATE division_allocations
                    SET consumed_credits_cached = consumed_credits_cached + :credits
                    WHERE tenant_id = :tenant_id AND division_id = :division_id AND period = :period
                """),
                {
                    "tenant_id": chat.tenant_id,
                    "division_id": chat.division_id,
                    "period": period,
                    "credits": credits_charged,
                },
            )

        # content SIEMPRE el original (ver docstring del módulo) — lo
        # enmascarado solo protegió el envío externo al proveedor.
        log_full = await _log_full_prompts(
            session, tenant_id=chat.tenant_id, division_id=chat.division_id
        )
        await session.execute(
            text("""
                INSERT INTO messages (tenant_id, conversation_id, role, content, content_hash)
                VALUES (:tenant_id, :conversation_id, 'user', :content, :content_hash)
            """),
            {
                "tenant_id": chat.tenant_id,
                "conversation_id": conversation_id,
                "content": chat.prompt_original if log_full else None,
                "content_hash": hashlib.sha256(chat.prompt_original.encode()).hexdigest(),
            },
        )
        await session.execute(
            text("""
                INSERT INTO messages
                    (tenant_id, conversation_id, role, content, content_hash, model_id)
                VALUES
                    (:tenant_id, :conversation_id, 'assistant', :content, :content_hash, :model_id)
            """),
            {
                "tenant_id": chat.tenant_id,
                "conversation_id": conversation_id,
                "content": assistant_text if log_full else None,
                "content_hash": hashlib.sha256(assistant_text.encode()).hexdigest(),
                "model_id": chat.model_id,
            },
        )

        # subject nunca lleva texto de prompt (ver docstring del módulo).
        await session.execute(
            text("""
                INSERT INTO audit_events (tenant_id, actor_user_id, actor_role, event_type, subject)
                VALUES (:tenant_id, :actor_user_id, :actor_role, 'request',
                    jsonb_build_object(
                        'model_id', CAST(:model_id AS text),
                        'request_id', CAST(:request_id AS text),
                        'tokens_in', CAST(:tokens_in AS integer),
                        'tokens_out', CAST(:tokens_out AS integer)
                    ))
            """),
            {
                "tenant_id": chat.tenant_id,
                "actor_user_id": chat.user_id,
                "actor_role": chat.role,
                "model_id": chat.model_id,
                "request_id": request_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        )

    return _FinalizeResult(
        conversation_id=conversation_id,
        credits_charged=credits_charged,
        wallet_balance=wallet_balance,
    )


async def _finalize_provider_error(chat: PreparedChat, *, latency_ms: int) -> None:
    async with tenant_session(chat.tenant_id) as session:
        if chat.model_source == "reseller":
            await ledger.release_hold(session, wallet_id=chat.wallet_id, amount=chat.hold_amount)

        request_id = str(
            (
                await session.execute(
                    text("""
                        INSERT INTO requests
                            (tenant_id, user_id, division_id, model_id, conversation_id, status,
                             dlp_verdict, dlp_entities_summary, latency_ms)
                        VALUES
                            (:tenant_id, :user_id, :division_id, :model_id, :conversation_id,
                             'provider_error', :dlp_verdict, CAST(:entities_summary AS jsonb),
                             :latency_ms)
                        RETURNING id
                    """),
                    {
                        "tenant_id": chat.tenant_id,
                        "user_id": chat.user_id,
                        "division_id": chat.division_id,
                        "model_id": chat.model_id,
                        "conversation_id": chat.conversation_id,
                        "dlp_verdict": chat.dlp_verdict,
                        "entities_summary": json.dumps(chat.entities_summary),
                        "latency_ms": latency_ms,
                    },
                )
            ).scalar_one()
        )

        await session.execute(
            text("""
                INSERT INTO audit_events
                    (tenant_id, actor_user_id, actor_role, event_type, subject)
                VALUES (:tenant_id, :actor_user_id, :actor_role, 'provider_error_alert',
                    jsonb_build_object(
                        'model_id', CAST(:model_id AS text),
                        'request_id', CAST(:request_id AS text)
                    ))
            """),
            {
                "tenant_id": chat.tenant_id,
                "actor_user_id": chat.user_id,
                "actor_role": chat.role,
                "model_id": chat.model_id,
                "request_id": request_id,
            },
        )


async def stream_chat(chat: PreparedChat) -> AsyncIterator[bytes]:
    """Solo se llama tras `prepare_stream()` — todas las validaciones ya
    pasaron y (si reseller) el hold ya está reservado. Un error aquí viaja
    como evento SSE `type=error`, nunca como código HTTP (la respuesta ya
    es 200)."""
    thinking_params = _PROVIDER_THINKING_PARAMS.get(chat.provider_slug, {})
    assistant_parts: list[str] = []
    tokens_in: int | None = None
    tokens_out: int | None = None
    error_detail: str | None = None

    try:
        response = await litellm.acompletion(
            model=chat.litellm_model_name,
            messages=[{"role": "user", "content": chat.prompt_for_provider}],
            api_key=chat.api_key,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=chat.max_tokens,
            **thinking_params,
        )
        async for chunk in response:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                tokens_in = usage.prompt_tokens
                tokens_out = usage.completion_tokens
            choices = getattr(chunk, "choices", None) or []
            if choices:
                content = getattr(choices[0].delta, "content", None)
                if content:
                    assistant_parts.append(content)
                    yield _sse({"type": "chunk", "content": content})
    except Exception as exc:  # provider_error a mitad de stream
        error_detail = str(exc)

    latency_ms = int((time.monotonic() - chat.started_at) * 1000)
    assistant_text = "".join(assistant_parts)

    if error_detail is None and tokens_in is not None and tokens_out is not None:
        try:
            result = await _finalize_success(
                chat,
                assistant_text=assistant_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            # Endurecimiento S1-11: un fallo aquí (tarifa vigente ausente,
            # un hipo de red a Postgres, lo que sea) NO debe dejar el
            # generador colgado ni el hold huérfano — se trata igual que un
            # provider_error real. La transacción de `_finalize_success` no
            # llegó a commitear (tenant_session hace rollback al salir con
            # excepción), así que es seguro reintentar la limpieza abajo.
            error_detail = f"fallo al finalizar la transacción: {exc}"
        else:
            yield _sse(
                {
                    "type": "done",
                    "conversation_id": result.conversation_id,
                    "credits_charged": str(result.credits_charged)
                    if result.credits_charged is not None
                    else None,
                    "wallet_balance": str(result.wallet_balance)
                    if result.wallet_balance is not None
                    else None,
                }
            )
            return

    # Fail-closed (ARQUITECTURA.md): sin usage exacto, nunca se cobra por
    # estimación — mismo tratamiento que un error real a mitad de stream.
    if error_detail is None:
        error_detail = "el proveedor no devolvió usage; no se cobran créditos por estimación"
    try:
        await _finalize_provider_error(chat, latency_ms=latency_ms)
    except Exception:
        # Best-effort (S1-11): si incluso la limpieza falla, el cliente
        # debe seguir recibiendo el evento de error — no dejar el
        # generador sin cerrar. El hold puede quedar huérfano en este caso
        # límite; `reset_orphaned_holds()` lo recupera en el próximo
        # arranque del proceso (ver docstring en app/services/ledger).
        _logger.exception("fallo al limpiar tras provider_error/finalize_error")
    yield _sse({"type": "error", "detail": error_detail})
