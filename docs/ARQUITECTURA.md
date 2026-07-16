# AIhub · Arquitectura

## Qué es
Gateway white-label multitenant a modelos de IA externos. Dos modos comerciales por tenant:
- **Reseller**: keys master nuestras; el tenant compra **créditos** (moneda interna, ~0,01 €)
  y consume a un tipo de cambio por modelo donde vive nuestro margen.
- **BYOK**: el tenant conecta sus propias keys; no consume créditos, paga plataforma.

## Pipeline por petición — `POST /api/chat/stream` (SSE)
```
1. TenantResolver (middleware): subdominio/CNAME → tenant; SET LOCAL app.tenant_id
   dentro de la transacción (RLS); branding+settings en cache de memoria del proceso
2. PolicyService.check(user, model, division)
     falla → 403 con motivo (modelo no habilitado / sin saldo / rate limit)
     denegación por política → audit_event
3. DLPService.analyze(prompt, tenant, division)  [Presidio + diccionarios custom]
     clean   → sigue
     masked  → si no llega confirm_masked=true: 409 con versión enmascarada
               (UI muestra "Enviar enmascarado / Editar"); NO se llama al proveedor
     blocked → 422 + audit_event + fila en requests con status blocked_dlp
4. Hold de créditos (solo reseller): fila de hold en Postgres con expiración +
   SELECT ... FOR UPDATE sobre el wallet al apuntar. Evita carreras de saldo.
5. GatewayService.stream() → chunks SSE al cliente
   LiteLLM como LIBRERÍA (litellm.acompletion(stream=True)) en el propio proceso;
   credencial: key master (reseller) o key del tenant descifrada (BYOK)
6. Al cerrar el stream, EN UNA TRANSACCIÓN:
   MeteringService → créditos exactos con usage real del proveedor
   LedgerService   → consumption (libera hold, apunta real + coste proveedor)
   requests        → fila completa (tokens, coste, créditos, verdict, latencia)
   AuditService    → event
7. Último evento SSE: credits_charged + saldo restante de la división
```
Error de proveedor a mitad de stream: cobrar solo usage parcial si lo reporta;
si no, no cobrar y status `provider_error`. Nunca hold huérfano (TTL + job limpieza).

**Regla fail-closed de MeteringService (post-spike SP-1)**: litellm normaliza
TODOS los proveedores a su propio esquema de chunk; el chunk final con `usage`
solo llega si la llamada pasa `stream_options={"include_usage": true}` —
verificado en vivo que esto aplica a Anthropic y Gemini por igual, no es un
detalle de un proveedor concreto. GatewayService pasa este flag SIEMPRE, sin
excepción. Si aun así el stream se completa sin que llegue el evento de usage
(bug de la librería, cambio de proveedor, lo que sea): **no se cobran créditos
por estimación en ningún caso**; el request se marca `status=provider_error`
(mismo tratamiento que un fallo de proveedor a mitad de stream — a valorar en
S1-11 si conviene un status propio en `requests` para distinguir ambos casos
en las métricas), AuditService registra un evento de alerta, y el hold
(reseller) se libera sin apuntar consumo. La única fuente válida de créditos
es el usage exacto devuelto por el proveedor.

## Servicios backend (`backend/app/services/`)
| Servicio | Responsabilidad |
|---|---|
| tenant_resolver | subdominio/CNAME → tenant; `SET LOCAL app.tenant_id`; cache branding |
| auth (adapter) | verifica JWT de Supabase Auth; invitaciones; mapping auth.users → users/memberships |
| policy | visibilidad de modelo por rol, saldo división, rate limit (contadores Postgres) |
| dlp | Presidio EN PROCESO (presidio-analyzer/anonymizer pip + spaCy es_core_news_md); diccionarios tenant/división; veredicto + texto enmascarado |
| gateway | litellm (librería) streaming; selección y descifrado de credencial |
| metering | tokens exactos del usage; resuelve exchange_rate vigente; calcula créditos |
| ledger | apuntes atómicos; topups; verificación de integridad (job nocturno) |
| audit | escritura de eventos; consultas para auditoría por persona |
| branding | theming CRUD; verificación CNAME (registro TXT) |
| onboarding | wizard: tenant+owner+división default; validación keys en vivo; activar catálogo |

## Decisiones resueltas
- **D1 logs**: prompts completos por defecto; `content_hash` SIEMPRE;
  `content` detrás del flag `log_full_prompts` (conmutable por división).
- **D2 cifrado BYOK**: envelope encryption en Postgres, master key como secreto
  de despliegue (env). Migrable a KMS/Vault en fase enterprise.
- **D3 Presidio**: en proceso (librería), engine spaCy español cargado al arrancar;
  diccionarios en cache de memoria con invalidación por versión en DB; spike mide p95.
- **D4 infraestructura**: Supabase (Postgres+RLS+Auth) + Railway (backend, frontend).
  SIN Docker local y SIN Redis en F1: holds de créditos con SELECT ... FOR UPDATE
  sobre wallets y rate limiting con contadores en Postgres. Redis (Upstash/Railway)
  solo si el volumen lo pide.
- **D5 auth**: Supabase Auth (email/password + Google). El backend verifica el JWT
  de Supabase; nuestras tablas users/memberships mapean auth.users → tenant/rol.
- **D6 thinking en catálogo (post-spike SP-1)**: varios modelos actuales tienen
  "thinking"/razonamiento extendido activado por defecto y pueden agotar
  `max_tokens` en tokens de razonamiento ocultos sin emitir texto visible —
  y esos tokens son coste real y facturable del proveedor. Los modelos que se
  habilitan en el catálogo de Fase 1 (`models`) se configuran con el thinking
  **desactivado o con presupuesto explícito y acotado** (según lo soporte cada
  proveedor); no se habilita ningún modelo con thinking sin límite. La
  configuración usada debe quedar registrada junto al modelo (para poder
  auditar por qué un `credits_charged` incluye tokens de razonamiento).

## Garantías de seguridad (ver también CLAUDE.md)
1. RLS por tenant en toda tabla + test cross-tenant en CI que falla si una tabla nace sin política
2. Ledger y auditoría inmutables por trigger de motor
3. Keys cifradas en reposo, solo last4 visibles, descifrado solo en gateway
4. Metering de grado facturable: usage real + exchange_rates versionadas (cada cobro auditable);
   fail-closed si el usage no llega — nunca se cobra por estimación (ver regla en el pipeline)

## Frontend — rutas (App Router)
```
/(onboarding)/start          wizard 4 pasos (crear espacio → conectar modelos →
                             preset DLP → invitar equipo); <5 min de key a primer prompt
/(app)/chat[/id]             chat multi-modelo: ContextBar (división·créditos·DLP),
                             coste por respuesta, ModelSelector, DLPInterstitial
/(admin)/admin/models        qué modelos ve cada rol
/(admin)/admin/dlp           diccionarios + política de logs por división
/(admin)/admin/wallet        saldo, packs, ledger, allocations por división
/(admin)/admin/branding      logo, colores, product_name, CNAME
/(admin)/admin/metrics       consumo, incidencias, por división
/(admin)/admin/audit         auditoría por persona + export CSV
/(operator)/...              panel operador cross-tenant (host aparte, rol DB propio):
                             tenants, topup manual, exchange rates, márgenes
```
Theming: design tokens CSS por tenant inyectados en SSR. Tenant demo de referencia
visual: "Velora" (ver mockups en el doc estratégico).

## Fuera de Fase 1
Stripe, marketplace visual, imagen/voz/video, subida de archivos al chat,
SSO SAML, single-tenant dedicado, mercado secundario de créditos.
