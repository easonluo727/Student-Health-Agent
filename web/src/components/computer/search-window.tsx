"use client"

// A small floating window that shows the agent's browser while it searches or reads a PDF: the latest page and the
// step it is on. It stays out of the way of the chat and the dashboard; expand it to the full computer view.

import { useState } from "react"
import { CheckCircle2, FileText, Globe, Loader2, Maximize2, Minus, Monitor, X } from "lucide-react"
import type { SearchStep } from "@/lib/protocol"
import { cn } from "@/lib/utils"
import { ComputerPanel } from "./computer-panel"

export function SearchWindow({ steps, onClose }: { steps: SearchStep[]; onClose: () => void }) {
  // null = automatic: the small window while the search runs, one line once it is done.
  const [picked, setMode] = useState<"mini" | "collapsed" | "expanded" | null>(null)
  const frames = steps.filter((s) => s.screenshot)
  const running = steps.find((s) => s.status === "running")
  const latest = frames.at(-1)
  const isDocument = steps[0]?.kind === "document"
  const title = isDocument ? "Reading your plan" : "Agent browser"
  const status = running?.text ?? steps.at(-1)?.text ?? "Starting…"
  const mode = picked ?? (running ? "mini" : "collapsed")

  if (mode === "expanded") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/20 p-8 backdrop-blur-sm">
        <div className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-2xl border bg-card shadow-2xl">
          <div className="flex items-center gap-2 border-b px-4 py-2.5">
            <Monitor className="size-4 text-primary" />
            <span className="text-sm font-semibold">{title}</span>
            <button
              onClick={() => setMode("mini")}
              className="ml-auto rounded-md p-1.5 text-muted-foreground hover:bg-muted"
              aria-label="Back to the small window"
            >
              <Minus className="size-4" />
            </button>
          </div>
          <div className="min-h-0 overflow-y-auto">
            <ComputerPanel steps={steps} />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        "fixed right-5 bottom-5 z-40 w-80 overflow-hidden rounded-2xl border bg-card shadow-xl transition-all",
        "animate-fade-up",
      )}
    >
      <div className="flex items-center gap-2 border-b bg-muted/40 px-3 py-2">
        {running ? (
          <span className="relative flex size-2">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-75" />
            <span className="relative inline-flex size-2 rounded-full bg-emerald-500" />
          </span>
        ) : (
          <CheckCircle2 className="size-3.5 text-emerald-600" />
        )}
        <span className="text-xs font-semibold">{title}</span>
        <span className="text-[11px] text-muted-foreground">
          {frames.length} {frames.length === 1 ? "page" : "pages"}
        </span>
        <div className="ml-auto flex items-center">
          <IconButton label={mode === "collapsed" ? "Show" : "Minimize"} onClick={() => setMode(mode === "collapsed" ? "mini" : "collapsed")}>
            <Minus className="size-3.5" />
          </IconButton>
          <IconButton label="Expand" onClick={() => setMode("expanded")}>
            <Maximize2 className="size-3.5" />
          </IconButton>
          <IconButton label="Close" onClick={onClose}>
            <X className="size-3.5" />
          </IconButton>
        </div>
      </div>
      {mode === "mini" && (
        <button onClick={() => setMode("expanded")} className="block w-full text-left" aria-label="Expand">
          <div className="relative aspect-[16/10] bg-muted">
            {latest ? (
              // eslint-disable-next-line @next/next/no-img-element -- data URL screenshot
              <img src={latest.screenshot!} alt={latest.title ?? latest.text} className="size-full object-cover object-top" />
            ) : (
              <div className="flex size-full items-center justify-center text-xs text-muted-foreground">
                <Loader2 className="mr-1.5 size-3.5 animate-spin" /> Opening…
              </div>
            )}
            {latest?.url && (
              <div className="absolute top-2 left-2 flex max-w-[85%] items-center gap-1 rounded-md bg-card/90 px-1.5 py-0.5 text-[10px] text-muted-foreground shadow-sm">
                {isDocument ? <FileText className="size-3 shrink-0" /> : <Globe className="size-3 shrink-0" />}
                <span className="truncate">{latest.url}</span>
              </div>
            )}
          </div>
        </button>
      )}
      <div className="flex items-center gap-2 px-3 py-2 text-xs">
        {running ? <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" /> : <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600" />}
        <span className="truncate">{status}</span>
      </div>
    </div>
  )
}

function IconButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      title={label}
      className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
    >
      {children}
    </button>
  )
}
