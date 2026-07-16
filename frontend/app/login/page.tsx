"use client";

import { useState, type FormEvent } from "react";

import { apiFetch } from "@/lib/api";
import { createClient } from "@/lib/supabase/client";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);

    const supabase = createClient();
    const { data, error: signInError } = await supabase.auth.signInWithPassword({
      email,
      password,
    });
    if (signInError || !data.session) {
      setError(signInError?.message ?? "No se pudo iniciar sesión");
      setLoading(false);
      return;
    }

    const response = await apiFetch("/api/auth/login", data.session.access_token, {
      method: "POST",
    });
    if (!response.ok) {
      setError("Sesión válida en Supabase, pero el backend la rechazó");
      setLoading(false);
      return;
    }

    // TODO(S1-12): guardar el perfil resuelto (rol, división) y redirigir a /chat
    window.location.href = "/";
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-xl font-semibold text-foreground">Entrar</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
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
          placeholder="Contraseña"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="rounded border border-border bg-background px-3 py-2 text-foreground"
        />
        {error && <p className="text-sm text-danger">{error}</p>}
        <button
          type="submit"
          disabled={loading}
          className="rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50"
        >
          {loading ? "Entrando…" : "Entrar"}
        </button>
      </form>
      <a href="/signup" className="text-sm text-foreground underline">
        ¿No tienes cuenta? Crea tu espacio
      </a>
    </main>
  );
}
