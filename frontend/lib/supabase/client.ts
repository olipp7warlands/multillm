import { createBrowserClient } from "@supabase/ssr";

/**
 * La sesión se guarda en cookie con dominio = BASE_DOMAIN (no en localStorage,
 * que es por-origen) para que sobreviva el salto de host que hace el wizard:
 * empieza en un host sin tenant (recién registrado, todavía no hay subdominio)
 * y a partir del paso 2 continúa en `<slug>.BASE_DOMAIN` — mismo dominio base,
 * cookie compartida entre subdominios.
 */
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookieOptions: {
        domain: process.env.NEXT_PUBLIC_BASE_DOMAIN,
        sameSite: "lax",
      },
    },
  );
}
