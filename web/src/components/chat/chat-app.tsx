"use client"

import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import {
  ArrowUp,
  Calculator,
  MessageCircle,
  FileText,
  FileUp,
  LayoutDashboard,
  Loader2,
  MapPinned,
  Paperclip,
  Scale,
  ShieldCheck,
  X,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { SearchWindow } from "@/components/computer/search-window"
import { Dashboard } from "@/components/dashboard/dashboard"
import { initialState, reducer, type Action, type TimelineItem } from "@/lib/chat-state"
import { IS_MOCK, listPlans, streamTurn, uploadFile } from "@/lib/client"
import type { Attachment, Mode, Plan, ReplyTarget, TurnContext, TurnInput } from "@/lib/protocol"
import { cn } from "@/lib/utils"
import { ConfirmCard } from "./confirm-card"
import { EstimateCard } from "./estimate-card"
import { FormCard } from "./form-card"
import { ModePill } from "./mode-pill"
import { SearchCard } from "./search-card"

type PlansState = { status: "loading" } | { status: "ready"; plans: Plan[] } | { status: "error" }

type PendingFile =
  | { status: "uploading"; name: string }
  | { status: "ready"; name: string; attachment: Attachment }
  | { status: "error"; name: string; error: string }

function placeholderFor(context: TurnContext | null, hasFile: boolean) {
  if (hasFile) return "Which visit or service should I price on this plan?"
  if (context?.mode === "calculate") return "Which visit or service should I price?"
  if (context?.mode === "review") return "Tell me about your health, budget, or doctors…"
  return "Ask about a visit's cost, or which plan fits you…"
}

export function ChatApp() {
  const [state, dispatch] = useReducer(reducer, initialState)
  // Created on first send: render-time randomness breaks prerendering.
  const sessionRef = useRef<string | null>(null)
  const [draft, setDraft] = useState("")
  const [replyTarget, setReplyTarget] = useState<ReplyTarget | null>(null)
  const [context, setContext] = useState<TurnContext | null>(null)
  const [plansState, setPlansState] = useState<PlansState>({ status: "loading" })
  const [dashboardOpen, setDashboardOpen] = useState(true)
  // Search windows the user closed (by search id); a new search opens its own window.
  const [closedSearches, setClosedSearches] = useState<string[]>([])
  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    listPlans()
      .then((plans) => setPlansState({ status: "ready", plans }))
      .catch(() => setPlansState({ status: "error" }))
  }, [])
  const plans = plansState.status === "ready" ? plansState.plans : []

  const run = useCallback(
    async (input: TurnInput, before: Action, target?: ReplyTarget, turnContext?: TurnContext) => {
      dispatch(before)
      sessionRef.current ??= crypto.randomUUID()
      try {
        for await (const event of streamTurn({
          session_id: sessionRef.current,
          input,
          reply_target: target,
          context: turnContext,
        })) {
          dispatch({ type: "server", event })
          if (event.event === "report") setDashboardOpen(true) // a new report comes to the front
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

  // The welcome suggestions pass their context directly: state updates land after this call.
  const send = (text: string, turnContext: TurnContext | null = context) => {
    const fallback = turnContext?.mode === "calculate" ? "Here's my plan. What would care cost on it?" : "Here's my plan. What do you think?"
    const trimmed = text.trim() || (attachments ? fallback : "")
    if (!trimmed || state.busy || pendingFile?.status === "uploading") return
    const target = replyTarget ?? undefined
    setDraft("")
    setReplyTarget(null)
    setPendingFile(null)
    run(
      { type: "message", text: trimmed, attachments },
      { type: "user_message", id: crypto.randomUUID(), text: trimmed, replyTarget: target, attachments },
      target,
      turnContext ?? undefined,
    )
  }

  const start = (text: string, turnContext: TurnContext) => {
    setContext(turnContext)
    send(text, turnContext)
  }
  const example = (text: string, mode: Mode) =>
    start(text, mode === "review" ? { mode, plan_ids: plans.map((p) => p.plan_id) } : { mode })
  const uploadForCalculate = () => {
    setContext({ mode: "calculate" })
    fileRef.current?.click()
  }

  const attach = async (file: File | undefined) => {
    if (!file) return
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      setPendingFile({ status: "error", name: file.name, error: "Only PDF files can be attached." })
      return
    }
    if (file.size > 25 * 1024 * 1024) {
      setPendingFile({ status: "error", name: file.name, error: "PDF must be 25 MB or smaller." })
      return
    }
    setPendingFile({ status: "uploading", name: file.name })
    sessionRef.current ??= crypto.randomUUID()
    try {
      const uploaded = await uploadFile(sessionRef.current, file)
      setPendingFile({ status: "ready", name: uploaded.name, attachment: { file_id: uploaded.file_id, name: uploaded.name } })
      inputRef.current?.focus()
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

  // The latest search with pages to show gets the floating window, until the user closes it.
  const search = state.items.findLast(
    (i): i is Extract<TimelineItem, { kind: "search" }> =>
      i.kind === "search" && (i.steps.some((s) => s.screenshot) || i.steps.some((s) => s.status === "running")),
  )
  const showSearch = search && !closedSearches.includes(search.id) && search.steps.some((s) => s.screenshot)
  const openComputer = () => setClosedSearches((ids) => ids.filter((id) => id !== search?.id))
  const withDashboard = Boolean(state.report && dashboardOpen)

  const waiting = state.busy && !state.items.some((i) => i.kind === "assistant" && i.streaming)
  const isHome = state.items.length === 0

  const composer = (
    <div>
      {(replyTarget || pendingFile || !isHome) && (
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          {!isHome && <ModePill context={context} plans={plans} disabled={state.busy} onChange={setContext} />}
          {replyTarget && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted px-2 py-1">
              Asking about: {replyTarget.label}
              <button
                onClick={() => setReplyTarget(null)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="Clear"
              >
                <X className="size-3.5" />
              </button>
            </span>
          )}
          {pendingFile && (
            <span
              className={cn(
                "inline-flex max-w-full items-center gap-1.5 rounded-lg border px-2 py-1",
                pendingFile.status === "error"
                  ? "border-destructive/40 bg-destructive/10 text-destructive"
                  : "border-border bg-card text-foreground",
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
              {pendingFile.status !== "uploading" && (
                <button
                  onClick={() => setPendingFile(null)}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label="Remove attachment"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </span>
          )}
        </div>
      )}
      <form
        className={cn(
          "flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-sm transition-colors focus-within:border-primary/50 focus-within:ring-3 focus-within:ring-primary/10",
          isHome && "p-3 shadow-md",
        )}
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
          rows={isHome ? 2 : 1}
          placeholder={placeholderFor(context, pendingFile?.status === "ready")}
          className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-muted-foreground/70"
        />
        <Button type="submit" size="icon" disabled={!canSend} aria-label="Send" className="rounded-xl">
          <ArrowUp />
        </Button>
      </form>
      <p className="mt-1.5 text-center font-mono text-[11px] text-muted-foreground">
        Estimates, not medical advice or a guarantee of coverage. Confirm with your insurer.
      </p>
    </div>
  )

  const timeline = (
    <div className={cn("space-y-4 py-6", withDashboard ? "px-4" : "mx-auto max-w-2xl px-4")}>
      {state.items.map((item) => (
        <TimelineRow key={item.id} item={item} busy={state.busy} run={run} onOpenComputer={openComputer} />
      ))}
      {waiting && <TypingDots />}
      <div ref={bottomRef} />
    </div>
  )

  return (
    <div className="flex h-dvh overflow-hidden">
      {/* The agent: always here. Full width before there is a report, a column beside the dashboard after. */}
      <section
        className={cn(
          "flex min-w-0 flex-col bg-background",
          withDashboard ? "w-[420px] shrink-0 border-r" : "flex-1",
        )}
      >
        <header className="flex h-14 shrink-0 items-center gap-2.5 px-5">
          <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <ShieldCheck className="size-4.5" strokeWidth={2.2} />
          </div>
          <div className="min-w-0 leading-tight">
            <div className="truncate text-sm font-semibold tracking-tight">Health Insurance Agent</div>
            {!withDashboard && <div className="text-[11px] text-muted-foreground">Costs, plans and doctors, priced by code</div>}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {IS_MOCK && (
              <span className="rounded-full border border-amber-500/30 bg-amber-50 px-2 py-0.5 text-xs text-amber-700">
                Mock backend
              </span>
            )}
            {state.report && !dashboardOpen && (
              <Button size="sm" variant="outline" onClick={() => setDashboardOpen(true)} className="gap-1.5">
                <LayoutDashboard className="size-4" /> Open report
              </Button>
            )}
          </div>
        </header>

        {isHome ? (
          <div className="flex-1 overflow-y-auto">
            <Welcome
              composer={composer}
              busy={state.busy}
              onExample={example}
              onUpload={uploadForCalculate}
              plansReady={plansState.status === "ready"}
              context={context}
              onMode={(mode) =>
                setContext(
                  mode === "chat" ? null : mode === "review" ? { mode, plan_ids: plans.map((p) => p.plan_id) } : { mode },
                )
              }
            />
          </div>
        ) : (
          <>
            <div className="min-h-0 flex-1 overflow-y-auto">{timeline}</div>
            <div className="px-4 pt-2 pb-3">
              <div className={cn(!withDashboard && "mx-auto max-w-2xl")}>{composer}</div>
            </div>
          </>
        )}
      </section>

      {withDashboard && state.report && (
        <main className="min-w-0 flex-1 bg-muted/40">
          <Dashboard report={state.report} onAsk={ask} onClose={() => setDashboardOpen(false)} />
        </main>
      )}

      {showSearch && search && (
        <SearchWindow
          key={search.id}
          steps={search.steps}
          onClose={() => setClosedSearches((ids) => [...ids, search.id])}
        />
      )}
    </div>
  )
}

const SUGGESTIONS: { icon: React.ReactNode; text: string; mode: Mode }[] = [
  { icon: <MapPinned className="size-4" />, text: "Where can I get a knee MRI near 90024, and what would I pay?", mode: "calculate" },
  { icon: <Calculator className="size-4" />, text: "How much would an ER visit for a sprained ankle cost?", mode: "calculate" },
  { icon: <Scale className="size-4" />, text: "Which plan is best if I see a specialist every month?", mode: "review" },
]

const MODES: { key: Mode | "chat"; label: string; hint: string; icon: React.ReactNode }[] = [
  { key: "calculate", label: "Calculate", hint: "estimate my costs", icon: <Calculator className="size-4" /> },
  { key: "review", label: "Review", hint: "find my best plan", icon: <Scale className="size-4" /> },
  { key: "chat", label: "Just chat", hint: "ask anything", icon: <MessageCircle className="size-4" /> },
]

// The start screen: a clean conversation. No forms or pickers; the agent asks for what it needs. The mode applies
// to the first message (Just chat sends no context), like the mode pill later on.
function Welcome({
  composer,
  busy,
  plansReady,
  context,
  onMode,
  onExample,
  onUpload,
}: {
  composer: React.ReactNode
  busy: boolean
  plansReady: boolean
  context: TurnContext | null
  onMode: (mode: Mode | "chat") => void
  onExample: (text: string, mode: Mode) => void
  onUpload: () => void
}) {
  const current = context?.mode ?? "chat"
  return (
    <div className="animate-fade-up mx-auto flex w-full max-w-2xl flex-col items-center px-4 pt-[16vh] pb-10">
      <h1 className="text-center text-3xl leading-tight font-semibold tracking-tight">
        What care are you trying to figure out?
      </h1>
      <p className="mt-3 max-w-md text-center text-sm leading-relaxed text-muted-foreground">
        Ask about a visit, a plan, or a doctor. Every number comes from a policy page or a real negotiated rate,
        computed by code.
      </p>
      <div className="mt-8 mb-3 flex gap-1 rounded-xl border bg-card p-1 shadow-xs" role="radiogroup" aria-label="Mode">
        {MODES.map((m) => (
          <button
            key={m.key}
            type="button"
            role="radio"
            aria-checked={current === m.key}
            disabled={busy || (m.key === "review" && !plansReady)}
            onClick={() => onMode(m.key)}
            className={cn(
              "inline-flex items-center gap-2 rounded-lg px-3.5 py-1.5 text-sm transition-colors disabled:opacity-50",
              current === m.key ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {m.icon}
            <span className="font-medium">{m.label}</span>
            <span className={cn("text-xs", current === m.key ? "text-primary-foreground/75" : "text-muted-foreground/80")}>
              {m.hint}
            </span>
          </button>
        ))}
      </div>
      <div className="w-full">{composer}</div>
      <div className="mt-5 flex w-full flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.text}
            type="button"
            disabled={busy || (s.mode === "review" && !plansReady)}
            onClick={() => onExample(s.text, s.mode)}
            className="inline-flex items-center gap-2 rounded-full border bg-card px-3.5 py-2 text-sm text-muted-foreground shadow-xs transition-colors hover:border-primary/40 hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
          >
            <span className="text-primary">{s.icon}</span>
            {s.text}
          </button>
        ))}
        <button
          type="button"
          disabled={busy}
          onClick={onUpload}
          className="inline-flex items-center gap-2 rounded-full border bg-card px-3.5 py-2 text-sm text-muted-foreground shadow-xs transition-colors hover:border-primary/40 hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
        >
          <FileUp className="size-4 text-primary" />
          Upload my plan PDF
        </button>
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
              className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-2 py-1 text-xs"
            >
              <FileText className="size-3.5" />
              {file.name}
            </span>
          ))}
          <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2 text-sm text-accent-foreground">
            {item.text}
          </div>
        </div>
      )
    case "assistant":
      return (
        <div className="prose-chat px-1 text-[15px] leading-relaxed">
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

function TypingDots() {
  return (
    <div className="flex gap-1.5 py-3 pl-1" aria-label="Thinking">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="size-1.5 animate-bounce rounded-full bg-foreground/60"
          style={{ animationDelay: `${i * 120}ms` }}
        />
      ))}
    </div>
  )
}
