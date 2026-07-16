# AIhub — módulo de Ownia

AI Gateway white-label multitenant con economía de créditos, DLP y auditoría inmutable.
Stack: **Supabase** (Postgres+RLS+Auth) + **Railway** (deploy) · sin Docker.

## Arrancar en local (2 procesos, nada más)
1. Crear proyecto en Supabase → copiar credenciales a `.env` (ver `.env.example`)
2. `cd backend && pip install -e . && python -m spacy download es_core_news_md
   && alembic upgrade head && uvicorn app.main:app --reload`
3. `cd frontend && npm install && npm run dev`
4. Abrir `http://demo.lvh.me:3000` (los subdominios de tenant funcionan en local vía lvh.me)

## Trabajar con Claude Code
Abrir desde esta raíz — el contexto vive en `CLAUDE.md` y la documentación en `docs/`.
Primer prompt sugerido: "Lee CLAUDE.md y docs/BACKLOG.md y empieza por SP-1".

## Documentación
- `docs/ARQUITECTURA.md` — pipeline, servicios, decisiones (D1-D5), garantías
- `docs/MODELO_DATOS.md` — esquema completo (fuente de verdad de la migración 001)
- `docs/BACKLOG.md` — spike (3 validaciones) + 2 sprints con criterios de aceptación
- `docs/DEPLOY.md` — deploy en Railway, variables de entorno, setup en frío
