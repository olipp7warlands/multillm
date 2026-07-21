"use client";

import { useEffect, useRef, useState } from "react";

import { ChatInput } from "@/components/ChatInput";
import { ContextBar, type ChatContext } from "@/components/ContextBar";
import { DLPBlockedBanner } from "@/components/DLPBlockedBanner";
import { DLPInterstitial, type EntitiesSummary } from "@/components/DLPInterstitial";
import { MessageBubble } from "@/components/MessageBubble";
import { ModelSelector, type ChatModel } from "@/components/ModelSelector";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/session";
import { readSSE } from "@/lib/sse";

// crypto.randomUUID() exige un contexto seguro (HTTPS o localhost) — la
// convención de dev de este proyecto es http://<slug>.lvh.me, que NO lo
// es (aunque resuelva a loopback), así que crypto.randomUUID lanza ahí.
// Estos IDs son solo para `key` de React / seguimiento local, no hace
// falta que sean criptográficamente aleatorios.
let messageIdCounter = 0;
function nextMessageId(): string {
  messageIdCounter += 1;
  return `msg-${Date.now()}-${messageIdCounter}`;
}

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  modelDisplayName?: string;
  creditsCharged?: string | null;
  streaming?: boolean;
  failed?: boolean;
};

type PolicyError = { reason: string; detail: string };

type PendingMasked = {
  originalText: string;
  maskedText: string;
  entitiesSummary: EntitiesSummary;
};

// Mismos tres eventos que emite gateway.stream_chat() (backend) por SSE.
type ChatChunkEvent = { type: "chunk"; content: string };
type ChatDoneEvent = {
  type: "done";
  conversation_id: string;
  credits_charged: string | null;
  wallet_balance: string | null;
};
type ChatErrorEvent = { type: "error"; detail: string };
type ChatStreamEvent = ChatChunkEvent | ChatDoneEvent | ChatErrorEvent;

type CurrentUser = {
  user_id: string;
  tenant_id: string;
  email: string;
  name: string;
  role: string;
  division_id: string;
};

