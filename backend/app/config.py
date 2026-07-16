from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env vive en la raíz del monorepo, un nivel por encima de backend/
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_PATH, extra="ignore")

    database_url: str
    supabase_url: str
    supabase_jwt_secret: str
    app_master_key: str
    base_domain: str = "lvh.me"


settings = Settings()
