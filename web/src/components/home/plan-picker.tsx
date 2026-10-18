"use client"

import { useState } from "react"
import { ArrowRight, Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import type { Plan } from "@/lib/protocol"
import { cn } from "@/lib/utils"

export type PlansState = { status: "loading" } | { status: "error" } | { status: "ready"; plans: Plan[] }

function PlansStatus({ state }: { state: PlansState }) {
  if (state.status === "loading") {
    return (
      <p className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" /> Loading plans…
      </p>
    )
  }
  if (state.status === "error") {
    return <p className="py-2 text-xs text-destructive">Couldn&apos;t load the plan library. You can still type below.</p>
  }
  return <p className="py-2 text-xs text-muted-foreground">No plans in the library yet.</p>
}

// Calculate: one click on a plan starts the conversation.
export function SinglePlanPicker({
  state,
  disabled,
  onPick,
}: {
  state: PlansState
  disabled?: boolean
  onPick: (plan: Plan) => void
}) {
  if (state.status !== "ready" || state.plans.length === 0) return <PlansStatus state={state} />
  return (
    <ul className="space-y-1.5">
      {state.plans.map((plan) => (
        <li key={plan.plan_id}>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onPick(plan)}
            className="group flex w-full items-center gap-3 rounded-lg border border-border bg-background px-3 py-2 text-left transition-colors hover:border-foreground/40 disabled:pointer-events-none disabled:opacity-50"
          >
            <PlanLabel plan={plan} />
            <ArrowRight className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
          </button>
        </li>
      ))}
    </ul>
  )
}

// Review: tick the plans to compare; all are ticked to start.
export function MultiPlanPicker({
  state,
  disabled,
  onCompare,
}: {
  state: PlansState
  disabled?: boolean
  onCompare: (plans: Plan[]) => void
}) {
  const [unticked, setUnticked] = useState<Set<string>>(new Set())
  if (state.status !== "ready" || state.plans.length === 0) return <PlansStatus state={state} />
  const chosen = state.plans.filter((p) => !unticked.has(p.plan_id))
  const toggle = (id: string, on: boolean) =>
    setUnticked((prev) => {
      const next = new Set(prev)
      if (on) next.delete(id)
      else next.add(id)
      return next
    })
  return (
    <div className="space-y-2">
      <ul className="space-y-1.5">
        {state.plans.map((plan) => {
          const on = !unticked.has(plan.plan_id)
          return (
            <li key={plan.plan_id}>
              <label
                className={cn(
                  "flex cursor-pointer items-center gap-3 rounded-lg border bg-background px-3 py-2 transition-colors",
                  on ? "border-foreground/40" : "border-border",
                )}
              >
                <Checkbox checked={on} onCheckedChange={(value) => toggle(plan.plan_id, value)} />
                <PlanLabel plan={plan} />
              </label>
            </li>
          )
        })}
      </ul>
      <Button
        type="button"
        className="w-full"
        size="lg"
        disabled={disabled || chosen.length === 0}
        onClick={() => onCompare(chosen)}
      >
        Compare {chosen.length} {chosen.length === 1 ? "plan" : "plans"} for me
        <ArrowRight />
      </Button>
    </div>
  )
}

function PlanLabel({ plan }: { plan: Plan }) {
  return (
    <span className="min-w-0 leading-tight">
      <span className="block truncate text-sm font-medium">{plan.name}</span>
      <span className="block truncate text-xs text-muted-foreground">{plan.insurer}</span>
    </span>
  )
}
