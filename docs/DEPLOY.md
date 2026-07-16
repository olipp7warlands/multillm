# AIhub · Deploy (Railway)

## Backend

Railway usa Nixpacks con un `buildCommand` explícito en `backend/railway.json`:

```
pip install -e . && python -m spacy download es_core_news_md
```

**Por qué el paso extra de spaCy**: `presidio-analyzer` necesita el modelo de
lenguaje `es_core_news_md`, pero ese modelo NO se instala solo por declarar
`spacy` como dependencia en `pyproject.toml` — es un paquete propio que se
descarga aparte. Sin este paso, el deploy arranca pero `DLPService` (S1-9)
fallaría en el primer request al intentar cargar el engine. Esto se
descubrió en el spike (`docs/spike.md`, SP-2) y aquí queda fijado en el
proceso de build, no solo como nota.

`deploy.startCommand` corre `alembic upgrade head` (contra `DATABASE_URL_ADMIN`,
rol `postgres` — nunca `app_backend`, ver `docs/MODELO_DATOS.md`) antes de
arrancar `uvicorn`.

### Variables de entorno requeridas
Las mismas de `.env.example`: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`SUPABASE_JWT_SECRET`, `DATABASE_URL_ADMIN` (postgres, vía pooler — el host
de conexión directa de Supabase es IPv6-only y no es utilizable desde
Railway sin confirmar soporte IPv6, ver `docs/spike.md` SP-3), `DATABASE_URL`
(`app_backend`, vía pooler en modo transaction), keys de proveedores,
`APP_MASTER_KEY`, `BASE_DOMAIN` (dominio wildcard real en producción, no
`lvh.me`).

## Frontend

`frontend/railway.json`: build estándar de Nixpacks (`npm install && npm run
build`, autodetectado), `startCommand: npm run start`.

## Local (setup en frío)

```
cd backend
pip install -e ".[dev]"
python -m spacy download es_core_news_md   # requerido para DLPService (S1-9+)
alembic upgrade head
uvicorn app.main:app --reload
```
