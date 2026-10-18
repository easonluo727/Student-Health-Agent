"use client"

import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import {
  ArrowUp,
  BarChart3,
  FileText,
  HeartPulse,
  Loader2,
  Monitor,
  PanelRightClose,
  PanelRightOpen,
  Paperclip,
  X,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { ComputerPanel } from "@/components/computer/computer-panel"
import { ArenaPanel } from "@/components/dashboard/arena-panel"
import { initialState, reducer, type Action, type TimelineItem } from "@/lib/chat-state"
import { IS_MOCK, streamTurn, uploadFile } from "@/lib/client"
import type { Attachment, ReplyTarget, TurnInput } from "@/lib/protocol"
import { cn } from "@/lib/utils"
import { ConfirmCard } from "./confirm-card"
import { EstimateCard } from "./estimate-card"
import { FormCard } from "./form-card"
import { SearchCard } from "./search-card"

type PendingFile =
  | { status: "uploading"; name: string }
  | { status: "ready"; name: string; attachment: Attachment }
  | { status: "error"; name: string; error: string }

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
  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

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

  const attachments = pendingFile?.status === "ready" ? [pendingFile.attachment] : undefined
  const canSend = !state.busy && pendingFile?.status !== "uploading" && Boolean(draft.trim() || attachments)

  const send = (text: string) => {
    const trimmed = text.trim() || (attachments ? "Here's my plan. What do you think?" : "")
    if (!trimmed || state.busy || pendingFile?.status === "uploading") return
    const target = replyTarget ?? undefined
    setDraft("")
    setReplyTarget(null)
    setPendingFile(null)
    run(
      { type: "message", text: trimmed, attachments },
      { type: "user_message", id: crypto.randomUUID(), text: trimmed, replyTarget: target, attachments },
      target,
    )
  }

  const attach = async (file: File | undefined) => {
    if (!file) return
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      setPendingFile({ status: "error", name: file.name, error: "Only PDF files can be attached." })
      return
    }
    setPendingFile({ status: "uploading", name: file.name })
    sessionRef.current ??= crypto.randomUUID()
    try {
      const uploaded = await uploadFile(sessionRef.current, file)
      setPendingFile({ status: "ready", name: uploaded.name, attachment: { file_id: uploaded.file_id, name: uploaded.name } })
    } catch (error) {
      setPendingFile({
        status: "error",
        name: file.name,
        error: error instanceof Error ? error.message : "Upload failed.",
      })
    }
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
    <div className="relative flex h-dvh flex-col overflow-hidden bg-background">
      {/* Tech grid backdrop */}
      <div className="bg-tech-grid pointer-events-none absolute inset-0" aria-hidden />

      <header className="relative z-10 flex h-16 shrink-0 items-center gap-3 border-b border-white/5 bg-card/50 px-5 backdrop-blur">
        <div className="shadow-brand-glow flex size-9 items-center justify-center rounded-xl bg-gradient-to-br from-[oklch(0.66_0.21_27)] to-[oklch(0.45_0.18_25)]">
          <HeartPulse className="size-5 text-white" strokeWidth={2.2} />
        </div>
        <div className="min-w-0 leading-tight">
          <div className="truncate font-semibold tracking-tight">
            Student Health Agent
          </div>
          <div className="text-[11px] text-muted-foreground">
            AI insurance advisor · AMD Developer Hackathon
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className="hidden items-center gap-1.5 rounded-full border border-white/10 bg-white/[3%] px-2.5 py-1 font-mono text-[11px] text-muted-foreground sm:inline-flex">
            <span className="size-1.5 animate-pulse-dot rounded-full bg-brand" />
            Powered by AMD API · Qwen
          </span>
          {IS_MOCK && (
            <span className="rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-xs text-amber-300">
              Mock backend
            </span>
          )}
        </div>
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

          <div className="border-t border-white/5 bg-background/70 px-4 py-3 backdrop-blur">
            <div className="mx-auto max-w-2xl">
              {replyTarget && (
                <div className="mb-2 flex items-center gap-2 text-xs">
                  <span className="rounded-full border border-brand/30 bg-brand/10 px-2 py-1 text-[oklch(0.78_0.13_30)]">
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
              {pendingFile && (
                <div className="mb-2 flex items-center gap-2 text-xs">
                  <span
                    className={cn(
                      "inline-flex max-w-full items-center gap-1.5 rounded-lg border px-2 py-1",
                      pendingFile.status === "error"
                        ? "border-destructive/40 bg-destructive/10 text-destructive"
                        : "border-white/10 bg-white/[4%] text-foreground",
                    )}
                  >
                    {pendingFile.status === "uploading" ? (
                      <Loader2 className="size-3.5 animate-spin" />
                    ) : (
                      <FileText className="size-3.5" />
                    )}
                    <span className="truncate">{pendingFile.name}</span>
                    {pendingFile.status === "uploading" && <span className="text-muted-foreground">Uploading…</span>}
                    {pendingFile.status === "error" && <span>· {pendingFile.error}</span>}
                  </span>
                  {pendingFile.status !== "uploading" && (
                    <button
                      onClick={() => setPendingFile(null)}
                      className="text-muted-foreground hover:text-foreground"
                      aria-label="Remove attachment"
                    >
                      <X className="size-3.5" />
                    </button>
                  )}
                </div>
              )}
              <form
                className="flex items-end gap-2 rounded-2xl border border-white/10 bg-card/80 p-2 shadow-lg shadow-black/20 transition-all focus-within:border-brand/50 focus-within:shadow-[0_0_28px_-8px_oklch(0.60_0.21_27/50%)]"
                onSubmit={(e) => {
                  e.preventDefault()
                  send(draft)
                }}
              >
                <input
                  ref={fileRef}
                  type="file"
                  accept="application/pdf,.pdf"
                  className="hidden"
                  onChange={(e) => {
                    attach(e.target.files?.[0])
                    e.target.value = ""
                  }}
                />
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  disabled={state.busy || pendingFile?.status === "uploading"}
                  onClick={() => fileRef.current?.click()}
                  aria-label="Attach your plan (PDF)"
                  title="Attach your plan (PDF)"
                  className="rounded-xl text-muted-foreground hover:text-foreground"
                >
                  <Paperclip />
                </Button>
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
                  className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-muted-foreground/70"
                />
                <Button
                  type="submit"
                  size="icon"
                  disabled={!canSend}
                  aria-label="Send"
                  className="shadow-brand-glow rounded-xl bg-gradient-to-br from-[oklch(0.62_0.21_27)] to-[oklch(0.47_0.19_26)] text-white hover:from-[oklch(0.66_0.21_27)] hover:to-[oklch(0.50_0.19_26)]"
                >
                  <ArrowUp />
                </Button>
              </form>
              <p className="mt-1.5 text-center font-mono text-[11px] text-muted-foreground">
                Estimates, not medical advice or a guarantee of coverage. Confirm with your insurer.
              </p>
            </div>
          </div>
        </main>

        {hasPanel && panelOpen && (
          <aside className="flex min-w-0 flex-1 flex-col border-l border-white/5 bg-card/30 backdrop-blur">
            <div className="flex shrink-0 items-center gap-1 border-b border-white/5 px-3 py-2">
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
            className="flex w-10 shrink-0 flex-col items-center gap-2 border-l border-white/5 bg-card/30 pt-3 text-muted-foreground hover:text-foreground"
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
          {item.attachments?.map((file) => (
            <span
              key={file.file_id}
              className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[4%] px-2 py-1 text-xs"
            >
              <FileText className="size-3.5 text-[oklch(0.78_0.13_30)]" />
              {file.name}
            </span>
          ))}
          <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-gradient-to-br from-[oklch(0.62_0.21_27)] to-[oklch(0.47_0.19_26)] px-4 py-2 text-sm text-white shadow-lg shadow-brand/25">
            {item.text}
          </div>
        </div>
      )
    case "assistant":
      return (
        <div className="prose-chat rounded-2xl border-l-2 border-l-brand/60 bg-card/40 px-4 py-3 text-sm leading-relaxed">
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
        "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors",
        active
          ? "border border-brand/30 bg-brand/10 font-medium text-foreground"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  )
}

function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="animate-fade-up py-12 text-center">
      <div className="shadow-brand-glow mx-auto mb-5 flex size-14 items-center justify-center rounded-2xl bg-gradient-to-br from-[oklch(0.66_0.21_27)] to-[oklch(0.45_0.18_25)]">
        <HeartPulse className="size-7 text-white" strokeWidth={2.2} />
      </div>
      <p className="font-mono text-[11px] tracking-[0.25em] text-glow uppercase">
        Ask · Compare · Decide
      </p>
      <h1 className="text-gradient-brand mx-auto mt-2 max-w-lg text-3xl leading-tight font-bold tracking-tight">
        Pick health insurance with real numbers
      </h1>
      <p className="mx-auto mt-3 max-w-md text-sm leading-relaxed text-muted-foreground">
        Aged off your parents&apos; plan? I&apos;ll price your plan for the care you actually use, and
        compare it with commercial plans — every number traced to a policy page or a negotiated rate.
      </p>
      <div className="mx-auto mt-6 flex max-w-xl flex-wrap items-center justify-center gap-2">
        {[
          ["629,842", "negotiated rates"],
          ["96%", "LA network coverage"],
          ["19", "common student visits"],
          ["100%", "open models on AMD"],
        ].map(([value, label]) => (
          <div
            key={label}
            className="rounded-xl border border-white/10 bg-white/[3%] px-3 py-1.5 text-left backdrop-blur"
          >
            <span className="font-mono text-sm font-semibold text-[oklch(0.78_0.13_30)]">{value}</span>
            <span className="ml-1.5 text-xs text-muted-foreground">{label}</span>
          </div>
        ))}
      </div>
      <div className="mt-8 flex flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="rounded-full border border-white/10 bg-white/[3%] px-3.5 py-1.5 text-sm text-muted-foreground transition-all hover:border-brand/50 hover:bg-brand/10 hover:text-foreground"
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
    <div className="flex gap-1.5 py-3 pl-1" aria-label="Thinking">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="size-1.5 animate-bounce rounded-full bg-brand"
          style={{ animationDelay: `${i * 120}ms` }}
        />
      ))}
    </div>
  )
}
