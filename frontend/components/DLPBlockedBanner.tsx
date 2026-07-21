import type { EntitiesSummary } from "@/components/DLPInterstitial";

type DLPBlockedBannerProps = {
  entitiesSummary: EntitiesSummary;
};

export function DLPBlockedBanner({ entitiesSummary }: DLPBlockedBannerProps) {
  return (
    <div className="rounded border border-danger bg-background p-4">
      <p className="text-sm font-medium text-danger">
        Este mensaje contiene información que la política de tu empresa no permite enviar.
      </p>
      <p className="mt-1 text-xs text-muted">
        {Object.entries(entitiesSummary)
          .map(([type, count]) => `${type}: ${count}`)
          .join(" · ")}
      </p>
      <p className="mt-2 text-xs text-muted">
        Edita el mensaje para quitar esa información y vuelve a intentarlo.
      </p>
    </div>
  );
}
