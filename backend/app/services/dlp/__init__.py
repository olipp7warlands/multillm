"""DLPService (S1-9): paso 3 del pipeline de docs/ARQUITECTURA.md — Presidio
en proceso (D3) sobre el prompt, veredicto según `dlp_settings` de la
división (block|mask|warn) y placeholders numerados por tipo (<PERSONA_1>,
<CLIENTE_1>...). El engine se precarga una vez en el lifespan de FastAPI
(`init_engine()`) porque cargar spaCy tarda 1-3 s (SP-2, docs/spike.md) —
irrelevante en el arranque, inaceptable por request.

Alcance de S1-9 (a propósito, no más): `analyze()` es una función pura de
lectura — no escribe `requests` ni `audit_events`. Esas escrituras
transaccionales son responsabilidad de GatewayService (S1-10), que es quien
decide qué hacer con el veredicto dentro del pipeline completo.
"""

import logging
import time
from dataclasses import dataclass

from presidio_analyzer import AnalyzerEngine, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import PhoneRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from sqlalchemy import text

from app.db import tenant_session

# Solo las entidades relevantes para DLP de prompts de chat (arrastrado de
# SP-2, docs/spike.md): SpacyRecognizer también etiqueta AGE/DATE_TIME/ID/
# NRP/ORGANIZATION con el modelo es_core_news_md, y no las pedimos — igual
# que no pedimos URL (UrlRecognizer). Pedir solo estas evita que esos
# recognizers/etiquetas corran en absoluto (AnalyzerEngine filtra por
# `entities` ANTES de ejecutar, no después), lo que además ayuda al p95.
_ENTITIES = [
    "PERSON",
    "LOCATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "CREDIT_CARD",
    "ES_NIF",
    "ES_NIE",
]

_LABELS = {
    "PERSON": "PERSONA",
    "LOCATION": "LOCALIZACION",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "TELEFONO",
    "IBAN_CODE": "IBAN",
    "CREDIT_CARD": "TARJETA",
    "ES_NIF": "NIF",
    "ES_NIE": "NIE",
}

# dlp_dictionaries.category -> (entidad ad-hoc, etiqueta del placeholder)
_DICT_CATEGORY_ENTITY = {
    "client": "DLP_DICT_CLIENT",
    "project": "DLP_DICT_PROJECT",
    "code": "DLP_DICT_CODE",
    "custom": "DLP_DICT_CUSTOM",
}
_DICT_CATEGORY_LABEL = {
    "client": "CLIENTE",
    "project": "PROYECTO",
    "code": "CODIGO",
    "custom": "TERMINO",
}
_DICT_ENTITY_LABEL = {v: _DICT_CATEGORY_LABEL[k] for k, v in _DICT_CATEGORY_ENTITY.items()}

_anonymizer_engine = AnonymizerEngine()  # sin estado pesado, barato de construir
_analyzer_engine: AnalyzerEngine | None = None


def init_engine() -> None:
    """Construye el AnalyzerEngine (carga es_core_news_md) — llamar UNA vez,
    en el lifespan de FastAPI. Vuelve a crear el engine si ya existía (por
    ejemplo, un test de módulo que lo necesita antes de que arranque la app)."""
    global _analyzer_engine

    # Presidio, en DEBUG, loguea el texto original (contexto de entidades,
    # valores de match) — nunca debe propagarse a ese nivel en esta app, sea
    # cual sea la config de logging raíz (regla 3, CLAUDE.md; hallazgo del
    # test de logging de S1-10 en tests/test_gateway.py).
    for logger_name in ("presidio-analyzer", "presidio-anonymizer"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "es", "model_name": "es_core_news_md"}],
    }
    nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()
    engine = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["es"])

    # Poda de recognizers sin uso real para DLP de prompts de chat (SP-2):
    # cada uno añade un pase de regex sobre todo el texto sin aportar valor.
    for name in (
        "CryptoRecognizer",
        "IpRecognizer",
        "MacAddressRecognizer",
        "MedicalLicenseRecognizer",
        "PhoneRecognizer",
    ):
        engine.registry.remove_recognizer(name, language="es")
    # El PhoneRecognizer por defecto usa regiones EE.UU./UK/DE/FR/IL/IN/CA/BR
    # y no detecta teléfonos españoles — se SUSTITUYE, nunca se añade encima
    # (si no, el regex corre duplicado). NIF/NIE español ya vienen cubiertos
    # por EsNifRecognizer/EsNieRecognizer predefinidos (con validación de
    # dígito de control) — sin recognizer custom propio para DNI.
    engine.registry.add_recognizer(
        PhoneRecognizer(supported_language="es", supported_regions=["ES"])
    )

    _analyzer_engine = engine


