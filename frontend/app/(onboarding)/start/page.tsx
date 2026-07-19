"use client";

import { useState, type FormEvent } from "react";

import { WizardProgress } from "@/components/WizardProgress";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/session";

export default function StartPage() {
  const [ownerName, setOwnerName] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [slug, setSlug] = useState("");
  const [logoUrl, setLogoUrl] = useState("");
  const [colorPrimary, setColorPrimary] = useState("#171717");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);

    const token = await getAccessToken();
    if (!token) {
      setError("Sesión no encontrada — crea una cuenta primero");
      setLoading(false);
      return;
    }

    const response = await apiFetch("/api/auth/register-tenant", token, {
      method: "POST",
      body: JSON.stringify({
        slug,
        tenant_name: tenantName,
        billing_mode: "reseller",
        owner_name: ownerName,
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      setError(body.detail ?? "No se pudo crear el espacio");
      setLoading(false);
      return;
    }

    // TODO(S2-5): logoUrl/colorPrimary se guardan en tenant_branding vía un
    // endpoint de branding propio — de momento el wizard los recoge pero no
    // hay servicio que los persista todavía.
    void logoUrl;
    void colorPrimary;

    const port = window.location.port ? `:${window.location.port}` : "";
    window.location.href = `${window.location.protocol}//${slug}.${process.env.NEXT_PUBLIC_BASE_DOMAIN}${port}/start/models`;
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center p-6">
      <WizardProgress current={1} />
      <h1 className="mb-1 text-xl font-semibold text-foreground">Crea tu espacio</h1>
      <p className="mb-6 text-sm text-muted">
        Esto es lo único que hace falta para empezar — el resto se puede ajustar luego.
      </p>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <input
          type="text"
          required
          placeholder="Tu nombre"
          value={ownerName}
          onChange={(event) => setOwnerName(event.target.value)}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        <input
          type="text"
          required
          placeholder="Nombre de la empresa"
          value={tenantName}
          onChange={(event) => setTenantName(event.target.value)}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        <input
          type="text"
          required
          pattern="[a-z0-9\-]+"
          placeholder="Subdominio (slug)"
          value={slug}
          onChange={(event) => setSlug(event.target.value.toLowerCase())}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        <input
          type="url"
          placeholder="Logo (URL, opcional)"
          value={logoUrl}
          onChange={(event) => setLogoUrl(event.target.value)}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        <label className="flex items-center gap-3 text-sm text-muted">
          Color principal
          <input
            type="color"
            value={colorPrimary}
            onChange={(event) => setColorPrimary(event.target.value)}
            className="h-8 w-12 rounded border border-border"
          />
        </label>
        {error && <p className="text-sm text-danger">{error}</p>}
        <button
          type="submit"
          disabled={loading}
          className="mt-2 rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50"
        >
          {loading ? "Creando…" : "Siguiente"}
        </button>
      </form>
    </main>
  );
}
