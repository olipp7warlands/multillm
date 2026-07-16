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

## SP-3 · Supabase RLS + pooler — **completo**

### Setup
Rol `app_backend` (`NOSUPERUSER NOBYPASSRLS`) creado por SQL contra el
proyecto Supabase real. Tabla `spike_rls_test` (`tenant_id uuid`, `note`)
con `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` y una política
por `current_setting('app.tenant_id')`. Verificado con `asyncpg`
(`statement_cache_size=0`) desde un script en un venv aislado, insertando
una fila de un tenant A y otra de un tenant B.

### Hallazgo 1 — el host de conexión directa es IPv6-only
`db.<ref>.supabase.co` solo resuelve a una dirección IPv6
(`2a05:d01c:...`); esta máquina/red no tiene ruta IPv6 y `getaddrinfo`
falla, aunque el DNS en sí resuelve bien (confirmado con `nslookup`, que sí
mostró la IP). La conexión de superusuario para el DDL del paso 1 se hizo
en su lugar a través del **mismo host del pooler** (que sí resuelve IPv4)
usando `postgres.<ref>` como usuario — Supabase soporta cualquier rol vía
pooler con ese formato. **Implicación para despliegue**: si Railway (o el
entorno que sea) no tiene salida IPv6, la app NUNCA debe depender del host
de conexión directa — solo del pooler. Esto ya era la decisión de D4/regla 6
del CLAUDE.md; este hallazgo la confirma con un motivo concreto y
reproducible, no solo como buena práctica.

### Hallazgo 2 — límites de `postgres` como CREATEROLE no-superuser (PG16+)
El `postgres` de Supabase **no es superuser real** (`rolsuper=false`,
`rolbypassrls=true` — confirmado por query directa). Con Postgres 16+, un
rol con `CREATEROLE` pero sin `SUPERUSER`:
- SÍ puede `CREATE ROLE ... NOSUPERUSER NOBYPASSRLS` (crear un rol nuevo).
- NO puede incluir `NOSUPERUSER`/`NOBYPASSRLS` en un `ALTER ROLE` posterior
  sobre ese mismo rol, aunque sea un no-op — Postgres exige ser superuser
  real para tocar esos atributos en un `ALTER`, incluso en la dirección "no".
- NO puede `DROP OWNED BY`/`DROP ROLE` sobre un rol que no posee como
  miembro, aunque lo haya creado él mismo (`permission denied to drop
  objects... only roles with privileges of role "app_backend"...`).

**Consecuencia práctica para S1-2**: la migración que crea `app_backend` debe
hacerlo con `CREATE ROLE` (fijando los atributos ahí, una sola vez) y tratarlo
como esencialmente permanente — rotar su password con `ALTER ROLE ... PASSWORD`
(sin tocar atributos) en vez de asumir que se puede recrear libremente desde
una migración idempotente basada en DROP/CREATE.

### Hallazgo 3 — tras rotar la password, la autenticación vía pooler falla
de forma transitoria (en AMBOS modos, no solo uno)
Justo después de `ALTER ROLE app_backend PASSWORD '...'`, el primer intento
de conectar como `app_backend` a través del pooler (tanto en modo session
como en modo transaction) falla con `password authentication failed`,
incluso con la contraseña correcta recién verificada, sin relación con
caracteres especiales, formato de usuario (`app_backend.<ref>`, confirmado
necesario — sin el sufijo de proyecto el pooler responde
`no tenant identifier provided`) ni con `rolcanlogin`. Un **reintento con
un pequeño backoff (unos segundos) resuelve la conexión de forma fiable**
— parece una ventana de sincronización de credenciales del pooler
(Supavisor), no un bloqueo permanente ni específico de un modo de pooling.
**Regla práctica para el backend**: la capa de conexión debe reintentar en
`InvalidPasswordError` con backoff corto tras cualquier rotación de
password de `app_backend`, en vez de asumir que el cambio es instantáneo.

### Hallazgo 4 — CRÍTICO: entre transacciones, el pooler deja el GUC
custom en `''` (cadena vacía), no en `NULL`
Con una política ingenua (`USING (tenant_id = current_setting('app.tenant_id',
true)::uuid)`), reproducido en un repro mínimo de 3 líneas:
- Transacción nueva, nunca se ha tocado `app.tenant_id` → `current_setting(...,
  true)` = `None` (correcto).
- Dentro de una transacción, tras `set_config('app.tenant_id', 'X', true)` →
  `current_setting(...)` = `'X'` (correcto, SET LOCAL aplicado).
