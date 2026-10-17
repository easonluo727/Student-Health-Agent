"use client"

import { useState } from "react"
import { CheckCircle2, Globe, LayoutGrid, Loader2, Lock, Radio, XCircle } from "lucide-react"
import type { SearchStep } from "@/lib/protocol"
import { cn } from "@/lib/utils"

// The agent's "computer": one frame per page the search worker visited.
// While the search runs the view follows the newest frame; when it finishes
// with several frames it shows an overview grid. Clicking a frame pauses
// following until the user goes back to live / overview.
// Key this component on the search id so a new search resets the view.
export function ComputerPanel({ steps }: { steps: SearchStep[] }) {
  const [focus, setFocus] = useState<number | null>(null)

  const frames = steps.filter((s) => s.screenshot)
  const running = steps.find((s) => s.status === "running")
  const live = Boolean(running)
  const following = focus === null
  const overview = following && !live && frames.length > 1
  const index = following ? frames.length - 1 : Math.min(focus, frames.length - 1)
  const frame = frames[index]

  return (
    <div className="flex h-full flex-col gap-3 p-5">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">Agent computer</span>
        {live ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">
            <Radio className="size-3 animate-pulse" /> Live
          </span>
        ) : (
          <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">Finished</span>
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          {frames.length} {frames.length === 1 ? "page" : "pages"} visited
        </span>
        {!following && (
          <button
            onClick={() => setFocus(null)}
            className="rounded-md border px-2 py-1 text-xs hover:bg-muted"
          >
            {live ? "Back to live" : "Overview"}
          </button>
        )}
      </div>

      {frames.length === 0 && (
        <div className="flex aspect-[16/10] items-center justify-center rounded-xl border bg-muted/40 text-sm text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" /> Starting the browser…
        </div>
      )}

      {overview ? (
        <div className="grid grid-cols-2 gap-3">
          {frames.map((f, i) => (
            <button
              key={f.step_id}
              onClick={() => setFocus(i)}
              className="group overflow-hidden rounded-lg border bg-card text-left hover:ring-2 hover:ring-primary/40"
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- data URL screenshot */}
              <img src={f.screenshot!} alt={f.title ?? f.text} className="aspect-[16/10] w-full object-cover object-top" />
              <div className="flex items-start gap-1.5 px-2.5 py-2 text-xs">
                <StatusIcon status={f.status} />
                <span className="line-clamp-2">{f.text}</span>
              </div>
            </button>
          ))}
        </div>
      ) : (
        frame && (
          <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
            <div className="flex items-center gap-2 border-b bg-muted/50 px-3 py-1.5">
              <div className="flex gap-1">
                <span className="size-2.5 rounded-full bg-muted-foreground/25" />
                <span className="size-2.5 rounded-full bg-muted-foreground/25" />
                <span className="size-2.5 rounded-full bg-muted-foreground/25" />
              </div>
              <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md bg-background px-2 py-0.5 text-[11px] text-muted-foreground">
                {frame.url?.startsWith("https") ? <Lock className="size-3 shrink-0" /> : <Globe className="size-3 shrink-0" />}
                <span className="truncate">{frame.url}</span>
              </div>
            </div>
            <div className="relative">
              {/* eslint-disable-next-line @next/next/no-img-element -- data URL screenshot */}
              <img src={frame.screenshot!} alt={frame.title ?? frame.text} className="aspect-[16/10] w-full object-cover object-top" />
              <div className="absolute inset-x-0 bottom-0 flex items-center gap-2 bg-gradient-to-t from-black/75 to-black/0 px-3 pb-2.5 pt-8 text-xs text-white">
                {following && running ? (
                  <>
                    <Loader2 className="size-3.5 shrink-0 animate-spin" />
                    <span className="truncate">{running.text}</span>
                  </>
                ) : (
                  <>
                    <StatusIcon status={frame.status} light />
                    <span className="truncate">{frame.text}</span>
                  </>
                )}
                <span className="ml-auto shrink-0 tabular-nums opacity-75">
                  {index + 1}/{frames.length}
                </span>
              </div>
            </div>
          </div>
        )
      )}

      {!overview && frames.length > 1 && (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {frames.map((f, i) => (
            <button
              key={f.step_id}
              onClick={() => setFocus(i)}
              className={cn(
                "w-28 shrink-0 overflow-hidden rounded-md border-2 transition-colors",
                i === index ? "border-primary" : "border-transparent opacity-70 hover:opacity-100",
              )}
              aria-label={f.text}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- data URL screenshot */}
              <img src={f.screenshot!} alt="" className="aspect-[16/10] w-full object-cover object-top" />
            </button>
          ))}
          {!live && (
            <button
              onClick={() => setFocus(null)}
              className="flex w-16 shrink-0 flex-col items-center justify-center gap-1 rounded-md border text-[11px] text-muted-foreground hover:bg-muted"
            >
              <LayoutGrid className="size-4" /> All
            </button>
          )}
        </div>
      )}

      <p className="mt-auto text-[11px] text-muted-foreground">
        A headless browser on our server visits public pages. It reads what&apos;s on screen, doesn&apos;t
        log in or fill forms, and stops if a site blocks automated access.
      </p>
    </div>
  )
}

function StatusIcon({ status, light }: { status: SearchStep["status"]; light?: boolean }) {
  if (status === "done")
    return <CheckCircle2 className={cn("size-3.5 shrink-0", light ? "text-emerald-300" : "text-emerald-600")} />
  if (status === "failed")
    return <XCircle className={cn("size-3.5 shrink-0", light ? "text-red-300" : "text-destructive")} />
  return <Loader2 className="size-3.5 shrink-0 animate-spin" />
}
