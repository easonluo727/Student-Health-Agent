"use client"

import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowUp, BarChart3, HeartPulse, Monitor, PanelRightClose, PanelRightOpen, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { ComputerPanel } from "@/components/computer/computer-panel"
import { ArenaPanel } from "@/components/dashboard/arena-panel"
import { initialState, reducer, type Action, type TimelineItem } from "@/lib/chat-state"
import { IS_MOCK, streamTurn } from "@/lib/client"
import type { ReplyTarget, TurnInput } from "@/lib/protocol"
import { cn } from "@/lib/utils"
import { ConfirmCard } from "./confirm-card"
import { EstimateCard } from "./estimate-card"
import { FormCard } from "./form-card"
import { SearchCard } from "./search-card"

const SUGGESTIONS = [
  "This is my UC SHIP, what do you think?",
  "How much would an ER visit for a sprained ankle cost?",
  "Can you recommend other commercial plans?",
  "What about Oscar Health?",
]

export function ChatApp() {
  const [state, dispatch] = useReducer(reducer, initialState)
  // Created on first send: render-time randomness breaks prerendering.
  const sessionRef = useRef<string | null>(null)
  const [draft, setDraft] = useState("")
  const [replyTarget, setReplyTarget] = useState<ReplyTarget | null>(null)
  const [panelTab, setPanelTab] = useState<"arena" | "computer">("arena")
  const [panelOpen, setPanelOpen] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const run = useCallback(
    async (input: TurnInput, before: Action, target?: ReplyTarget) => {
      dispatch(before)
      sessionRef.current ??= crypto.randomUUID()
      try {
        for await (const event of streamTurn({ session_id: sessionRef.current, input, reply_target: target })) {
          dispatch({ type: "server", event })
          // Bring the panel that just got new content to the front.
          if (event.event === "search_step" && event.data.screenshot) {
            setPanelTab("computer")
            setPanelOpen(true)
          } else if (event.event === "report") {
            setPanelTab("arena")
            setPanelOpen(true)
          }
        }
        dispatch({ type: "turn_finished" })
      } catch (error) {
        dispatch({
          type: "turn_failed",
          message: error instanceof Error ? error.message : "Something went wrong.",
        })
      }
    },
    [],
  )

  const send = (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || state.busy) return
    const target = replyTarget ?? undefined
    setDraft("")
    setReplyTarget(null)
    run(
      { type: "message", text: trimmed },
      { type: "user_message", id: crypto.randomUUID(), text: trimmed, replyTarget: target },
      target,
    )
  }

  const ask = (target: ReplyTarget) => {
    setReplyTarget(target)
    inputRef.current?.focus()
  }

  // Changes whenever the timeline grows: new items, streamed text, new search steps.
  const last = state.items.at(-1)
  const lastItemKey = `${state.items.length}:${
    last?.kind === "assistant" ? last.text.length : last?.kind === "search" ? last.steps.length : 0
  }`
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [lastItemKey])

  // The latest search that has screenshots feeds the computer panel.
  const search = state.items.findLast(
    (i): i is Extract<TimelineItem, { kind: "search" }> =>
      i.kind === "search" && i.steps.some((s) => s.screenshot),
  )
  const hasPanel = Boolean(state.report || search)
  const tab = panelTab === "computer" && search ? "computer" : state.report ? "arena" : "computer"
  const openComputer = () => {
    setPanelTab("computer")
    setPanelOpen(true)
  }

  const waiting = state.busy && !state.items.some((i) => i.kind === "assistant" && i.streaming)

  return (
    <div className="flex h-dvh flex-col bg-background">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-4">
        <HeartPulse className="size-5 text-primary" />
        <span className="truncate font-semibold">Student Health Agent</span>
        {IS_MOCK && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
            Mock backend
          </span>
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        <main className={cn("flex min-w-0 flex-1 flex-col", hasPanel && panelOpen && "max-w-[52%]")}>
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-2xl space-y-4 px-4 py-6">
              {state.items.length === 0 && <EmptyState onPick={send} />}
              {state.items.map((item) => (
                <TimelineRow
                  key={item.id}
                  item={item}
                  busy={state.busy}
                  run={run}
                  onOpenComputer={openComputer}
                />
              ))}
              {waiting && <TypingDots />}
              <div ref={bottomRef} />
            </div>
          </div>

          <div className="border-t bg-background px-4 py-3">
            <div className="mx-auto max-w-2xl">
              {replyTarget && (
                <div className="mb-2 flex items-center gap-2 text-xs">
                  <span className="rounded-full bg-primary/10 px-2 py-1 text-primary">
                    Asking about: {replyTarget.label}
                  </span>
                  <button
                    onClick={() => setReplyTarget(null)}
                    className="text-muted-foreground hover:text-foreground"
                    aria-label="Clear"
                  >
                    <X className="size-3.5" />
                  </button>
                </div>
              )}
              <form
                className="flex items-end gap-2 rounded-2xl border bg-card p-2 focus-within:ring-3 focus-within:ring-ring/30"
                onSubmit={(e) => {
                  e.preventDefault()
                  send(draft)
                }}
              >
                <textarea
                  ref={inputRef}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault()
                      send(draft)
                    }
                  }}
                  rows={1}
                  placeholder="Ask about your plan, a visit, or which plan fits you…"
                  className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none"
                />
                <Button type="submit" size="icon" disabled={!draft.trim() || state.busy} aria-label="Send">
                  <ArrowUp />
                </Button>
              </form>
              <p className="mt-1.5 text-center text-[11px] text-muted-foreground">
                Estimates, not medical advice or a guarantee of coverage. Confirm with your insurer.
              </p>
            </div>
          </div>
        </main>

        {hasPanel && panelOpen && (
          <aside className="flex min-w-0 flex-1 flex-col border-l bg-muted/20">
            <div className="flex shrink-0 items-center gap-1 border-b px-3 py-2">
              {state.report && (
                <PanelTab active={tab === "arena"} onClick={() => setPanelTab("arena")}>
                  <BarChart3 className="size-4" /> Arena
                </PanelTab>
              )}
              {search && (
                <PanelTab active={tab === "computer"} onClick={() => setPanelTab("computer")}>
                  <Monitor className="size-4" /> Computer
                </PanelTab>
              )}
              <button
                onClick={() => setPanelOpen(false)}
                className="ml-auto rounded-md p-1.5 text-muted-foreground hover:bg-muted"
                aria-label="Hide panel"
              >
                <PanelRightClose className="size-4" />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {tab === "arena" && state.report && <ArenaPanel report={state.report} onAsk={ask} />}
              {tab === "computer" && search && <ComputerPanel key={search.id} steps={search.steps} />}
            </div>
          </aside>
        )}
        {hasPanel && !panelOpen && (
          <button
            onClick={() => setPanelOpen(true)}
            className="flex w-10 shrink-0 flex-col items-center gap-2 border-l bg-muted/20 pt-3 text-muted-foreground hover:text-foreground"
            aria-label="Show panel"
          >
            <PanelRightOpen className="size-4" />
          </button>
        )}
      </div>

    </div>
  )
}

