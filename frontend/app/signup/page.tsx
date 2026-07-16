"use client";

import { useState, type FormEvent } from "react";

import { apiFetch } from "@/lib/api";
import { createClient } from "@/lib/supabase/client";

export default function SignupPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [ownerName, setOwnerName] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [slug, setSlug] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState(false);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);

    const supabase = createClient();
    const { data, error: signUpError } = await supabase.auth.signUp({ email, password });
    if (signUpError) {
      setError(signUpError.message);
      setLoading(false);
      return;
    }

    if (!data.session) {
      // el proyecto de Supabase exige confirmar el email antes de dar sesión —
      // el alta del tenant (register-tenant) se completa en el primer login
      // tras confirmar (TODO: flujo completo en el wizard de S1-6/S1-7).
      setPendingConfirmation(true);
      setLoading(false);
      return;
    }

    const response = await apiFetch(
      "/api/auth/register-tenant",
      data.session.access_token,
      {
        method: "POST",
        body: JSON.stringify({
          slug,
          tenant_name: tenantName,
          billing_mode: "reseller",
          owner_name: ownerName,
        }),
      },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      setError(body.detail ?? "No se pudo crear el espacio");
      setLoading(false);
      return;
    }

    window.location.href = `http://${slug}.lvh.me:3000/login`;
  }

  if (pendingConfirmation) {
    return (
      <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
        <p className="text-foreground">
          Te hemos enviado un email de confirmación a {email}. Confirma tu cuenta y
          vuelve a intentarlo.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-xl font-semibold text-foreground">Crea tu espacio</h1>
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
          type="email"
          required
          placeholder="Email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        <input
          type="password"
          required
          minLength={8}
          placeholder="Contraseña"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
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
          pattern="[a-z0-9-]+"
          placeholder="Subdominio (slug)"
          value={slug}
          onChange={(event) => setSlug(event.target.value.toLowerCase())}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        {error && <p className="text-sm text-danger">{error}</p>}
        <button
          type="submit"
          disabled={loading}
          className="rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50"
        >
          {loading ? "Creando…" : "Crear espacio"}
        </button>
      </form>
      <a href="/login" className="text-sm text-foreground underline">
        ¿Ya tienes cuenta? Entra
      </a>
    </main>
  );
}