def get_analyzer_engine() -> AnalyzerEngine:
    if _analyzer_engine is None:
        raise RuntimeError("DLPService.init_engine() no se llamó todavía (falta el lifespan)")
    return _analyzer_engine


@dataclass(frozen=True)
class DLPResult:
    verdict: str  # clean|masked|blocked
    masked_text: str | None
    entities_summary: dict[str, int]  # SOLO tipos y conteos (regla D1) — nunca valores


# --- caché en memoria de dlp_settings/dlp_dictionaries (sin Redis, D4) ---
# Mismo patrón que tenant_resolver: TTL + invalidación explícita por
# tenant_id. dlp_dictionaries no tiene columna de versión en el esquema (a
# diferencia de tenant_branding.updated_at) — aquí no hace falta reutilizar
# nada porque no hay ninguna columna natural que sirva de marca de versión;
# el TTL + invalidate_dlp_cache() (a llamar desde el CRUD de S2-1 tras
# cualquier escritura) es suficiente y evita una migración solo para esto.
_CACHE_TTL_SECONDS = 60
_dictionary_cache: dict[tuple[str, str], tuple[float, dict[str, list[str]]]] = {}
_settings_cache: dict[tuple[str, str], tuple[float, str]] = {}


def invalidate_dlp_cache(tenant_id: str | None = None) -> None:
    """A llamar tras un INSERT/UPDATE/DELETE en dlp_dictionaries o
    dlp_settings (S2-1) — o con ningún argumento para vaciar toda la caché."""
    if tenant_id is None:
        _dictionary_cache.clear()
        _settings_cache.clear()
        return
    for cache in (_dictionary_cache, _settings_cache):
        for key in [k for k in cache if k[0] == tenant_id]:
            del cache[key]


async def _fetch_mode(tenant_id: str, division_id: str) -> str:
    async with tenant_session(tenant_id) as session:
        row = (
            (
                await session.execute(
                    text("""
                    SELECT mode FROM dlp_settings
                    WHERE tenant_id = :tenant_id
                      AND (division_id = :division_id OR division_id IS NULL)
                    ORDER BY CASE WHEN division_id = :division_id THEN 0 ELSE 1 END
                    LIMIT 1
                """),
                    {"tenant_id": tenant_id, "division_id": division_id},
                )
            )
            .mappings()
            .first()
        )
    # Sin fila configurada: fail-closed (block), no "warn" — un DLP sin
    # configurar no debe equivaler a sin protección (mismo principio fail-
    # closed que MeteringService, ver docs/ARQUITECTURA.md).
    return row["mode"] if row is not None else "block"


async def _fetch_dictionary_terms(tenant_id: str, division_id: str) -> dict[str, list[str]]:
    async with tenant_session(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text("""
                    SELECT term, category FROM dlp_dictionaries
                    WHERE tenant_id = :tenant_id
                      AND (division_id = :division_id OR division_id IS NULL)
                """),
                    {"tenant_id": tenant_id, "division_id": division_id},
                )
            )
            .mappings()
            .all()
        )
    terms_by_category: dict[str, list[str]] = {}
    for row in rows:
        terms_by_category.setdefault(row["category"], []).append(row["term"])
    return terms_by_category


