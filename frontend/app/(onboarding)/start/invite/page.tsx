"use client";

import { useState } from "react";

import { WizardProgress } from "@/components/WizardProgress";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/session";

function parseEmails(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((e) => e.trim())
    .filter(Boolean);
}

export default function InviteStepPage() {
  const [rawEmails, setRawEmails] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function finish() {
    const token = await getAccessToken();
    if (!token) {
      setError("Sesión no encontrada");
      setSaving(false);
      return;
    }
    const response = await apiFetch("/api/onboarding/complete", token, { method: "POST" });
    if (!response.ok) {
      setError("No se pudo completar el alta");
      setSaving(false);
      return;
    }
    window.location.href = "/chat";
  }

  async function handleInviteAndFinish() {
    setSaving(true);
    setError(null);
    const emails = parseEmails(rawEmails);
    if (emails.length > 0) {
      const token = await getAccessToken();
      if (!token) {
        setError("Sesión no encontrada");
        setSaving(false);
        return;
      }
      const response = await apiFetch("/api/onboarding/invite-team", token, {
        method: "POST",
        body: JSON.stringify({ emails }),
      });
      if (!response.ok) {
        setError("No se pudieron enviar las invitaciones");
        setSaving(false);
        return;
      }
    }
    await finish();
  }

  async function handleSkip() {
    setSaving(true);
    setError(null);
    await finish();
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center p-6">
      <WizardProgress current={4} />
      <h1 className="mb-1 text-xl font-semibold text-foreground">Invita a tu equipo</h1>
      <p className="mb-6 text-sm text-muted">
        Un email por línea (o separados por comas). Se puede hacer luego desde Ajustes.
      </p>

      <textarea
        value={rawEmails}
        onChange={(event) => setRawEmails(event.target.value)}
        rows={5}
        placeholder={"ana@empresa.com\nluis@empresa.com"}
        className="rounded border border-border bg-background px-3 py-2 text-foreground"
      />

      {error && <p className="mt-3 text-sm text-danger">{error}</p>}

      <div className="mt-6 flex gap-3">
        <button
          type="button"
          onClick={handleSkip}
          disabled={saving}
          className="rounded border border-border px-3 py-2 text-foreground disabled:opacity-50"
        >
          Saltar
        </button>
        <button
          type="button"
          onClick={handleInviteAndFinish}
          disabled={saving}
          className="flex-1 rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50"
        >
          {saving ? "Terminando…" : "Invitar y terminar"}
        </button>
      </div>
    </main>
  );
}
