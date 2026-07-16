# AIhub · Resultados del spike (SP-1, SP-2, SP-3)

Entorno de referencia: Windows 11, Python 3.13.1, `pip 25.3`. Scripts ejecutados en
venvs aislados fuera del repo (no forman parte del scaffold de Sprint 1).

> **Nota de entorno Windows/Python 3.13**: con resolución normal de pip, la instalación
> de `litellm`/`tiktoken` intentó compilar una dependencia desde código fuente y pedía
> un toolchain de Rust (`cargo`) no instalado. Todas las dependencias tienen wheel
> para `cp313-win_amd64`; el fix fue forzar `pip install --only-binary :all: ...`.
> Dejar esto anotado para el scaffold de `backend/` en S1-1 (pin de versiones + wheels).

---

## SP-1 · litellm como librería — **completo**

### Estado
- **Gemini: verificado en vivo**, streaming + usage exacto confirmado.
- **Anthropic: verificado en vivo** tras rotar la key y activar saldo en la
  cuenta. El bloqueo inicial (`"Your credit balance is too low"`) era de
  facturación de la cuenta, no de la integración; resuelto.
- Nota lateral: el modelo usado en la primera prueba (`claude-3-5-haiku-20241022`)
  ya no existe para esta cuenta/fecha (`404 not_found_error`) — confirma el
  Hallazgo 3 (nombres de modelo con vida corta) también en Anthropic, no solo
  en Gemini. Modelo válido usado para verificar: `claude-haiku-4-5-20251001`
  (vía `client.models.list()`).

### Hallazgo 1 — el usage en streaming NO llega por defecto en NINGÚN
proveedor a través de litellm (se corrige una hipótesis inicial)
Sin `stream_options={"include_usage": True}`, litellm devuelve `usage=None`
en el último chunk **tanto para Gemini como para Anthropic**, aunque el
contenido de la respuesta llega perfectamente — el stream "funciona" pero
MeteringService no tendría con qué cobrar. Con el flag, el usage llega en un
chunk final dedicado (sin `choices[].delta.content`, solo el objeto `usage`).

Se había planteado como hipótesis que Anthropic no necesitaría el flag
porque su API nativa siempre incluye usage (`message_start` +
`message_delta`, sin flag) — **verificado en vivo que es falso a través de
litellm**: litellm normaliza TODOS los proveedores a su propio esquema de
chunk estilo OpenAI, y ese chunk de usage final está condicionado al flag
**independientemente del proveedor subyacente**. Confirmado con
`claude-haiku-4-5-20251001`: `usage=None` sin el flag, `usage` completo
(`prompt_tokens=24, completion_tokens=45`) con el flag — coincide
exactamente con lo que reportó el SDK nativo de Anthropic en la misma
llamada (`input_tokens=24, output_tokens=41`; los 4 tokens de diferencia en
completion son de una respuesta ligeramente distinta, no del mecanismo).

**Regla para S1-10/GatewayService: pasar `stream_options={"include_usage": true}`
SIEMPRE, para todos los proveedores, sin excepción** — no hay atajo por
proveedor. Ver regla fail-closed añadida a `docs/ARQUITECTURA.md`.

### Hallazgo 2 — modelos "reasoning" consumen el presupuesto de tokens en
pensamiento oculto, no en la respuesta visible
`gemini-flash-latest` y `gemini-3.5-flash` tienen "thinking" activado por
defecto. Con `max_tokens=60` y `max_tokens=300` el modelo agotó todo el
presupuesto en `reasoning_tokens` (Gemini los reporta como
`completion_tokens_details.reasoning_tokens` vía litellm, o
`thoughts_token_count` vía SDK nativo) y cortó por `finish_reason=length`
**sin emitir ni una palabra de texto visible**. Hubo que pasar
`reasoning_effort="disable"` para obtener contenido real con un presupuesto
de tokens razonable.

**Implicación directa para MeteringService (S1-11)**: esos tokens de
razonamiento son coste real y facturable del proveedor aunque el usuario
nunca los vea ni pasen por DLP de salida. Si un modelo de un tenant tiene
thinking activado, `credits_charged` debe incluir esos tokens — y
probablemente conviene decidir por modelo/tenant si el thinking va activado,
porque puede disparar el coste de una respuesta corta sin avisar.

### Hallazgo 3 — nombres de modelo con vida corta
`gemini-2.0-flash` y `gemini-2.5-flash` devuelven 404
("no longer available" / "no longer available a new users") aunque siguen
listados por `client.models.list()`. Los alias "-latest" (`gemini-flash-latest`)
funcionan pero pueden apuntar a un modelo distinto sin aviso. **Decisión**:
`models.litellm_model_name` en el catálogo debe fijar una versión concreta
verificada, no un alias "-latest", y necesitamos un job/alerta que detecte
modelos deprecados (404) para no dejar un modelo "enabled" que ya no responde.

### Latencia litellm vs SDK nativo

**Gemini** (con thinking desactivado, 3 repeticiones por vía):
- litellm: 20.2s / 19.1s / 13.1s (mediana 19.1s)
- SDK nativo (`google-genai`): 17.7s / 10.8s / 15.4s (mediana 15.4s)

