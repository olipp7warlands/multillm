"""onboarding: wizard de alta de tenant — validar keys BYOK en vivo,
habilitar modelos (camino reseller), preset de DLP, cierre del wizard.
"""

import secrets
from dataclasses import dataclass

import litellm
from sqlalchemy import text

from app.db import engine, tenant_session
from app.services.gateway import encrypt_provider_key

# Estricto|Equilibrado|Solo avisar (backlog, en español) -> dlp_settings.mode
_DLP_PRESET_TO_MODE = {
    "strict": "block",
    "balanced": "mask",
    "warn_only": "warn",
}


@dataclass(frozen=True)
class KeyValidationResult:
    status: str  # valid|invalid
    key_last4: str


async def _pick_test_model(session, provider_id) -> str | None:
    result = await session.execute(
        text("""
            SELECT litellm_model_name FROM models
            WHERE provider_id = :provider_id AND status = 'active'
            LIMIT 1
        """),
        {"provider_id": provider_id},
    )
    row = result.first()
    return row[0] if row else None


async def _call_provider_test(litellm_model_name: str, api_key: str) -> bool:
    """Llamada de test real y mínima — solo importa si el proveedor acepta
    la key, no el contenido de la respuesta."""
    try:
        await litellm.acompletion(
            model=litellm_model_name,
            messages=[{"role": "user", "content": "ping"}],
            api_key=api_key,
            max_tokens=1,
        )
        return True
    except Exception:
        return False


async def validate_and_store_key(
    *, tenant_id: str, provider_slug: str, api_key: str, created_by: str
) -> KeyValidationResult:
    """AC (S1-6): la key SIEMPRE se cifra antes de guardarse, válida o no
    — nunca en claro. `api_key` nunca se registra en logs (no hay ninguna
    llamada de log/print en esta ruta que la toque)."""
    async with tenant_session(tenant_id) as session:
        provider_row = (
            await session.execute(
                text("SELECT id FROM providers WHERE slug = :slug"), {"slug": provider_slug}
            )
        ).first()
        if provider_row is None:
            raise ValueError(f"proveedor desconocido: {provider_slug}")
        provider_id = provider_row[0]

        test_model = await _pick_test_model(session, provider_id)
        is_valid = bool(test_model) and await _call_provider_test(test_model, api_key)
        status = "valid" if is_valid else "invalid"

        encrypted = encrypt_provider_key(api_key)
        key_last4 = api_key[-4:] if len(api_key) >= 4 else api_key

        existing = (
            await session.execute(
                text(
                    "SELECT id FROM provider_connections "
                    "WHERE tenant_id = :tenant_id AND provider_id = :provider_id"
                ),
                {"tenant_id": tenant_id, "provider_id": provider_id},
            )
        ).first()

        if existing is not None:
            await session.execute(
                text("""
                    UPDATE provider_connections
                    SET encrypted_key = :encrypted_key, key_last4 = :key_last4,
                        status = :status, validated_at = now()
                    WHERE id = :id
                """),
                {
                    "encrypted_key": encrypted,
                    "key_last4": key_last4,
                    "status": status,
                    "id": existing[0],
                },
            )
        else:
            await session.execute(
                text("""
                    INSERT INTO provider_connections
                        (tenant_id, provider_id, encrypted_key, key_last4,
                         status, validated_at, created_by)
                    VALUES
                        (:tenant_id, :provider_id, :encrypted_key, :key_last4,
                         :status, now(), :created_by)
                """),
                {
                    "tenant_id": tenant_id,
                    "provider_id": provider_id,
                    "encrypted_key": encrypted,
                    "key_last4": key_last4,
                    "status": status,
                    "created_by": created_by,
                },
            )

    return KeyValidationResult(status=status, key_last4=key_last4)


async def enable_models(*, tenant_id: str, model_ids: list[str]) -> None:
    """Camino reseller: habilita el acceso a modelos del catálogo."""
    async with tenant_session(tenant_id) as session:
        for model_id in model_ids:
            existing = (
                await session.execute(
                    text(
                        "SELECT 1 FROM tenant_model_access "
                        "WHERE tenant_id = :tenant_id AND model_id = :model_id"
                    ),
                    {"tenant_id": tenant_id, "model_id": model_id},
                )
            ).first()
            if existing is not None:
                await session.execute(
                    text("""
                        UPDATE tenant_model_access SET enabled = true, source = 'reseller'
                        WHERE tenant_id = :tenant_id AND model_id = :model_id
                    """),
                    {"tenant_id": tenant_id, "model_id": model_id},
                )
            else:
                await session.execute(
                    text("""
                        INSERT INTO tenant_model_access (tenant_id, model_id, enabled, source)
                        VALUES (:tenant_id, :model_id, true, 'reseller')
                    """),
                    {"tenant_id": tenant_id, "model_id": model_id},
                )