- **Una transacción NUEVA después de que la anterior hiciera commit** (mismo
  `asyncpg.Connection`, vía el pooler) → `current_setting(..., true)` = `''`
  (cadena vacía), **no** `None`. Esto no es el comportamiento de una sesión
  Postgres normal (donde `SET LOCAL` se deshace automáticamente al cerrar la
  transacción, volviendo a "no definido"); apunta a que el pooler de Supabase
  hace algún tipo de limpieza de GUCs custom al reciclar la conexión entre
  transacciones, dejándolos en `''` en vez de indefinidos.

**Por qué importa**: `''::uuid` lanza `InvalidTextRepresentationError` en vez
de simplemente no matchear ninguna fila. Con la política ingenua, cualquier
código que olvide el `SET LOCAL app.tenant_id` al principio de una
transacción **no falla de forma segura con "0 filas" — lanza un error de
Postgres** (falla ruidoso, que es preferible a una fuga silenciosa, pero
rompe la query en vez de simplemente no devolver datos).

**Segundo gotcha, más sutil**: la corrección obvia —
`AND current_setting(...) <> ''` antes del cast — **no funciona**. Postgres
NO garantiza el orden de evaluación de los operandos de un `AND` (es un
punto documentado de las propias FAQ de Postgres); el intento de cast a
`uuid` se puede seguir disparando aunque el guard anterior sea falso. La
única forma correcta de garantizar el orden es un `CASE WHEN`:

```sql
USING (
    tenant_id = (
        CASE
            WHEN current_setting('app.tenant_id', true) IS NULL
                 OR current_setting('app.tenant_id', true) = ''
            THEN NULL
            ELSE current_setting('app.tenant_id', true)::uuid
        END
    )
)
```

Con este `CASE`, verificado en los 4 escenarios (sin `SET LOCAL`; tenant A;
nueva transacción sin `SET LOCAL` tras la de A; tenant B) en **ambos modos
del pooler**: aísla correctamente, cero filas cuando no hay tenant en
contexto, cero fugas cruzadas, sin errores.

**Regla obligatoria para S1-2**: TODA política RLS de AIhub que use
`current_setting('app.tenant_id')` debe usar esta forma con `CASE WHEN`, no
la forma ingenua con `AND`/cast directo — de lo contrario cualquier request
que reutilice una conexión pooled tras una transacción anterior puede
lanzar un 500 en vez de devolver "sin acceso".

### Verificación de aislamiento — resultado final (ambos modos)
| Escenario | Modo session (5432) | Modo transaction (6543) |
|---|---|---|
| Sin `SET LOCAL` | 0 filas ✓ | 0 filas ✓ |
| `SET LOCAL` tenant A | 1 fila, sin fuga de B ✓ | 1 fila, sin fuga de B ✓ |
| Transacción nueva sin `SET LOCAL` (tras la de A) | 0 filas ✓ | 0 filas ✓ |
| `SET LOCAL` tenant B | 1 fila, sin fuga de A ✓ | 1 fila, sin fuga de A ✓ |

### Verificación del punto 4 — `postgres` salta el RLS (por qué la regla 1
del CLAUDE.md lo prohíbe para negocio)
Con la misma tabla y política, conectando como `postgres`
(`rolbypassrls=true`) y sin ningún `SET LOCAL`, un `SELECT` devuelve **las
filas de AMBOS tenants**:
```
[postgres, rolbypassrls=true] SELECT sin SET LOCAL -> 2 filas:
   <uuid tenant A>  fila de tenant A
   <uuid tenant B>  fila de tenant B
```
Esta es la demostración directa de por qué la regla 1 del CLAUDE.md prohíbe
usar el rol `postgres`/service role para queries de negocio: `BYPASSRLS` no
es una política más permisiva, es una salida total del sistema de RLS — da
igual cuántas políticas por tenant existan, un rol con `bypassrls=true` las
ignora todas. Toda query de negocio DEBE ir por `app_backend`
(`NOBYPASSRLS`), nunca por `postgres`/service role.

### Recomendación de modo de pooler
**Usar modo transaction (6543)**, tal como ya decidía D4/regla 6 del
CLAUDE.md — no por descarte del modo session (ambos aislaron correctamente
en esta prueba), sino porque el modo transaction es el que escala con
`holds`/rate limiting sobre Postgres sin agotar conexiones bajo carga
concurrente (que es exactamente el escenario de producción, a diferencia de
este spike con una sola conexión secuencial). El hallazgo 4 (CASE obligatorio
en la política) y el hallazgo 3 (reintento tras rotar password) aplican por
igual a los dos modos — no son un argumento para elegir uno u otro.

### Limpieza
Tabla `spike_rls_test` eliminada. El rol `app_backend` **se deja creado**
(no es cruft del spike — es infraestructura real que S1-2 va a necesitar de
todos modos); su password se ha rotado varias veces durante las pruebas y la
última rotación queda solo en memoria del proceso del spike, no persistida
en ningún sitio — S1-2 debe rotarla de nuevo y guardarla como corresponda.