**Anthropic** (`claude-haiku-4-5-20251001`, sin "thinking" — modelo estable,
3 repeticiones por vía):
- litellm: 1.243s / 1.266s / 1.071s (mediana 1.243s)
- SDK nativo (`anthropic`): 1.141s / 1.564s / 2.026s (mediana 1.564s)

En Anthropic, sin la varianza de un modelo "reasoning" de por medio, litellm
no muestra overhead consistente frente al SDK nativo (incluso más rápido en
esta muestra) — la diferencia está dentro del ruido de red. En Gemini la
varianza entre repeticiones (10-20s) domina sobre cualquier diferencia
atribuible al wrapping de litellm. **Conclusión: no hay overhead sistemático
de litellm detectable frente a las SDKs nativas** en ninguno de los dos
proveedores.

---

## SP-2 · Presidio en proceso — **completo**

### Setup
`presidio-analyzer`, `presidio-anonymizer`, `spacy==3.8.13`,
modelo `es_core_news_md==3.8.0` (~42 MB, wheel, sin compilación). Recognizer
custom para el diccionario tenant/división: `PatternRecognizer` con
`deny_list` de 200 términos (`Proyecto-Aurora-000..099`,
`Cliente-Zafiro-000..099`), simulando `dlp_dictionaries`.

### Arranque del engine
**1.0 – 3.2 s** en las distintas ejecuciones (carga de spaCy + registro de
recognizers). Cumple sobradamente el objetivo de **< 15 s** — apto para
precargar en el `lifespan` de FastAPI sin impacto notable en el arranque.

### Benchmark de latencia — objetivo p95 < 150 ms: **NO se cumple en este equipo**
Prompt sintético en español de 528 palabras con PII real embebida (nombres,
emails, teléfonos, IBAN, tarjeta, NIF con checksum válido, localización) y
12 términos del diccionario custom intercalados. 100 iteraciones de
`analyzer.analyze()` por ejecución, repetido varias veces:

| ejecución | mean (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| 1 | 242 | 289 | 298 |
| 2 | 250 | 291 | 297 |
| 3 | 242 | 301 | 357 |

p95 real observado: **~290-300 ms**, entre 1.5x y 2x el objetivo de 150 ms.
El propio engine mostró 2-3x de varianza entre ejecuciones (ver nota de
metodología abajo).

### Diagnóstico
- Ejecutar solo `nlp(texto)` de spaCy (sin Presidio) mide ~71-90 ms p95 en
  aislamiento — la mayor parte del coste NO es la inferencia de spaCy en sí,
  sino overhead propio de Presidio (`NlpArtifacts`, enriquecimiento de
  contexto, orquestación de recognizers).
- Desactivar el componente `parser` de spaCy (el más caro del pipeline y el
  menos usado por los recognizers de Presidio, que dependen de NER) dio una
  mejora marginal, insuficiente por sí sola para bajar de 150 ms.
- El recognizer custom de 200 términos (`deny_list`) no es el cuello de
  botella principal.

### Bugs/gotchas corregidos durante el spike (relevantes para S1-9)
- El `PhoneRecognizer` que carga por defecto usa regiones EE.UU. y no
  detecta teléfonos españoles («612 345 678»); hay que sustituirlo
  (`registry.remove_recognizer` + añadir uno con `supported_regions=["ES"]`),
  no añadirlo encima (si no, corre el regex duplicado).
- NIF/NIE español ya vienen cubiertos por `EsNifRecognizer`/`EsNieRecognizer`
  predefinidos — no hace falta un recognizer custom para DNI. Su validación
  de dígito de control funciona correctamente: un DNI con letra de checksum
  inválida NO se marca como `ES_NIF` (probado con `12345678A`, checksum
  correcto es `Z`).
- Recognizers predefinidos sin relevancia para el caso de uso (Crypto, IP,
  MAC, licencia médica) se quitaron del registry — cada uno añade un pase de
  regex sobre todo el texto sin aportar valor a un DLP de prompts de chat.

### Nota de metodología — varianza de la máquina
Se observó una varianza de 2-3x entre ejecuciones sucesivas del mismo
benchmark en este portátil (mean pasó de 107 ms a 253 ms entre corridas sin
cambios de código), consistente con contención de CPU en segundo plano
(entorno de desarrollo compartido, no un contenedor dedicado). **Estos
números son direccionales, no una medición de SLA.** Antes de comprometerse
a escanear DLP de forma síncrona dentro de la request en S1-9, hay que
repetir este benchmark en las specs reales de Railway.

### Decisión
Seguir con Presidio en proceso (librería, D3), engine precargado en el
`lifespan` de FastAPI — el coste de arranque es irrelevante. El riesgo de
p95 se traslada como advertencia explícita a S1-9: si el número se confirma
también en Railway, las mitigaciones a evaluar son (a) `es_core_news_sm`
como modelo más ligero (con pérdida de recall en NER), (b) solapar el
análisis DLP con PolicyService en vez de ejecutarlos estrictamente en serie,
(c) revisar si diccionarios custom más grandes que 200 términos degradan el
recognizer de `deny_list` de forma no lineal.

---

## SP-3 · Supabase RLS + pooler — **pendiente**

No iniciado. Bloqueado a la espera de que el usuario cree el proyecto
Supabase y añada `DATABASE_URL` (pooler, rol `app_backend` sin bypassrls),
`SUPABASE_URL` y `SUPABASE_JWT_SECRET` a `.env`. Se ejecuta al final,
según lo acordado.