async def set_dlp_preset(*, tenant_id: str, preset: str) -> str:
    mode = _DLP_PRESET_TO_MODE.get(preset)
    if mode is None:
        raise ValueError(f"preset de DLP desconocido: {preset}")

    async with tenant_session(tenant_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM dlp_settings "
                    "WHERE tenant_id = :tenant_id AND division_id IS NULL"
                ),
                {"tenant_id": tenant_id},
            )
        ).first()
        if existing is not None:
            await session.execute(
                text("UPDATE dlp_settings SET mode = :mode WHERE id = :id"),
                {"mode": mode, "id": existing[0]},
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO dlp_settings (tenant_id, division_id, mode) "
                    "VALUES (:tenant_id, NULL, :mode)"
                ),
                {"tenant_id": tenant_id, "mode": mode},
            )
    return mode


async def complete_onboarding(*, tenant_id: str, actor_user_id: str, actor_role: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("""
                INSERT INTO audit_events (tenant_id, actor_user_id, actor_role, event_type)
                VALUES (:tenant_id, :actor_user_id, :actor_role, 'onboarding_completed')
            """),
            {"tenant_id": tenant_id, "actor_user_id": actor_user_id, "actor_role": actor_role},
        )


async def list_models_catalog() -> list[dict]:
    """Catálogo [global] (sin RLS de tenant) para el paso "modelos" del
    wizard: modelo + proveedor + precios vigentes (valid_to IS NULL)."""
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                    SELECT m.id, m.slug, m.display_name, p.slug AS provider_slug,
                           p.name AS provider_name, er.unit, er.credit_price
                    FROM models m
                    JOIN providers p ON p.id = m.provider_id
                    LEFT JOIN exchange_rates er
                        ON er.model_id = m.id AND er.valid_to IS NULL
                    WHERE m.status = 'active'
                    ORDER BY p.name, m.display_name, er.unit
                """)
                )
            )
            .mappings()
            .all()
        )

    catalog: dict[str, dict] = {}
    for row in rows:
        model_id = str(row["id"])
        entry = catalog.setdefault(
            model_id,
            {
                "id": model_id,
                "slug": row["slug"],
                "display_name": row["display_name"],
                "provider_slug": row["provider_slug"],
                "provider_name": row["provider_name"],
                "prices": [],
            },
        )
        if row["unit"] is not None:
            entry["prices"].append(
                {"unit": row["unit"], "credit_price": float(row["credit_price"])}
            )

    return list(catalog.values())


async def invite_team(*, tenant_id: str, emails: list[str], role: str, created_by: str) -> int:
    """Crea filas en `invitations` (token + expiración) para la división
    default del tenant. El ENVÍO real del email es S2-7 — aquí solo se deja
    la invitación persistida; token_hash es un placeholder de un solo uso
    hasta que S2-7 defina el esquema real de token (firmado, un solo uso)."""
    async with tenant_session(tenant_id) as session:
        division_id = (
            await session.execute(
                text(
                    "SELECT id FROM divisions "
                    "WHERE tenant_id = :tenant_id AND is_default = true LIMIT 1"
                ),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()

        for email in emails:
            await session.execute(
                text("""
                    INSERT INTO invitations
                        (tenant_id, email, division_id, role, token_hash, expires_at, created_by)
                    VALUES
                        (:tenant_id, :email, :division_id, :role, :token_hash,
                         now() + interval '7 days', :created_by)
                """),
                {
                    "tenant_id": tenant_id,
                    "email": email,
                    "division_id": division_id,
                    "role": role,
                    "token_hash": secrets.token_hex(32),
                    "created_by": created_by,
                },
            )

    return len(emails)


async def list_enabled_models(*, tenant_id: str) -> list[dict]:
    """Modelos habilitados para ESTE tenant (tenant_model_access.enabled) —
    consulta mínima para el landing de /chat al terminar el wizard. Sin
    filtrado por rol/división todavía: eso es PolicyService (S1-8), que
    puede ampliar o sustituir esta consulta cuando exista."""
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                text("""
                    SELECT m.id, m.display_name, m.slug
                    FROM tenant_model_access tma
                    JOIN models m ON m.id = tma.model_id
                    WHERE tma.tenant_id = :tenant_id AND tma.enabled = true
                    ORDER BY m.display_name
                """),
                {"tenant_id": tenant_id},
            )
        ).mappings().all()
    return [{"id": str(r["id"]), "display_name": r["display_name"], "slug": r["slug"]} for r in rows]
