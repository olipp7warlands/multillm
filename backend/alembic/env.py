import asyncio
from logging.config import fileConfig
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Migraciones DDL van con el rol postgres (DATABASE_URL_ADMIN), nunca con
# app_backend — ver docs/MODELO_DATOS.md ("Gestión del rol app_backend").


class _AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent.parent / ".env", extra="ignore"
    )
    database_url_admin: str


admin_settings = _AdminSettings()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# El esquema real llega en S1-2; de momento no hay metadata que autogenerar.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=admin_settings.database_url_admin,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(
        admin_settings.database_url_admin,
        connect_args={"statement_cache_size": 0, "ssl": "require"},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