async def _get_mode(tenant_id: str, division_id: str) -> str:
    key = (tenant_id, division_id)
    cached = _settings_cache.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]
    mode = await _fetch_mode(tenant_id, division_id)
    _settings_cache[key] = (now, mode)
    return mode


async def get_mode(*, tenant_id: str, division_id: str) -> str:
    """Wrapper público de `_get_mode` (S1-12, `ContextBar` del chat quiere
    mostrar el modo DLP activo sin analizar ningún prompt) — misma caché,
    mismo fail-closed a `block` sin fila de `dlp_settings`, cero lógica
    duplicada."""
    return await _get_mode(tenant_id, division_id)


async def _get_dictionary_terms(tenant_id: str, division_id: str) -> dict[str, list[str]]:
    key = (tenant_id, division_id)
    cached = _dictionary_cache.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]
    terms = await _fetch_dictionary_terms(tenant_id, division_id)
    _dictionary_cache[key] = (now, terms)
    return terms


def _label_for(entity_type: str) -> str:
    return _LABELS.get(entity_type) or _DICT_ENTITY_LABEL.get(entity_type, entity_type)


def _mask(original: str, results: list) -> tuple[str, dict[str, int]]:
    """Placeholders numerados por tipo (<PERSONA_1>, <PERSONA_2>...) — el
    mismo valor exacto reutiliza el mismo placeholder si aparece más de una
    vez. AnonymizerEngine resuelve los solapamientos entre recognizers
    (p.ej. EMAIL_ADDRESS vs. un match espurio de otro recognizer) con su
    estrategia por defecto (MERGE_SIMILAR_OR_CONTAINED) — no hay que
    reimplementar esa lógica a mano."""
    assigned: dict[tuple[str, str], str] = {}
    counts: dict[str, int] = {}

    def make_operator(entity_type: str) -> OperatorConfig:
        label = _label_for(entity_type)

        def _replacement(text_value: str) -> str:
            key = (label, text_value)
            if key not in assigned:
                counts[label] = counts.get(label, 0) + 1
                assigned[key] = f"<{label}_{counts[label]}>"
            return assigned[key]

        return OperatorConfig("custom", {"lambda": _replacement})

    operators = {
        entity_type: make_operator(entity_type) for entity_type in {r.entity_type for r in results}
    }
    anonymized = _anonymizer_engine.anonymize(
        text=original, analyzer_results=results, operators=operators
    )
    return anonymized.text, dict(counts)


async def analyze(*, tenant_id: str, division_id: str, prompt: str) -> DLPResult:
    engine = get_analyzer_engine()
    mode = await _get_mode(tenant_id, division_id)
    terms_by_category = await _get_dictionary_terms(tenant_id, division_id)

    ad_hoc_recognizers = []
    entities = list(_ENTITIES)
    for category, terms in terms_by_category.items():
        entity_name = _DICT_CATEGORY_ENTITY.get(category)
        if entity_name is None or not terms:
            continue
        ad_hoc_recognizers.append(
            PatternRecognizer(
                supported_entity=entity_name, deny_list=terms, supported_language="es"
            )
        )
        entities.append(entity_name)

    results = engine.analyze(
        text=prompt, language="es", entities=entities, ad_hoc_recognizers=ad_hoc_recognizers
    )
    if not results:
        return DLPResult(verdict="clean", masked_text=None, entities_summary={})

    masked_text, entities_summary = _mask(prompt, results)

    if mode == "block":
        return DLPResult(verdict="blocked", masked_text=None, entities_summary=entities_summary)
    if mode == "mask":
        return DLPResult(
            verdict="masked", masked_text=masked_text, entities_summary=entities_summary
        )
    # warn: visible en dlp_entities_summary/auditoría, pero no bloquea ni
    # enmascara nada — el prompt sigue tal cual ("Solo avisar", S1-6).
    return DLPResult(verdict="clean", masked_text=None, entities_summary=entities_summary)