function TimelineRow({
  item,
  busy,
  run,
  onOpenComputer,
}: {
  item: TimelineItem
  busy: boolean
  run: (input: TurnInput, before: Action) => void
  onOpenComputer: () => void
}) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex flex-col items-end gap-1">
          {item.replyTarget && (
            <span className="text-xs text-muted-foreground">About: {item.replyTarget.label}</span>
          )}
          <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-primary px-4 py-2 text-sm text-primary-foreground">
            {item.text}
          </div>
        </div>
      )
    case "assistant":
      return (
        <div className="prose-chat text-sm leading-relaxed">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.text}</ReactMarkdown>
        </div>
      )
    case "form":
      return (
        <FormCard
          item={item}
          disabled={busy && item.state === "open"}
          onSubmit={(answers) =>
            run(
              { type: "form_response", request_id: item.form.request_id, answers },
              { type: "form_submit", id: item.id, answers },
            )
          }
          onDismiss={() =>
            run({ type: "form_dismiss", request_id: item.form.request_id }, { type: "form_dismiss", id: item.id })
          }
        />
      )
    case "confirm":
      return (
        <ConfirmCard
          item={item}
          disabled={busy && item.state === "open"}
          onSubmit={(confirmed, correction) =>
            run(
              { type: "confirm_response", request_id: item.confirm.request_id, confirmed, correction },
              { type: "confirm_submit", id: item.id, confirmed, correction },
            )
          }
        />
      )
    case "estimate":
      return <EstimateCard estimate={item.estimate} />
    case "search":
      return <SearchCard steps={item.steps} onOpenComputer={onOpenComputer} />
    case "error":
      return (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {item.message}
        </div>
      )
  }
}

function PanelTab({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm",
        active ? "bg-background font-medium shadow-sm" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  )
}

function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="py-12 text-center">
      <HeartPulse className="mx-auto mb-3 size-8 text-primary" />
      <h1 className="text-xl font-semibold">Pick health insurance with real numbers</h1>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">
        Aged off your parents&apos; plan? I&apos;ll price your plan for the care you actually use, and
        compare it with commercial plans — every number traced to a policy page or a negotiated rate.
      </p>
      <div className="mt-6 flex flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="rounded-full border px-3 py-1.5 text-sm hover:bg-muted"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

function TypingDots() {
  return (
    <div className="flex gap-1 py-2" aria-label="Thinking">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60"
          style={{ animationDelay: `${i * 120}ms` }}
        />
      ))}
    </div>
  )
}
