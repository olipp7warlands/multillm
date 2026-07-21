"use client";

export type ChatModelPrice = { unit: string; credit_price: number };

export type ChatModel = {
  id: string;
  slug: string;
  display_name: string;
  provider_slug: string;
  provider_name: string;
  min_role: string | null;
  allowed: boolean;
  prices: ChatModelPrice[];
};

function formatPrices(prices: ChatModelPrice[]): string {
  return prices
    .map((p) => `${p.credit_price} créditos / ${p.unit.replace("1k_tokens_", "1K tokens ")}`)
    .join(" · ");
}

// min_role puede ser cualquiera de los cuatro roles, no solo "admin" — el
// texto de restricción tiene que reflejar el rol real exigido, no un
// "Solo admins" fijo que mentiría en los otros casos.
const ROLE_LABEL: Record<string, string> = {
  user: "usuario",
  division_admin: "admin de división",
  admin: "admin",
  owner: "owner",
};

type ModelSelectorProps = {
  models: ChatModel[];
  selectedId: string | null;
  onSelect: (id: string) => void;
};

export function ModelSelector({ models, selectedId, onSelect }: ModelSelectorProps) {
  const byProvider = new Map<string, ChatModel[]>();
  for (const model of models) {
    const list = byProvider.get(model.provider_name) ?? [];
    list.push(model);
    byProvider.set(model.provider_name, list);
  }

  return (
    <select
      value={selectedId ?? ""}
      onChange={(event) => onSelect(event.target.value)}
      className="rounded border border-border bg-background px-3 py-2 text-sm text-foreground"
    >
      <option value="" disabled>
        Elige un modelo
      </option>
      {Array.from(byProvider.entries()).map(([providerName, providerModels]) => (
        <optgroup key={providerName} label={providerName}>
          {providerModels.map((model) => (
            <option key={model.id} value={model.id} disabled={!model.allowed}>
              {model.display_name} — {formatPrices(model.prices)}
              {!model.allowed && model.min_role
                ? ` (requiere rol ${ROLE_LABEL[model.min_role] ?? model.min_role})`
                : ""}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}
