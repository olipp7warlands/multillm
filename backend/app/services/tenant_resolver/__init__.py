"""TenantResolver: subdominio/CNAME → tenant, con branding+settings en
caché de memoria del proceso (sin Redis en F1, ver D4 en docs/ARQUITECTURA.md).

Diseño de la caché (no viene detallado en el backlog, decisión propia):
no hay columna de versión dedicada en el esquema — se reutiliza
`tenant_branding.updated_at`, que ya existe y avanza en cada cambio, como
marca de versión de cada entrada. La entrada se sirve tal cual mientras no
haya pasado `_CACHE_TTL_SECONDS` desde que se guardó (evita una query en
cada request); pasado ese tiempo se refresca entera. Los servicios que
escriben tenants/tenant_branding (branding en S2-5, suspensión de tenant)
deben llamar a `invalidate_tenant_cache()` tras el UPDATE para verse
reflejados al instante en el mismo proceso, en vez de esperar al TTL —
importante en despliegues con varios workers, donde cada proceso tiene su
propia caché y no hay pub/sub compartido en esta fase.
"""

import time
from dataclasses import dataclass

from sqlalchemy import text

from app.config import settings
from app.db import engine, tenant_session

_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class TenantContext:
    id: str
    slug: str
    status: str
    billing_mode: str
    product_name: str | None
    logo_url: str | None
    favicon_url: str | None
    color_primary: str | None
    color_accent: str | None
    email_from_name: str | None
    branding_version: str | None  # tenant_branding.updated_at, como marca de versión


class TenantNotFoundError(Exception):
    pass


class TenantSuspendedError(Exception):
    def __init__(self, tenant: TenantContext):
        self.tenant = tenant
        super().__init__(f"tenant {tenant.slug!r} está suspendido")


_cache: dict[str, tuple[float, TenantContext]] = {}


def _split_host(host: str) -> tuple[str, str]:
    """Devuelve (host sin puerto, slug candidato relativo a BASE_DOMAIN)."""
    host_no_port = host.split(":")[0].lower()
    suffix = f".{settings.base_domain.lower()}"
    if host_no_port.endswith(suffix):
        return host_no_port, host_no_port[: -len(suffix)]
    return host_no_port, host_no_port


def invalidate_tenant_cache(tenant_id: str | None = None) -> None:
    """Invalida por `tenant_id` — lo que tiene a mano cualquier servicio que
    acaba de hacer un UPDATE sobre tenants/tenant_branding (branding en
    S2-5, suspensión de tenant) — o toda la caché si no se indica ninguno.
    Nunca por host/slug: la caché se indexa por host (custom_domain O
    slug.BASE_DOMAIN) y un caller que escribe datos normalmente no tiene el
    host a mano, solo el id del tenant que acaba de tocar."""
    if tenant_id is None:
        _cache.clear()
        return
    for key, (_, context) in list(_cache.items()):
        if context.id == tenant_id:
            del _cache[key]


async def _fetch_tenant_context(host_no_port: str, slug: str) -> TenantContext | None:
    # tenants es [global] (sin RLS de tenant): una conexión simple basta.
    async with engine.connect() as conn:
        result = await conn.execute(
            text("""
                SELECT id, slug, status, billing_mode
                FROM tenants
                WHERE custom_domain = :host OR slug = :slug
                LIMIT 1
            """),
            {"host": host_no_port, "slug": slug},
        )
        row = result.mappings().first()
    if row is None:
        return None

    # tenant_branding SÍ es tenant-scoped (RLS con la plantilla CASE WHEN) —
    # hay que pasar por tenant_session() para que el SET LOCAL deje ver la fila,
    # tal como pide el ticket (nunca una query suelta sobre una tabla con RLS).
    branding = {}
    async with tenant_session(str(row["id"])) as session:
        b_result = await session.execute(
            text("""
                SELECT product_name, logo_url, favicon_url, color_primary,
                       color_accent, email_from_name, updated_at
                FROM tenant_branding
                WHERE tenant_id = :tenant_id
            """),
            {"tenant_id": row["id"]},
        )
        b_row = b_result.mappings().first()
        if b_row is not None:
            branding = dict(b_row)

    return TenantContext(
        id=str(row["id"]),
        slug=row["slug"],
        status=row["status"],
        billing_mode=row["billing_mode"],
        product_name=branding.get("product_name"),
        logo_url=branding.get("logo_url"),
        favicon_url=branding.get("favicon_url"),
        color_primary=branding.get("color_primary"),
        color_accent=branding.get("color_accent"),
        email_from_name=branding.get("email_from_name"),
        branding_version=str(branding["updated_at"]) if branding.get("updated_at") else None,
    )


async def resolve_tenant(host: str) -> TenantContext:
    """404 -> TenantNotFoundError; 403 -> TenantSuspendedError (con el
    tenant ya resuelto colgado en la excepción, por si el caller quiere
    detalles); si no, el TenantContext activo."""
    host_no_port, slug = _split_host(host)
    cache_key = host_no_port
    cached = _cache.get(cache_key)
    now = time.monotonic()

    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        context = cached[1]
    else:
        context = await _fetch_tenant_context(host_no_port, slug)
        if context is None:
            _cache.pop(cache_key, None)
            raise TenantNotFoundError(host_no_port)
        _cache[cache_key] = (now, context)

    if context.status == "suspended":
        raise TenantSuspendedError(context)
    return context
