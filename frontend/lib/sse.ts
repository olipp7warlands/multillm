/**
 * Parser SSE a mano sobre `response.body` — `EventSource` no sirve aquí
 * (no soporta POST ni headers custom, y `/api/chat/stream` es un POST
 * autenticado). Formato simétrico al que emite `gateway._sse()` en el
 * backend: líneas `data: {json}\n\n`.
 */
export async function* readSSE<T>(response: Response): AsyncGenerator<T> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex = buffer.indexOf("\n\n");
    while (separatorIndex !== -1) {
      const rawEvent = buffer.slice(0, separatorIndex).trim();
      buffer = buffer.slice(separatorIndex + 2);
      if (rawEvent.startsWith("data:")) {
        const jsonText = rawEvent.slice("data:".length).trim();
        if (jsonText) yield JSON.parse(jsonText) as T;
      }
      separatorIndex = buffer.indexOf("\n\n");
    }
  }
}
