const STEPS = ["Espacio", "Modelos", "DLP", "Equipo"];

export function WizardProgress({ current }: { current: number }) {
  return (
    <ol className="mb-8 flex items-center gap-2">
      {STEPS.map((label, index) => {
        const step = index + 1;
        const isActive = step === current;
        const isDone = step < current;
        return (
          <li key={label} className="flex flex-1 items-center gap-2">
            <span
              className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-medium ${
                isDone
                  ? "bg-success text-primary-foreground"
                  : isActive
                    ? "bg-primary text-primary-foreground"
                    : "bg-surface text-muted"
              }`}
            >
              {isDone ? "✓" : step}
            </span>
            <span className={`text-sm ${isActive ? "text-foreground" : "text-muted"}`}>
              {label}
            </span>
            {step < STEPS.length && <span className="h-px flex-1 bg-border" />}
          </li>
        );
      })}
    </ol>
  );
}
