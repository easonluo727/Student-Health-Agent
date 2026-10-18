"use client"

import { useState } from "react"
import { Calculator, ChevronDown, FileUp, Library, Scale } from "lucide-react"
import type { Mode, Plan } from "@/lib/protocol"
import { cn } from "@/lib/utils"
import { MultiPlanPicker, SinglePlanPicker, type PlansState } from "./plan-picker"

const EXAMPLES: Record<Mode, string[]> = {
  calculate: ["How much would an ER visit for a sprained ankle cost?", "What do I pay for a therapy session?"],
  review: ["Which plan is best if I see a specialist every month?", "I want the lowest cost in a bad year."],
}

export function HomeScreen({
  plans,
  busy,
  composer,
  onCalculatePlan,
  onUpload,
  onReview,
  onExample,
}: {
  plans: PlansState
  busy: boolean
  composer: React.ReactNode
  onCalculatePlan: (plan: Plan) => void
  onUpload: () => void
  onReview: (plans: Plan[]) => void
  onExample: (text: string, mode: Mode) => void
}) {
  const [open, setOpen] = useState<"calculate" | "review" | null>(null)
  const toggle = (card: "calculate" | "review") => setOpen((prev) => (prev === card ? null : card))

  return (
    <div className="animate-fade-up mx-auto flex w-full max-w-3xl flex-col items-center px-4 pt-[12vh] pb-10">
      <p className="font-mono text-[11px] tracking-[0.25em] text-muted-foreground uppercase">
        Student Insurance Agent
      </p>
      <h1 className="mt-3 text-center text-3xl leading-tight font-semibold tracking-tight sm:text-4xl">
        Know what your care costs,
        <br />
        and which plan fits you.
      </h1>
      <p className="mt-3 max-w-md text-center text-sm leading-relaxed text-muted-foreground">
        Every number comes from a policy page or a real negotiated rate, computed by code, not guessed by AI.
      </p>

      <div className="mt-8 w-full max-w-2xl">{composer}</div>

      <div className="mt-6 grid w-full max-w-2xl gap-3 sm:grid-cols-2">
        <FeatureCard
          icon={<Calculator className="size-5" />}
          name="Calculate"
          question="What will a visit cost on my plan?"
          examples={EXAMPLES.calculate}
          busy={busy}
          onExample={(text) => onExample(text, "calculate")}
        >
          <div className="grid gap-2">
            <ActionButton
              icon={<Library className="size-4" />}
              label="Use a plan we have"
              expanded={open === "calculate"}
              onClick={() => toggle("calculate")}
            />
            {open === "calculate" && <SinglePlanPicker state={plans} disabled={busy} onPick={onCalculatePlan} />}
            <ActionButton
              icon={<FileUp className="size-4" />}
              label="Upload my plan PDF"
              disabled={busy}
              onClick={onUpload}
            />
          </div>
        </FeatureCard>

        <FeatureCard
          icon={<Scale className="size-5" />}
          name="Review"
          question="Which plan in our library fits me best?"
          examples={EXAMPLES.review}
          busy={busy}
          onExample={(text) => onExample(text, "review")}
        >
          <div className="grid gap-2">
            <ActionButton
              icon={<Scale className="size-4" />}
              label="Choose plans to compare"
              expanded={open === "review"}
              onClick={() => toggle("review")}
            />
            {open === "review" && <MultiPlanPicker state={plans} disabled={busy} onCompare={onReview} />}
          </div>
        </FeatureCard>
      </div>
    </div>
  )
}

function FeatureCard({
  icon,
  name,
  question,
  examples,
  busy,
  onExample,
  children,
}: {
  icon: React.ReactNode
  name: string
  question: string
  examples: string[]
  busy: boolean
  onExample: (text: string) => void
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col rounded-xl border border-border bg-card p-4 shadow-sm">
      <div className="flex items-center gap-3">
        <span className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          {icon}
        </span>
        <div className="leading-tight">
          <h2 className="font-semibold tracking-tight">{name}</h2>
          <p className="text-xs text-muted-foreground">{question}</p>
        </div>
      </div>
      <div className="mt-4 flex-1">{children}</div>
      <div className="mt-4 space-y-1 border-t border-border pt-3">
        {examples.map((text) => (
          <button
            key={text}
            type="button"
            disabled={busy}
            onClick={() => onExample(text)}
            className="block w-full truncate text-left text-xs text-muted-foreground transition-colors hover:text-foreground disabled:pointer-events-none"
          >
            “{text}”
          </button>
        ))}
      </div>
    </section>
  )
}

function ActionButton({
  icon,
  label,
  expanded,
  disabled,
  onClick,
}: {
  icon: React.ReactNode
  label: string
  expanded?: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      aria-expanded={expanded}
      className="flex w-full items-center gap-2 rounded-lg border border-border bg-secondary px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
    >
      {icon}
      {label}
      {expanded !== undefined && (
        <ChevronDown className={cn("ml-auto size-4 text-muted-foreground transition-transform", expanded && "rotate-180")} />
      )}
    </button>
  )
}
