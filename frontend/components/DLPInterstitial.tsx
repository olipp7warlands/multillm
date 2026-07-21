export type EntitiesSummary = Record<string, number>;

type DLPInterstitialProps = {
  maskedText: string;
  entitiesSummary: EntitiesSummary;
  onSendMasked: () => void;
  onEdit: () => void;
  sending?: boolean;
};

function highlightPlaceholders(text: string) {
  const parts = text.split(/(<[A-Z_]+_\d+>)/g);
  return parts.map((part, index) =>
    /^<[A-Z_]+_\d+>$/.test(part) ? (
      <mark key={index} className="rounded border border-border bg-surface px-1 text-foreground">
        {part}
      </mark>
    ) : (
      <span key={index}>{part}</span>
    ),
  );
}

export function DLPInterstitial({
  maskedText,
  entitiesSummary,
  onSendMasked,
  onEdit,
  sending,
}: DLPInterstitialProps) {
  return (
    <div className="rounded border border-border bg-background p-4">
      <p className="mb-2 text-sm font-medium text-foreground">
        Se detectó información sensible — así se enviaría al modelo:
      </p>
      <p className="mb-3 whitespace-pre-wrap rounded bg-surface p-3 text-sm text-foreground">
        {highlightPlaceholders(maskedText)}
      </p>
      <p className="mb-3 text-xs text-muted">
        {Object.entries(entitiesSummary)
          .map(([type, count]) => `${type}: ${count}`)
          .join(" · ")}
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onSendMasked}
          disabled={sending}
          className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50"
        >
          Enviar enmascarado
        </button>
        <button
          type="button"
          onClick={onEdit}
          className="rounded border border-border px-3 py-2 text-sm text-foreground"
        >
          Editar
        </button>
      </div>
    </div>
  );
}
