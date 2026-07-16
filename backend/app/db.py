from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# asyncpg + pooler de Supabase: statement_cache_size=0 y SSL (regla 6, CLAUDE.md).
engine = create_async_engine(
    settings.database_url,
    connect_args={"statement_cache_size": 0, "ssl": "require"},
)

async_session = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Sesión con `app.tenant_id` fijado vía SET LOCAL dentro de la misma
    transacción (regla 6, CLAUDE.md) — nunca usar fuera de este helper para
    queries sobre tablas tenant-scoped."""
    async with async_session() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
        yield session
