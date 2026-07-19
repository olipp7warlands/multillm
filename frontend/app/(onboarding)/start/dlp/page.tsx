"use client";

import { useState } from "react";

import { WizardProgress } from "@/components/WizardProgress";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/session";

const PRESETS = [
  {
    id: "strict",
    label: "Estricto",
    description: "Bloquea el envío si detecta datos sensibles — el más seguro.",
  },
  {
    id: "balanced",
    label: "Equilibrado",
    description: "Enmascara los datos sensibles y deja continuar — recomendado.",
  },
  {
    id: "warn_only",
    label: "Solo avisar",
    description: "Avisa pero no bloquea ni enmascara nada.",
  },
] as const;

export default function DlpStepPage() {
  const [preset, setPreset] = useState<string>("balanced");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleNext() {
    setSaving(true);
    setError(null);
    const token = await getAccessToken();
    if (!token) {
      setError("Sesión no encontrada");
      setSaving(false);
      return;
    }
    const response = await apiFetch("/api/onboarding/dlp-preset", token, {
      method: "POST",
      body: JSON.stringify({ preset }),
    });
    if (!response.ok) {
      setError("No se pudo guardar la política de DLP");
      setSaving(false);
      return;
    }
    window.location.href = "/start/invite";
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center p-6">
      <WizardProgress current={3} />
      <h1 className="mb-1 text-xl font-semibold text-foreground">
        Protección de datos sensibles
      </h1>
      <p className="mb-6 text-sm text-muted">
        Se puede cambiar luego en Ajustes → DLP, por división si hace falta.
      </p>

      <div className="flex flex-col gap-2">
        {PRESETS.map((p) => (
          <label
            key={p.id}
            className={`flex cursor-pointer flex-col gap-1 rounded border p-3 text-sm ${
              preset === p.id ? "border-primary" : "border-border"
            }`}
          >
            <span className="flex items-center gap-2 font-medium text-foreground">
              <input
                type="radio"
                name="dlp-preset"
                checked={preset === p.id}
                onChange={() => setPreset(p.id)}
              />
              {p.label}
            </span>
            <span className="text-muted">{p.description}</span>
          </label>
        ))}
      </div>

      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      <button
        type="button"
        onClick={handleNext}
        disabled={saving}
        className="mt-6 rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50"
      >
        {saving ? "Guardando…" : "Siguiente"}
      </button>
    </main>
  );
}
