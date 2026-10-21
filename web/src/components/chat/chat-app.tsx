"use client"

import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import {
  ArrowUp,
  BarChart3,
  FileText,
  Loader2,
  Monitor,
  PanelRightClose,
  PanelRightOpen,
  Paperclip,
  ShieldCheck,
  X,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { ComputerPanel } from "@/components/computer/computer-panel"
import { ArenaPanel } from "@/components/dashboard/arena-panel"
import { HomeScreen } from "@/components/home/home-screen"
import type { PlansState } from "@/components/home/plan-picker"
import { initialState, reducer, type Action, type TimelineItem } from "@/lib/chat-state"
import { IS_MOCK, listPlans, streamTurn, uploadFile } from "@/lib/client"
import type { Attachment, Mode, Plan, ReplyTarget, TurnContext, TurnInput } from "@/lib/protocol"
import { cn } from "@/lib/utils"
import { ConfirmCard } from "./confirm-card"
import { EstimateCard } from "./estimate-card"
import { FormCard } from "./form-card"
import { ModePill } from "./mode-pill"
import { SearchCard } from "./search-card"

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
  const [panelTab, setPanelTab] = useState<"arena" | "computer">("arena")
  const [panelOpen, setPanelOpen] = useState(true)
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

  // The home-screen actions pass their context directly: state updates land after this call.
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
  const calculateOn = (plan: Plan) =>
    start(`I want to know what care would cost on ${plan.name}.`, { mode: "calculate", plan_ids: [plan.plan_id] })
  const review = (chosen: Plan[]) =>
    start(
      chosen.length === 1
        ? `Is ${chosen[0].name} a good fit for me?`
        : `Help me compare ${chosen.map((p) => p.name).join(" and ")}, and find the plan that fits me.`,
      { mode: "review", plan_ids: chosen.map((p) => p.plan_id) },
    )
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
          "flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-sm transition-colors focus-within:border-foreground/40",
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

  return (
    <div className="relative flex h-dvh flex-col overflow-hidden">
      <header className="relative z-10 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-background/80 px-5 backdrop-blur">
        <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <ShieldCheck className="size-4.5" strokeWidth={2.2} />
        </div>
        <div className="min-w-0 leading-tight">
          <div className="truncate font-semibold tracking-tight">Health Insurance Agent</div>
          <div className="text-[11px] text-muted-foreground">Calculate costs · Review plans</div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {IS_MOCK && (
            <span className="rounded-full border border-amber-500/30 bg-amber-50 px-2 py-0.5 text-xs text-amber-700">
              Mock backend
            </span>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <main className={cn("flex min-w-0 flex-1 flex-col", hasPanel && panelOpen && "max-w-[52%]")}>
          {isHome ? (
            <div className="flex-1 overflow-y-auto">
              <HomeScreen
                plans={plansState}
                busy={state.busy}
                composer={composer}
                onCalculatePlan={calculateOn}
                onUpload={uploadForCalculate}
                onReview={review}
                onExample={example}
              />
            </div>
          ) : (
            <>
              <div className="flex-1 overflow-y-auto">
                <div className="mx-auto max-w-2xl space-y-4 px-4 py-6">
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
              <div className="border-t border-border bg-background/80 px-4 py-3 backdrop-blur">
                <div className="mx-auto max-w-2xl">{composer}</div>
              </div>
            </>
          )}
        </main>

        {hasPanel && panelOpen && (
          <aside className="flex min-w-0 flex-1 flex-col border-l border-border bg-card/60">
            <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-2">
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
            className="flex w-10 shrink-0 flex-col items-center gap-2 border-l border-border bg-card/60 pt-3 text-muted-foreground hover:text-foreground"
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
              className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-2 py-1 text-xs"
            >
              <FileText className="size-3.5" />
              {file.name}
            </span>
          ))}
          <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-primary px-4 py-2 text-sm text-primary-foreground">
            {item.text}
          </div>
        </div>
      )
    case "assistant":
      return (
        <div className="prose-chat rounded-2xl border border-border bg-card px-4 py-3 text-sm leading-relaxed">
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
        active ? "border border-border bg-background font-medium text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  )
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
