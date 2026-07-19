"use client";

import { useState, type FormEvent } from "react";

import { createClient } from "@/lib/supabase/client";

export default function SignupPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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
      // el proyecto exige confirmar el email antes de dar sesión (o el rate
      // limit de envío de Supabase bloqueó el correo) — no hay nada que
      // completar aquí hasta que exista una sesión real.
      setPendingConfirmation(true);
      setLoading(false);
      return;
    }

    // el alta del espacio (tenant + owner + división) es el paso 1 del
    // wizard, no de aquí — ver app/(onboarding)/start
    window.location.href = "/start";
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
      <h1 className="text-xl font-semibold text-foreground">Crea tu cuenta</h1>
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
          minLength={8}
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
          {loading ? "Creando…" : "Continuar"}
        </button>
      </form>
      <a href="/login" className="text-sm text-foreground underline">
        ¿Ya tienes cuenta? Entra
      </a>
    </main>
  );
}