export function ChatWindow() {
  const [ready, setReady] = useState(false);
  const [signedIn, setSignedIn] = useState(true);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [models, setModels] = useState<ChatModel[]>([]);
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
  const [context, setContext] = useState<ChatContext | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draftText, setDraftText] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [pendingMasked, setPendingMasked] = useState<PendingMasked | null>(null);
  const [blockedNotice, setBlockedNotice] = useState<EntitiesSummary | null>(null);
  const [policyError, setPolicyError] = useState<PolicyError | null>(null);
  const conversationIdRef = useRef<string | null>(null);

  useEffect(() => {
    async function bootstrap() {
      const token = await getAccessToken();
      if (!token) {
        setSignedIn(false);
        setReady(true);
        return;
      }

      const [meResponse, modelsResponse, contextResponse] = await Promise.all([
        apiFetch("/api/me", token),
        apiFetch("/api/chat/models", token),
        apiFetch("/api/chat/context", token),
      ]);

      if (!meResponse.ok) {
        // Token de Supabase válido pero sin membership en este tenant
        // (host equivocado, invitación no aceptada, etc.) — mismo estado
        // que "no autenticado" desde el punto de vista de esta pantalla.
        setSignedIn(false);
        setReady(true);
        return;
      }
      setCurrentUser(await meResponse.json());

      if (modelsResponse.ok) {
        const body: { models: ChatModel[] } = await modelsResponse.json();
        setModels(body.models);
        const firstAllowed = body.models.find((model) => model.allowed);
        if (firstAllowed) setSelectedModelId(firstAllowed.id);
      }
      if (contextResponse.ok) {
        setContext(await contextResponse.json());
      }
      setReady(true);
    }
    bootstrap();
  }, []);

  async function sendMessage(text: string, options?: { confirmMasked?: boolean }) {
    if (!selectedModelId || !text.trim()) return;
    setPolicyError(null);
    setBlockedNotice(null);
    setStreaming(true);

    const token = await getAccessToken();
    if (!token) {
      setSignedIn(false);
      setStreaming(false);
      return;
    }

    const response = await apiFetch("/api/chat/stream", token, {
      method: "POST",
      body: JSON.stringify({
        model_id: selectedModelId,
        message: text,
        conversation_id: conversationIdRef.current,
        confirm_masked: options?.confirmMasked ?? false,
      }),
    });

    if (response.status === 403) {
      const body = await response.json().catch(() => ({}));
      setPolicyError({
        reason: body.reason ?? "denied",
        detail: body.detail ?? "Petición denegada",
      });
      setStreaming(false);
      return;
    }
    if (response.status === 409) {
      const body = await response.json().catch(() => ({}));
      setPendingMasked({
        originalText: text,
        maskedText: body.masked_text ?? "",
        entitiesSummary: body.entities_summary ?? {},
      });
      setStreaming(false);
      return;
    }
    if (response.status === 422) {
      const body = await response.json().catch(() => ({}));
      setBlockedNotice(body.entities_summary ?? {});
      setStreaming(false);
      return;
    }
    if (!response.ok) {
      setPolicyError({ reason: "unknown", detail: "No se pudo enviar el mensaje" });
      setStreaming(false);
      return;
    }

    setPendingMasked(null);
    setDraftText("");
    const model = models.find((m) => m.id === selectedModelId);
    const userMessageId = nextMessageId();
    const assistantMessageId = nextMessageId();
    setMessages((prev) => [
      ...prev,
      { id: userMessageId, role: "user", content: text },
      {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        modelDisplayName: model?.display_name,
        streaming: true,
      },
    ]);

    for await (const event of readSSE<ChatStreamEvent>(response)) {
      if (event.type === "chunk") {
        const chunk = event.content;
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMessageId ? { ...m, content: m.content + chunk } : m)),
        );
      } else if (event.type === "done") {
        conversationIdRef.current = event.conversation_id;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessageId
              ? { ...m, streaming: false, creditsCharged: event.credits_charged }
              : m,
          ),
        );
        const walletBalance = event.wallet_balance;
        setContext((prev) => (prev ? { ...prev, wallet_available: walletBalance ?? prev.wallet_available } : prev));
      } else if (event.type === "error") {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMessageId ? { ...m, streaming: false, failed: true } : m)),
        );
      }
    }

    setStreaming(false);
  }

  function handleSend() {
    void sendMessage(draftText);
  }

  function handleSendMasked() {
    if (!pendingMasked) return;
    void sendMessage(pendingMasked.originalText, { confirmMasked: true });
  }

  function handleEditMasked() {
    if (!pendingMasked) return;
    setDraftText(pendingMasked.originalText);
    setPendingMasked(null);
  }

  if (!ready) {
    return <p className="p-6 text-sm text-muted">Cargando…</p>;
  }

  if (!signedIn) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 p-6">
        <p className="text-sm text-foreground">Inicia sesión para continuar.</p>
        <a href="/login" className="text-sm text-foreground underline">
          Ir a entrar
        </a>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col">
      {currentUser && (
        <div className="flex items-center justify-between px-4 pt-3">
          <p className="text-sm text-foreground">
            Hola, {currentUser.name} <span className="text-muted">· {currentUser.role}</span>
          </p>
        </div>
      )}
      {context && <ContextBar context={context} />}
      <div className="border-b border-border p-3">
        <ModelSelector models={models} selectedId={selectedModelId} onSelect={setSelectedModelId} />
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {messages.map((message) => (
          <MessageBubble key={message.id} {...message} />
        ))}
        {policyError && <p className="text-sm text-danger">{policyError.detail}</p>}
        {blockedNotice && <DLPBlockedBanner entitiesSummary={blockedNotice} />}
        {pendingMasked && (
          <DLPInterstitial
            maskedText={pendingMasked.maskedText}
            entitiesSummary={pendingMasked.entitiesSummary}
            onSendMasked={handleSendMasked}
            onEdit={handleEditMasked}
            sending={streaming}
          />
        )}
      </div>
      <ChatInput
        value={draftText}
        onChange={setDraftText}
        onSend={handleSend}
        disabled={streaming || !selectedModelId}
      />
    </div>
  );
}
