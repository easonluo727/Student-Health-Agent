"use client"

// The final report, next to the agent. Tabs for whatever the report holds: care at specific providers (with the
// map) and the plan comparison (Arena). report.title heads it; the agent chat stays beside it the whole time.

import { useState } from "react"
import { BarChart3, MapPinned, X } from "lucide-react"
import type { Report, ReplyTarget } from "@/lib/protocol"
import { IS_MOCK } from "@/lib/client"
import { cn } from "@/lib/utils"
import { ArenaPanel } from "./arena-panel"
import { CareView } from "./care-view"

type Tab = "care" | "plans"

export function Dashboard({
  report,
  onAsk,
  onClose,
}: {
  report: Report
  onAsk: (target: ReplyTarget) => void
  onClose: () => void
}) {
  const hasCare = Boolean(report.care)
  const hasPlans = report.plans.length > 0
  const [picked, setPicked] = useState<Tab>(hasCare ? "care" : "plans")
  const tab: Tab = picked === "care" && !hasCare ? "plans" : picked === "plans" && !hasPlans ? "care" : picked

  return (
    <div className="flex h-full min-w-0 flex-col">
      <div className="flex h-14 shrink-0 items-center gap-3 border-b bg-card/80 px-6 backdrop-blur">
        <h1 className="truncate text-base font-semibold tracking-tight">{report.title}</h1>
        {IS_MOCK && (
          <span className="rounded-full border border-amber-500/30 bg-amber-50 px-2 py-0.5 text-[11px] text-amber-700">
            Mock data
          </span>
        )}
        {hasCare && hasPlans && (
          <div className="ml-4 flex gap-1 rounded-lg bg-muted p-1" role="tablist">
            <TabButton active={tab === "care"} onClick={() => setPicked("care")}>
              <MapPinned className="size-4" /> Care near you
            </TabButton>
            <TabButton active={tab === "plans"} onClick={() => setPicked("plans")}>
              <BarChart3 className="size-4" /> Compare plans
            </TabButton>
          </div>
        )}
        <button
          onClick={onClose}
          className="ml-auto rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label="Close report"
          title="Close report"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "care" && report.care ? (
          <CareView key={report.report_id} care={report.care} onAsk={onAsk} />
        ) : (
          <div className="mx-auto max-w-5xl">
            <ArenaPanel report={report} onAsk={onAsk} />
          </div>
        )}
      </div>
    </div>
  )
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-3 py-1 text-sm transition-colors",
        active ? "bg-card font-medium shadow-sm" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  )
}
