export type ChatContext = {
  division_name: string;
  billing_mode: string;
  dlp_mode: string;
  wallet_available: string | null;
  division_allocated: string | null;
  division_consumed: string | null;
};

const DLP_MODE_LABEL: Record<string, string> = {
  block: "Bloqueo",
  mask: "Enmascarado",
  warn: "Solo aviso",
};

type ContextBarProps = {
  context: ChatContext;
};

export function ContextBar({ context }: ContextBarProps) {
  const showCredits = context.billing_mode !== "byok" && context.wallet_available != null;

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border bg-surface px-4 py-2 text-sm text-muted">
      <span className="font-medium text-foreground">{context.division_name}</span>
      <span>·</span>
      {showCredits ? (
        context.division_allocated != null ? (
          <span>
            {context.division_consumed} / {context.division_allocated} créditos usados
          </span>
        ) : (
          <span>{context.wallet_available} créditos disponibles</span>
        )
      ) : (
        <span className="rounded bg-primary px-2 py-0.5 text-xs text-primary-foreground">
          BYOK
        </span>
      )}
      <span>·</span>
      <span>DLP: {DLP_MODE_LABEL[context.dlp_mode] ?? context.dlp_mode}</span>
    </div>
  );
}
