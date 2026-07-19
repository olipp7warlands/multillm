"use client";

import { useEffect, useState } from "react";

import { WizardProgress } from "@/components/WizardProgress";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/session";

type CatalogPrice = { unit: string; credit_price: number };
type CatalogModel = {
  id: string;
  slug: string;
  display_name: string;
  provider_slug: string;
  provider_name: string;
  prices: CatalogPrice[];
};

function formatPrices(prices: CatalogPrice[]): string {
  return prices
    .map((p) => `${p.credit_price} créditos / ${p.unit.replace("1k_tokens_", "1K tokens ")}`)
    .join(" · ");
}

export default function ModelsStepPage() {
  const [mode, setMode] = useState<"reseller" | "byok">("reseller");
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loadingCatalog, setLoadingCatalog] = useState(true);

  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [validation, setValidation] = useState<"idle" | "checking" | "valid" | "invalid">("idle");
  const [validationError, setValidationError] = useState<string | null>(null);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadCatalog() {
      const token = await getAccessToken();
      if (!token) return;
      const response = await apiFetch("/api/onboarding/models-catalog", token);
      if (response.ok) {
        const body = await response.json();
        setCatalog(body.models);
        if (body.models.length > 0) setProvider(body.models[0].provider_slug);
      }
      setLoadingCatalog(false);
    }
    loadCatalog();
  }, []);

  function toggleModel(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleValidateKey() {
    setValidation("checking");
    setValidationError(null);
    const token = await getAccessToken();
    if (!token) return;
    const response = await apiFetch("/api/onboarding/validate-key", token, {
      method: "POST",
      body: JSON.stringify({ provider_slug: provider, api_key: apiKey }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok && body.status === "valid") {
      setValidation("valid");
    } else {
      setValidation("invalid");
      setValidationError("La key no funcionó — revisa que sea correcta y tenga saldo");
    }
  }

  async function handleNext() {
    setSaving(true);
    setError(null);
    const token = await getAccessToken();
    if (!token) {
      setError("Sesión no encontrada");
      setSaving(false);
      return;
    }

    if (mode === "reseller") {
      const response = await apiFetch("/api/onboarding/enable-models", token, {
        method: "POST",
        body: JSON.stringify({ model_ids: Array.from(selected) }),
      });
      if (!response.ok) {
        setError("No se pudieron habilitar los modelos");
        setSaving(false);
        return;
      }
    }
    window.location.href = "/start/dlp";
  }

  const providers = Array.from(
    new Map(catalog.map((m) => [m.provider_slug, m.provider_name])).entries(),
  );

  return (
    <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center p-6">
      <WizardProgress current={2} />
      <h1 className="mb-1 text-xl font-semibold text-foreground">Conecta tus modelos</h1>
      <p className="mb-6 text-sm text-muted">
        Usa tus propias keys (BYOK) o elige del catálogo con precio en créditos.
      </p>

      <div className="mb-4 flex gap-2">
        <button
          type="button"
          onClick={() => setMode("reseller")}
          className={`rounded px-3 py-1.5 text-sm ${
            mode === "reseller" ? "bg-primary text-primary-foreground" : "bg-surface text-foreground"
          }`}
        >
          Catálogo
        </button>
        <button
          type="button"
          onClick={() => setMode("byok")}
          className={`rounded px-3 py-1.5 text-sm ${
            mode === "byok" ? "bg-primary text-primary-foreground" : "bg-surface text-foreground"
          }`}
        >
          Traer mis propias keys (BYOK)
        </button>
      </div>

      {mode === "reseller" && (
        <div className="flex flex-col gap-2">
          {loadingCatalog && <p className="text-sm text-muted">Cargando catálogo…</p>}
          {catalog.map((model) => (
            <label
              key={model.id}
              className="flex items-start gap-3 rounded border border-border p-3 text-sm"
            >
              <input
                type="checkbox"
                checked={selected.has(model.id)}
                onChange={() => toggleModel(model.id)}
                className="mt-1"
              />
              <span>
                <span className="block font-medium text-foreground">
                  {model.display_name} <span className="text-muted">· {model.provider_name}</span>
                </span>
                <span className="block text-muted">{formatPrices(model.prices)}</span>
              </span>
            </label>
          ))}
        </div>
      )}

      {mode === "byok" && (
        <div className="flex flex-col gap-3">
          <select
            value={provider}
            onChange={(event) => {
              setProvider(event.target.value);
              setValidation("idle");
            }}
            className="rounded border border-border bg-background px-3 py-2 text-foreground"
          >
            {providers.map(([slug, name]) => (
              <option key={slug} value={slug}>
                {name}
              </option>
            ))}
          </select>
          <div className="flex items-center gap-2">
            <input
              type="password"
              placeholder="API key"
              value={apiKey}
              onChange={(event) => {
                setApiKey(event.target.value);
                setValidation("idle");
              }}
              className="flex-1 rounded border border-border bg-background px-3 py-2 text-foreground"
            />
            <button
              type="button"
              onClick={handleValidateKey}
              disabled={!apiKey || validation === "checking"}
              className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50"
            >
              {validation === "checking" ? "Validando…" : "Validar"}
            </button>
            {validation === "valid" && (
              <span
                className="checkmark-pop flex h-7 w-7 items-center justify-center rounded-full bg-success text-primary-foreground"
                aria-label="Key válida"
              >
                ✓
              </span>
            )}
            {validation === "invalid" && (
              <span
                className="flex h-7 w-7 items-center justify-center rounded-full bg-danger text-primary-foreground"
                aria-label="Key inválida"
              >
                ✕
              </span>
            )}
          </div>
          {validationError && <p className="text-sm text-danger">{validationError}</p>}
        </div>
      )}

      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      <button
        type="button"
        onClick={handleNext}
        disabled={saving || (mode === "byok" && validation !== "valid")}
        className="mt-6 rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50"
      >
        {saving ? "Guardando…" : "Siguiente"}
      </button>
    </main>
  );
}
