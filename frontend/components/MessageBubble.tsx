export type MessageBubbleProps = {
  role: "user" | "assistant";
  content: string;
  modelDisplayName?: string;
  creditsCharged?: string | null;
  streaming?: boolean;
  failed?: boolean;
};

export function MessageBubble({
  role,
  content,
  modelDisplayName,
  creditsCharged,
  streaming,
  failed,
}: MessageBubbleProps) {
  const isUser = role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[75%] whitespace-pre-wrap rounded px-3 py-2 text-sm ${
          isUser ? "bg-primary text-primary-foreground" : "bg-surface text-foreground"
        } ${failed ? "border border-danger" : ""}`}
      >
        <p>{content || (streaming ? "…" : "")}</p>
        {!isUser && modelDisplayName && (
          <p className="mt-1 text-xs text-muted">
            {modelDisplayName}
            {creditsCharged != null && ` · ${creditsCharged} créditos`}
          </p>
        )}
        {failed && (
          <p className="mt-1 text-xs text-danger">
            No se pudo completar — no se cobraron créditos.
          </p>
        )}
      </div>
    </div>
  );
}
