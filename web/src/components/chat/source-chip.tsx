"use client"

import { useState } from "react"
import { FileText, Database, Globe, User, Sparkles } from "lucide-react"
import type { Source, SourceKind } from "@/lib/protocol"
import { cn } from "@/lib/utils"

const ICONS: Record<SourceKind, typeof FileText> = {
  policy: FileText,
  mrf: Database,
  web: Globe,
  user_form: User,
  user_message: User,
  inferred: Sparkles,
}

export const SOURCE_LABELS: Record<SourceKind, string> = {
  policy: "Policy",
  mrf: "Price DB",
  web: "Web",
  user_form: "Your answer",
  user_message: "You said",
  inferred: "My guess",
}

export function SourceTag({ kind, className }: { kind: SourceKind; className?: string }) {
  const Icon = ICONS[kind]
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground",
        className,
      )}
    >
      <Icon className="size-3" />
      {SOURCE_LABELS[kind]}
    </span>
  )
}

// Click to reveal where a number came from: page, quote, link.
export function SourceChip({ source }: { source: Source }) {
  const [open, setOpen] = useState(false)
  const Icon = ICONS[source.kind]
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation()
          setOpen((value) => !value)
        }}
        className="inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted"
        aria-expanded={open}
      >
        <Icon className="size-3" />
        {source.page ? `p.${source.page}` : SOURCE_LABELS[source.kind]}
      </button>
      {open && (
        <span
          className="absolute right-0 z-20 mt-1 block w-64 rounded-lg border bg-popover p-3 text-left text-xs text-popover-foreground shadow-md"
          onClick={(event) => event.stopPropagation()}
        >
          <span className="block font-medium">{source.label}</span>
          {source.page != null && (
            <span className="block text-muted-foreground">Page {source.page}</span>
          )}
          {source.quote && (
            <span className="mt-1.5 block border-l-2 pl-2 italic text-muted-foreground">
              “{source.quote}”
            </span>
          )}
          {source.url && (
            <a
              href={source.url}
              target="_blank"
              rel="noreferrer"
              className="mt-1.5 block truncate text-primary underline"
            >
              {source.url}
            </a>
          )}
        </span>
      )}
    </span>
  )
}

export function usd(amount: number) {
  return amount.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: amount % 1 === 0 ? 0 : 2,
  })
}

export function costText(cost: { low?: number; mid: number; high?: number }) {
  if (cost.low != null && cost.high != null && cost.low !== cost.high) {
    return `${usd(cost.low)}–${usd(cost.high)}`
  }
  return usd(cost.mid)
}
