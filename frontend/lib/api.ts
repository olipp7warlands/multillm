/**
 * El backend resuelve el tenant por el header Host de la petición
 * (TenantResolver, S1-4) — así que en dev hay que llamarlo por el mismo
 * subdominio que el frontend (`<slug>.lvh.me`), solo que al puerto del
 * backend, no por NEXT_PUBLIC_API_BASE_URL a secas (eso perdería el
 * subdominio). En producción, con front y back en el mismo dominio
 * público, esto se simplifica a una ruta relativa.
 */
function apiUrl(path: string): string {
  if (typeof window !== "undefined") {
    const backendPort = new URL(process.env.NEXT_PUBLIC_API_BASE_URL!).port;
    return `${window.location.protocol}//${window.location.hostname}:${backendPort}${path}`;
  }
  return `${process.env.NEXT_PUBLIC_API_BASE_URL}${path}`;
}

export async function apiFetch(
  path: string,
  accessToken: string,
  init?: RequestInit,
): Promise<Response> {
  return fetch(apiUrl(path), {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
  });
}
