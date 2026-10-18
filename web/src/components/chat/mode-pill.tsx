"use client"

import { useEffect, useRef, useState } from "react"
import { Calculator, Check, ChevronUp, MessageCircle, Scale } from "lucide-react"
import type { Mode, Plan, TurnContext } from "@/lib/protocol"
import { cn } from "@/lib/utils"

export function contextLabel(context: TurnContext | null, plans: Plan[]) {
  if (!context) return "Just chat"
  const names = (context.plan_ids ?? []).map((id) => plans.find((p) => p.plan_id === id)?.name ?? id)
  if (context.mode === "calculate") return names.length === 1 ? `Calculate · ${names[0]}` : "Calculate"
  return names.length ? `Review · ${names.length} ${names.length === 1 ? "plan" : "plans"}` : "Review"
}

const ICON: Record<Mode, typeof Calculator> = { calculate: Calculator, review: Scale }

// Shows the active feature above the input; the menu switches it for the next message.
export function ModePill({
  context,
  plans,
  disabled,
  onChange,
}: {
  context: TurnContext | null
  plans: Plan[]
  disabled?: boolean
  onChange: (context: TurnContext | null) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", close)
    return () => document.removeEventListener("mousedown", close)
  }, [open])

  const Icon = context ? ICON[context.mode] : MessageCircle
  const options: { key: string; label: string; icon: typeof Calculator; value: TurnContext | null }[] = [
    ...plans.map((plan) => ({
      key: `calc_${plan.plan_id}`,
      label: `Calculate · ${plan.name}`,
      icon: Calculator,
      value: { mode: "calculate" as const, plan_ids: [plan.plan_id] },
    })),
    {
      key: "review_all",
      label: contextLabel({ mode: "review", plan_ids: plans.map((p) => p.plan_id) }, plans),
      icon: Scale,
      value: { mode: "review", plan_ids: plans.map((p) => p.plan_id) },
    },
    { key: "chat", label: "Just chat", icon: MessageCircle, value: null },
  ]
  const current = contextLabel(context, plans)

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs font-medium transition-colors hover:border-foreground/40 disabled:opacity-50"
      >
        <Icon className="size-3.5" />
        <span className="truncate">{current}</span>
        <ChevronUp className={cn("size-3.5 text-muted-foreground transition-transform", !open && "rotate-180")} />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-20 mb-2 w-72 rounded-lg border border-border bg-popover p-1 shadow-lg">
          <p className="px-2 pt-1.5 pb-1 text-[11px] text-muted-foreground">Applies to your next message</p>
          {options.map((option) => {
            const active = option.label === current || (option.value === null && context === null)
            return (
              <button
                key={option.key}
                type="button"
                onClick={() => {
                  onChange(option.value)
                  setOpen(false)
                }}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
              >
                <option.icon className="size-4 shrink-0 text-muted-foreground" />
                <span className="truncate">{option.label}</span>
                {active && <Check className="ml-auto size-4 shrink-0" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
