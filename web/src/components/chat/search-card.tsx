import { CheckCircle2, Loader2, Monitor, XCircle, Search } from "lucide-react"
import type { SearchStep } from "@/lib/protocol"

export function SearchCard({ steps, onOpenComputer }: { steps: SearchStep[]; onOpenComputer?: () => void }) {
  const running = steps.some((s) => s.status === "running")
  return (
    <div className="rounded-xl border bg-card px-4 py-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <Search className="size-4 text-primary" />
        {running ? "Searching the web…" : "Web search"}
        {onOpenComputer && steps.some((s) => s.screenshot) && (
          <button
            onClick={onOpenComputer}
            className="ml-auto inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-normal text-muted-foreground hover:bg-muted"
          >
            <Monitor className="size-3.5" /> Watch
          </button>
        )}
      </div>
      <ol className="space-y-1.5">
        {steps.map((step) => (
          <li key={step.step_id} className="flex items-start gap-2 text-sm">
            {step.status === "running" && <Loader2 className="mt-0.5 size-4 animate-spin text-primary" />}
            {step.status === "done" && <CheckCircle2 className="mt-0.5 size-4 text-emerald-600" />}
            {step.status === "failed" && <XCircle className="mt-0.5 size-4 text-destructive" />}
            <div className="min-w-0">
              <div className={step.status === "running" ? "" : "text-muted-foreground"}>{step.text}</div>
              {step.url && (
                <a
                  href={step.url}
                  target="_blank"
                  rel="noreferrer"
                  className="block truncate text-xs text-primary/80 hover:underline"
                >
                  {step.url}
                </a>
              )}
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}
