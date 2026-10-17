import type {
  Answer,
  Confirm,
  Estimate,
  Form,
  ReplyTarget,
  Report,
  SearchStep,
  ServerEvent,
} from "./protocol"

export type CardState = "open" | "submitting" | "answered" | "dismissed"

export type TimelineItem =
  | { kind: "user"; id: string; text: string; replyTarget?: ReplyTarget }
  | { kind: "assistant"; id: string; text: string; streaming: boolean }
  | {
      kind: "form"
      id: string
      form: Form
      state: CardState
      answers?: Record<string, Answer>
      error?: string
    }
  | {
      kind: "confirm"
      id: string
      confirm: Confirm
      state: CardState
      confirmed?: boolean
      correction?: string
      error?: string
    }
  | { kind: "estimate"; id: string; estimate: Estimate }
  | { kind: "search"; id: string; steps: SearchStep[] }
  | { kind: "error"; id: string; message: string }

export interface ChatState {
  items: TimelineItem[]
  report: Report | null
  busy: boolean
  // Which card's answer started the current turn, so failures can be shown on it.
  pendingCardId: string | null
}

export const initialState: ChatState = {
  items: [],
  report: null,
  busy: false,
  pendingCardId: null,
}

export type Action =
  | { type: "user_message"; id: string; text: string; replyTarget?: ReplyTarget }
  | { type: "form_submit"; id: string; answers: Record<string, Answer> }
  | { type: "form_dismiss"; id: string }
  | { type: "confirm_submit"; id: string; confirmed: boolean; correction?: string }
  | { type: "server"; event: ServerEvent }
  | { type: "turn_failed"; message: string }
  | { type: "turn_finished" }

function update<T extends TimelineItem>(
  items: TimelineItem[],
  id: string,
  patch: (item: T) => T,
): TimelineItem[] {
  return items.map((item) => (item.id === id ? patch(item as T) : item))
}

type FormItem = Extract<TimelineItem, { kind: "form" }>
type ConfirmItem = Extract<TimelineItem, { kind: "confirm" }>
type AssistantItem = Extract<TimelineItem, { kind: "assistant" }>
type SearchItem = Extract<TimelineItem, { kind: "search" }>

// Cards that were submitted become "answered" once the server starts replying.
function settlePending(state: ChatState): ChatState {
  if (!state.pendingCardId) return state
  const items = update<FormItem | ConfirmItem>(state.items, state.pendingCardId, (item) =>
    item.state === "submitting" ? { ...item, state: "answered", error: undefined } : item,
  )
  return { ...state, items, pendingCardId: null }
}

export function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "user_message":
      return {
        ...state,
        busy: true,
        items: [
          ...state.items,
          { kind: "user", id: action.id, text: action.text, replyTarget: action.replyTarget },
        ],
      }

    case "form_submit":
      return {
        ...state,
        busy: true,
        pendingCardId: action.id,
        items: update<FormItem>(state.items, action.id, (item) => ({
          ...item,
          state: "submitting",
          answers: action.answers,
          error: undefined,
        })),
      }

    case "form_dismiss":
      return {
        ...state,
        busy: true,
        pendingCardId: null,
        items: update<FormItem>(state.items, action.id, (item) => ({ ...item, state: "dismissed" })),
      }

    case "confirm_submit":
      return {
        ...state,
        busy: true,
        pendingCardId: action.id,
        items: update<ConfirmItem>(state.items, action.id, (item) => ({
          ...item,
          state: "submitting",
          confirmed: action.confirmed,
          correction: action.correction,
          error: undefined,
        })),
      }

    case "turn_failed": {
      if (state.pendingCardId) {
        // Reopen the card with the error so the user can retry from it.
        return {
          ...state,
          busy: false,
          pendingCardId: null,
          items: update<FormItem | ConfirmItem>(state.items, state.pendingCardId, (item) => ({
            ...item,
            state: "open",
            error: action.message,
          })),
        }
      }
      return {
        ...state,
        busy: false,
        items: [...state.items, { kind: "error", id: crypto.randomUUID(), message: action.message }],
      }
    }

    case "turn_finished":
      return { ...settlePending(state), busy: false }

    case "server":
      return applyEvent(settlePending(state), action.event)
  }
}

function applyEvent(state: ChatState, { event, data }: ServerEvent): ChatState {
  switch (event) {
    case "message_start":
      return {
        ...state,
        items: [...state.items, { kind: "assistant", id: data.message_id, text: "", streaming: true }],
      }
    case "message_delta":
      return {
        ...state,
        items: update<AssistantItem>(state.items, data.message_id, (item) => ({
          ...item,
          text: item.text + data.text,
        })),
      }
    case "message_end":
      return {
        ...state,
        items: update<AssistantItem>(state.items, data.message_id, (item) => ({
          ...item,
          streaming: false,
        })),
      }
    case "form":
      return {
        ...state,
        items: [...state.items, { kind: "form", id: data.request_id, form: data, state: "open" }],
      }
    case "confirm":
      return {
        ...state,
        items: [
          ...state.items,
          { kind: "confirm", id: data.request_id, confirm: data, state: "open" },
        ],
      }
    case "estimate":
      return {
        ...state,
        items: [...state.items, { kind: "estimate", id: data.estimate_id, estimate: data }],
      }
    case "search_step": {
      const existing = state.items.find((item) => item.id === data.search_id)
      if (!existing) {
        return {
          ...state,
          items: [...state.items, { kind: "search", id: data.search_id, steps: [data] }],
        }
      }
      return {
        ...state,
        items: update<SearchItem>(state.items, data.search_id, (item) => {
          const index = item.steps.findIndex((step) => step.step_id === data.step_id)
          const steps =
            index === -1
              ? [...item.steps, data]
              : item.steps.map((step, i) => (i === index ? data : step))
          return { ...item, steps }
        }),
      }
    }
    case "report":
      return { ...state, report: data.report }
    case "error":
      return {
        ...state,
        items: [...state.items, { kind: "error", id: crypto.randomUUID(), message: data.message }],
      }
    case "done":
      return state
  }
}
