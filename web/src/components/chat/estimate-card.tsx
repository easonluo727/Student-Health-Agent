import { AlertTriangle, Receipt } from "lucide-react"
import type { Estimate } from "@/lib/protocol"
import { SourceChip, costText, usd } from "./source-chip"

const STATUS_TEXT: Record<Estimate["status"], string> = {
  estimated: "Estimated",
  needs_information: "Need more information",
  needs_review: "Needs review",
  not_covered: "Not covered",
  invalid_input: "Can't calculate",
}

export function EstimateCard({ estimate }: { estimate: Estimate }) {
  const ok = estimate.status === "estimated" && estimate.patient_cost
  const isRange =
    ok && estimate.patient_cost!.low != null && estimate.patient_cost!.high != null

  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <div className="flex items-start gap-3 px-4 pt-3">
        <Receipt className="mt-1 size-4 text-primary" />
        <div className="flex-1">
          <div className="text-sm text-muted-foreground">
            {estimate.title} · {estimate.plan_name}
          </div>
          {ok ? (
            <div className="flex items-baseline gap-2">
              <span className="text-2xl font-semibold tabular-nums">
                {costText(estimate.patient_cost!)}
              </span>
              <span className="text-sm text-muted-foreground">
                {isRange ? "you pay (estimated range)" : "you pay"}
              </span>
            </div>
          ) : (
            <div className="flex items-center gap-2 py-1 font-medium text-amber-700 dark:text-amber-400">
              <AlertTriangle className="size-4" />
              {STATUS_TEXT[estimate.status]}
            </div>
          )}
          {estimate.message && <p className="text-sm text-muted-foreground">{estimate.message}</p>}
        </div>
      </div>

      {estimate.rows.length > 0 && (
        <table className="mt-2 w-full text-sm">
          <tbody className="divide-y border-t">
            {estimate.rows.map((row, i) => (
              <tr key={i}>
                <td className="px-4 py-1.5">
                  {row.label}
                  {row.note && <div className="text-xs text-muted-foreground">{row.note}</div>}
                </td>
                <td className="px-2 py-1.5 text-right tabular-nums">
                  {row.amount == null ? "—" : usd(row.amount)}
                </td>
                <td className="w-24 px-4 py-1.5 text-right">
                  {row.source && <SourceChip source={row.source} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {estimate.assumptions.length > 0 && (
        <div className="border-t bg-muted/30 px-4 py-2 text-xs text-muted-foreground">
          Assumes: {estimate.assumptions.join(" · ")}
        </div>
      )}
    </div>
  )
}
