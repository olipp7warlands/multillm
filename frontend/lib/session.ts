import { createClient } from "@/lib/supabase/client";

/** Token de la sesión actual de Supabase, o null si no hay ninguna (p.ej.
 * si el usuario llega a un paso del wizard sin haber pasado por signup). */
export async function getAccessToken(): Promise<string | null> {
  const supabase = createClient();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}
