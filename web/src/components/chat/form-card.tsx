"use client"

import { useState } from "react"
import { Check, ChevronDown, ClipboardList, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { Answer, AnswerValue, Question } from "@/lib/protocol"
import type { TimelineItem } from "@/lib/chat-state"
import { cn } from "@/lib/utils"

type FormItem = Extract<TimelineItem, { kind: "form" }>

function initialAnswers(questions: Question[]): Record<string, Answer> {
  return Object.fromEntries(
    questions.map((q) => [q.field, { value: q.prefill?.value ?? null, unknown: false }]),
  )
}

function isAnswered(question: Question, answer: Answer) {
  if (answer.unknown) return true
  const { value } = answer
  if (value == null || value === "") return false
  if (Array.isArray(value)) return value.length > 0
  if (question.kind === "number") {
    const n = Number(value)
    if (Number.isNaN(n)) return false
    if (question.min != null && n < question.min) return false
    if (question.max != null && n > question.max) return false
  }
  return true
}

export function FormCard({
  item,
  disabled,
  onSubmit,
  onDismiss,
}: {
  item: FormItem
  disabled: boolean
  onSubmit: (answers: Record<string, Answer>) => void
  onDismiss: () => void
}) {
  const { form } = item
  const [answers, setAnswers] = useState(() => item.answers ?? initialAnswers(form.questions))
  const [expanded, setExpanded] = useState(false)
  const set = (field: string, answer: Answer) => setAnswers((prev) => ({ ...prev, [field]: answer }))

  if (item.state === "answered" || item.state === "dismissed") {
    return (
      <div className="rounded-xl border bg-card text-sm">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
        >
          {item.state === "answered" ? (
            <Check className="size-4 text-emerald-600" />
          ) : (
            <X className="size-4 text-muted-foreground" />
          )}
          <span className="font-medium">{form.title}</span>
          <span className="text-muted-foreground">
            · {item.state === "answered" ? `${form.questions.length} answers` : "skipped"}
          </span>
          {item.state === "answered" && (
            <ChevronDown
              className={cn("ml-auto size-4 transition-transform", expanded && "rotate-180")}
            />
          )}
        </button>
        {expanded && item.answers && (
          <dl className="space-y-1 border-t px-4 py-2.5">
            {form.questions.map((q) => (
              <div key={q.field} className="flex gap-2">
                <dt className="text-muted-foreground">{q.label}:</dt>
                <dd>{summarize(q, item.answers![q.field])}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    )
  }

  const submitting = item.state === "submitting"
  const locked = submitting || disabled
  const complete = form.questions.every((q) => isAnswered(q, answers[q.field]))

  return (
    <form
      className="overflow-hidden rounded-xl border bg-card shadow-sm"
      onSubmit={(event) => {
        event.preventDefault()
        if (complete && !locked) onSubmit(normalize(form.questions, answers))
      }}
    >
      <div className="flex items-start gap-3 border-b bg-muted/40 px-4 py-3">
        <ClipboardList className="mt-0.5 size-4 text-primary" />
        <div className="flex-1">
          <div className="font-medium">{form.title}</div>
          {form.intro && <div className="text-sm text-muted-foreground">{form.intro}</div>}
        </div>
        <button
          type="button"
          onClick={onDismiss}
          disabled={locked}
          className="rounded p-1 text-muted-foreground hover:bg-muted disabled:opacity-50"
          aria-label="Skip these questions"
        >
          <X className="size-4" />
        </button>
      </div>

      <fieldset disabled={locked} className="space-y-5 px-4 py-4">
        {form.questions.map((q, i) => (
          <div key={q.field} className="space-y-3">
            {q.heading && q.heading !== form.questions[i - 1]?.heading && (
              <div className={cn("text-sm font-semibold", i > 0 && "border-t pt-4")}>{q.heading}</div>
            )}
            <QuestionField question={q} answer={answers[q.field]} onChange={(a) => set(q.field, a)} />
          </div>
        ))}
      </fieldset>

      <div className="flex items-center gap-3 border-t px-4 py-3">
        {item.error && <span className="text-sm text-destructive">{item.error}</span>}
        <span className="ml-auto text-xs text-muted-foreground">
          Your numbers go straight to the calculator.
        </span>
        <Button type="submit" disabled={!complete || locked}>
          {submitting ? "Sending…" : item.error ? "Retry" : "Submit"}
        </Button>
      </div>
    </form>
  )
}

function normalize(questions: Question[], answers: Record<string, Answer>) {
  return Object.fromEntries(
    questions.map((q) => {
      const answer = answers[q.field]
      if (answer.unknown) return [q.field, { value: null, unknown: true }]
      const value =
        q.kind === "number" || q.kind === "weight" ? Number(answer.value) : answer.value
      return [q.field, { value, unknown: false }]
    }),
  )
}

function summarize(question: Question, answer: Answer | undefined) {
  if (!answer || answer.unknown) return "I don't know"
  const label = (v: AnswerValue) =>
    question.options?.find((o) => o.value === v)?.label ?? String(v)
  if (Array.isArray(answer.value)) return answer.value.map(label).join(", ")
  if (question.kind === "weight") return `${answer.value} / 5`
  return `${label(answer.value)}${question.unit ? ` ${question.unit}` : ""}`
}

function QuestionField({
  question: q,
  answer,
  onChange,
}: {
  question: Question
  answer: Answer
  onChange: (answer: Answer) => void
}) {
  const unknown = answer.unknown
  const pick = (value: AnswerValue) => onChange({ value, unknown: false })

  return (
    <div className="space-y-2">
      <div>
        <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
          {q.label}
          {q.prefill && !unknown && (
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-normal text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
              From your message — please confirm
            </span>
          )}
        </div>
        {q.help && <div className="text-xs text-muted-foreground">{q.help}</div>}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className={cn("contents", unknown && "[&>*]:opacity-40")}>
        {q.kind === "single" &&
          q.options?.map((o) => (
            <Chip key={o.value} selected={!unknown && answer.value === o.value} onClick={() => pick(o.value)}>
              {o.label}
            </Chip>
          ))}

        {q.kind === "multi" &&
          q.options?.map((o) => {
            const current = Array.isArray(answer.value) && !unknown ? answer.value : []
            const selected = current.includes(o.value)
            return (
              <Chip
                key={o.value}
                selected={selected}
                onClick={() =>
                  pick(selected ? current.filter((v) => v !== o.value) : [...current, o.value])
                }
              >
                {o.label}
              </Chip>
            )
          })}

        {q.kind === "number" && (
          <label className="flex items-center gap-2">
            <input
              type="number"
              inputMode="decimal"
              min={q.min}
              max={q.max}
              value={unknown || answer.value == null ? "" : String(answer.value)}
              onChange={(e) => pick(e.target.value === "" ? null : e.target.value)}
              className="h-9 w-28 rounded-lg border bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            />
            {q.unit && <span className="text-sm text-muted-foreground">{q.unit}</span>}
          </label>
        )}

        {q.kind === "text" && (
          <input
            type="text"
            value={unknown || answer.value == null ? "" : String(answer.value)}
            onChange={(e) => pick(e.target.value)}
            className="h-9 w-full rounded-lg border bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        )}

        {q.kind === "weight" && (
          <div className="w-full max-w-sm">
            <div className="flex gap-1.5">
              {[1, 2, 3, 4, 5].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => pick(n)}
                  className={cn(
                    "h-8 flex-1 rounded-md border text-sm transition-colors",
                    !unknown && Number(answer.value) >= n
                      ? "border-primary bg-primary text-primary-foreground"
                      : "hover:bg-muted",
                  )}
                  aria-label={`${n} of 5`}
                >
                  {n}
                </button>
              ))}
            </div>
            {q.scale_labels && (
              <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
                <span>{q.scale_labels[0]}</span>
                <span>{q.scale_labels[1]}</span>
              </div>
            )}
          </div>
        )}
        </div>
        <Chip
          selected={unknown}
          subtle
          onClick={() => onChange(unknown ? { value: null, unknown: false } : { value: null, unknown: true })}
        >
          I don&apos;t know
        </Chip>
      </div>
    </div>
  )
}

function Chip({
  selected,
  subtle,
  onClick,
  children,
}: {
  selected: boolean
  subtle?: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        "rounded-full border px-3 py-1.5 text-sm transition-colors disabled:cursor-not-allowed",
        selected
          ? "border-primary bg-primary text-primary-foreground"
          : subtle
            ? "border-dashed text-muted-foreground hover:bg-muted"
            : "hover:bg-muted",
      )}
    >
      {children}
    </button>
  )
}
