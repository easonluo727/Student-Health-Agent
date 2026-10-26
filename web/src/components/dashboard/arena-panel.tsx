"use client"

import { useState } from "react"
import { Award, Ban, MessageSquareText, Scale } from "lucide-react"
import type { Ranking, Report, ReplyTarget, Scenario } from "@/lib/protocol"
import { SourceChip, costText, usd } from "@/components/chat/source-chip"
import { IS_MOCK } from "@/lib/client"
import { cn } from "@/lib/utils"

export function ArenaPanel({
  report,
  onAsk,
}: {
  report: Report
  onAsk: (target: ReplyTarget) => void
}) {
  const planName = (id: string) => report.plans.find((p) => p.plan_id === id)?.name ?? id
  const recommended = report.recommendation?.plan_id

  return (
    <div className="space-y-6 p-5">
      {IS_MOCK && (
        <div className="rounded-lg border border-dashed border-amber-400 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          Mock data — placeholder numbers for UI development, not real plan figures.
        </div>
      )}

      {/* 1. Recommendation */}
      {report.recommendation && (
        <section className="rounded-xl border bg-primary/5 p-4">
          <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-primary">
            <Award className="size-4" /> Recommendation
          </div>
          <p className="text-base font-semibold leading-snug">{report.recommendation.headline}</p>
          <ul className="mt-2 list-disc space-y-0.5 pl-5 text-sm text-muted-foreground">
            {report.recommendation.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          {report.recommendation.excluded.length > 0 && (
            <div className="mt-3 space-y-1 border-t pt-2 text-sm">
              {report.recommendation.excluded.map((ex) => (
                <div key={ex.plan_id} className="flex items-start gap-2 text-muted-foreground">
                  <Ban className="mt-0.5 size-3.5 shrink-0" />
                  <span>
                    <span className="font-medium text-foreground">{planName(ex.plan_id)}</span> ruled
                    out: {ex.reason}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {/* 2. How the ranking was computed */}
      {report.ranking && <RankingExplainer ranking={report.ranking} />}

      {/* 3. Scenario comparison */}
      <ScenarioChart report={report} recommended={recommended} />

      {/* 4. Comparison table */}
      <section>
        <h3 className="mb-1 text-sm font-semibold">Side by side</h3>
        <p className="mb-2 text-xs text-muted-foreground">
          Highlighted rows matter for your situation. Click a row to ask about it.
        </p>
        <div className="overflow-x-auto rounded-xl border">
          <table className="w-full min-w-[520px] text-sm">
            <thead>
              <tr className="border-b bg-muted/40 text-left">
                <th className="px-3 py-2 font-medium text-muted-foreground"></th>
                {report.plans.map((plan) => (
                  <th key={plan.plan_id} className="px-3 py-2 align-bottom font-medium">
                    <div className="flex items-center gap-1">
                      {plan.plan_id === recommended && <Award className="size-3.5 text-primary" />}
                      {plan.name}
                    </div>
                    <div className="text-xs font-normal text-muted-foreground">
                      {plan.insurer} · {plan.network}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              <tr>
                <td className="px-3 py-2 text-muted-foreground">Annual premium</td>
                {report.plans.map((plan) => (
                  <td key={plan.plan_id} className="px-3 py-2 tabular-nums">
                    <span className="mr-1">
                      {plan.annual_premium == null ? "—" : usd(plan.annual_premium)}
                    </span>
                    {plan.source && <SourceChip source={plan.source} />}
                  </td>
                ))}
              </tr>
              {report.comparison.map((row) => (
                <tr
                  key={row.key}
                  onClick={() =>
                    onAsk({ kind: "dashboard_item", ref: `comparison.${row.key}`, label: row.label })
                  }
                  className={cn(
                    "group cursor-pointer hover:bg-muted/50",
                    row.highlight && "bg-amber-50/70 dark:bg-amber-950/20",
                  )}
                >
                  <td className="px-3 py-2 text-muted-foreground">
                    <span className="flex items-center gap-1.5">
                      {row.highlight && <span className="size-1.5 rounded-full bg-amber-500" />}
                      {row.label}
                      <MessageSquareText className="size-3.5 opacity-0 group-hover:opacity-60" />
                    </span>
                  </td>
                  {report.plans.map((plan) => {
                    const cell = row.cells[plan.plan_id]
                    return (
                      <td key={plan.plan_id} className="px-3 py-2">
                        {cell ? (
                          <>
                            <span className="inline-flex flex-wrap items-center gap-1">
                              {cell.text}
                              {cell.source && <SourceChip source={cell.source} />}
                            </span>
                            {cell.detail && (
                              <span className="mt-0.5 block text-xs text-muted-foreground">
                                {cell.detail}
                              </span>
                            )}
                          </>
                        ) : (
                          "—"
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {report.note && <p className="mt-2 text-[11px] text-muted-foreground">{report.note}</p>}
      </section>
    </div>
  )
}

// The ranking rule and each priority's weight, as Python applied them.
function RankingExplainer({ ranking }: { ranking: Ranking }) {
  return (
    <section className="rounded-xl border p-4">
      <h3 className="mb-1 flex items-center gap-2 text-sm font-semibold">
        <Scale className="size-4" /> How this ranking works
      </h3>
      <p className="text-xs text-muted-foreground">{ranking.summary}</p>
      {ranking.factors.length > 0 && (
        <table className="mt-3 w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="py-1.5 font-medium">Priority</th>
              <th className="py-1.5 font-medium">Your rating</th>
              <th className="py-1.5 font-medium">Weight</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {ranking.factors.map((f) => (
              <tr key={f.factor} className={cn(!f.used && "text-muted-foreground")}>
                <td className="py-1.5 pr-3">
                  {f.label}
                  {f.reason && <span className="block text-xs">{f.reason}</span>}
                </td>
                <td className="py-1.5 pr-3 tabular-nums">
                  {f.answer == null ? "Not rated (counts as 3)" : `${f.answer} / 5`}
                </td>
                <td className="py-1.5 tabular-nums">{f.weight}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

// Annual total cost per plan, one scenario at a time. Plans are identified by
// their row label, not by color; the recommended plan gets the accent.
function ScenarioChart({ report, recommended }: { report: Report; recommended?: string }) {
  const [key, setKey] = useState(
    report.scenarios.find((s) => s.key === "expected")?.key ?? report.scenarios[0]?.key,
  )
  const [hover, setHover] = useState<string | null>(null)
  const scenario: Scenario | undefined = report.scenarios.find((s) => s.key === key)
  if (!scenario) return null

  // One shared scale across all scenarios so switching tabs keeps bars comparable.
  const max = Math.max(
    ...report.scenarios.flatMap((s) => Object.values(s.costs).map((c) => c.high ?? c.mid)),
  )
  const pct = (v: number) => `${(v / max) * 100}%`
  const cheapest = Math.min(...Object.values(scenario.costs).map((c) => c.mid))

  return (
    <section>
      <h3 className="mb-1 text-sm font-semibold">Total cost for a year</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        A whole year under each plan. Pick a tab to change the assumptions.
      </p>
      <div className="mb-3 flex gap-1 rounded-lg bg-muted p-1" role="tablist">
        {report.scenarios.map((s) => (
          <button
            key={s.key}
            role="tab"
            aria-selected={s.key === key}
            onClick={() => setKey(s.key)}
            className={cn(
              "flex-1 rounded-md px-2 py-1.5 text-xs font-medium transition-colors",
              s.key === key ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {s.label}
          </button>
        ))}
      </div>
      <p className="mb-3 text-xs text-muted-foreground">{scenario.description}</p>

      <div className="space-y-3">
        {report.plans.map((plan) => {
          const cost = scenario.costs[plan.plan_id]
          if (!cost) return null
          const isRec = plan.plan_id === recommended
          const hasRange = cost.low != null && cost.high != null && cost.low !== cost.high
          const delta = cost.mid - cheapest
          return (
            <div
              key={plan.plan_id}
              className="relative"
              onMouseEnter={() => setHover(plan.plan_id)}
              onMouseLeave={() => setHover(null)}
            >
              <div className="mb-1 flex items-baseline justify-between text-xs">
                <span className={cn(isRec ? "font-semibold" : "text-muted-foreground")}>
                  {plan.name} <span className="font-normal text-muted-foreground">· {plan.insurer}</span>
                </span>
                <span className="tabular-nums">
                  <span className="font-medium">{costText(cost)}</span>
                  {delta > 0 && (
                    <span className="ml-1.5 text-muted-foreground">+{usd(delta)}</span>
                  )}
                </span>
              </div>
              <div className="relative h-2.5 rounded-full bg-muted">
                <div
                  className={cn(
                    "absolute inset-y-0 left-0 rounded-full",
                    isRec ? "bg-primary" : "bg-foreground/30",
                  )}
                  style={{ width: pct(cost.mid) }}
                />
                {hasRange && (
                  <div
                    className="absolute -inset-y-1 border-x-2 border-foreground/60"
                    style={{ left: pct(cost.low!), width: `calc(${pct(cost.high! - cost.low!)})` }}
                    aria-hidden
                  />
                )}
              </div>
              {hover === plan.plan_id && (
                <div className="absolute right-0 top-full z-10 mt-1 rounded-md border bg-popover px-2.5 py-1.5 text-xs shadow-md">
                  <div className="font-medium">{plan.name}</div>
                  <div className="tabular-nums text-muted-foreground">
                    {plan.annual_premium == null
                      ? "Care only (premium not priced yet)"
                      : `Premium ${usd(plan.annual_premium)} · Care ${usd(Math.max(0, cost.mid - plan.annual_premium))}`}
                  </div>
                  {hasRange && (
                    <div className="tabular-nums text-muted-foreground">
                      Range {usd(cost.low!)}–{usd(cost.high!)} (prices estimated)
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-[11px] text-muted-foreground">
        Bars show the middle estimate; brackets show the low–high range when prices are estimated.
      </p>
    </section>
  )
}
