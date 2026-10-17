"use client"

import { useState } from "react"
import { Check, ShieldCheck, Pencil } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { TimelineItem } from "@/lib/chat-state"
import { SourceTag } from "./source-chip"

type ConfirmItem = Extract<TimelineItem, { kind: "confirm" }>

export function ConfirmCard({
  item,
  disabled,
  onSubmit,
}: {
  item: ConfirmItem
  disabled: boolean
  onSubmit: (confirmed: boolean, correction?: string) => void
}) {
  const { confirm } = item
  const [correcting, setCorrecting] = useState(false)
  const [correction, setCorrection] = useState("")
  const submitting = item.state === "submitting"
  const locked = submitting || disabled
  const done = item.state === "answered"

  return (
    <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
      <div className="flex items-center gap-2 border-b bg-muted/40 px-4 py-3">
        <ShieldCheck className="size-4 text-primary" />
        <span className="font-medium">{confirm.title}</span>
        {done && (
          <span className="ml-auto flex items-center gap-1 text-sm text-muted-foreground">
            {item.confirmed ? (
              <>
                <Check className="size-4 text-emerald-600" /> Confirmed
              </>
            ) : (
              <>
                <Pencil className="size-3.5" /> Corrected
              </>
            )}
          </span>
        )}
      </div>

      <dl className="divide-y text-sm">
        {confirm.items.map((row) => (
          <div key={row.label} className="flex items-center gap-3 px-4 py-2">
            <dt className="w-36 shrink-0 text-muted-foreground">{row.label}</dt>
            <dd className="flex-1">{row.value}</dd>
            <SourceTag
              kind={row.source}
              className={row.source === "inferred" ? "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200" : undefined}
            />
          </div>
        ))}
      </dl>

      {done && !item.confirmed && item.correction && (
        <div className="border-t px-4 py-2 text-sm text-muted-foreground">“{item.correction}”</div>
      )}

      {!done && (
        <div className="space-y-2 border-t px-4 py-3">
          {correcting && (
            <input
              autoFocus
              value={correction}
              onChange={(e) => setCorrection(e.target.value)}
              placeholder="What should change? e.g. 4 specialist visits, not 2"
              className="h-9 w-full rounded-lg border bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
              disabled={locked}
            />
          )}
          <div className="flex items-center gap-2">
            {item.error && <span className="text-sm text-destructive">{item.error}</span>}
            <div className="ml-auto flex gap-2">
              {correcting ? (
                <>
                  <Button variant="ghost" onClick={() => setCorrecting(false)} disabled={locked}>
                    Cancel
                  </Button>
                  <Button
                    variant="outline"
                    disabled={locked || !correction.trim()}
                    onClick={() => onSubmit(false, correction.trim())}
                  >
                    {submitting ? "Sending…" : "Send correction"}
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="outline" onClick={() => setCorrecting(true)} disabled={locked}>
                    Something&apos;s off
                  </Button>
                  <Button onClick={() => onSubmit(true)} disabled={locked}>
                    {submitting ? "Calculating…" : "Looks right"}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
