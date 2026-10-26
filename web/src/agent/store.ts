import type { Confirm, EmailDraft, EmailStatus, Estimate, Form, Report, ReplyTarget, SearchStep, ServerEvent } from "./protocol";

export interface LocalAttachment { file_id: string; name: string; size: number; content_type: string; preview?: string | undefined }

export type TimelineItem =
  | { kind: "user"; id: string; text: string; attachments: LocalAttachment[]; replyTarget?: ReplyTarget | undefined }
  | { kind: "agent"; id: string; text: string; streaming: boolean }
  | { kind: "form"; id: string; form: Form; state: "open" | "submitted" | "dismissed"; summary?: string | undefined }
  | { kind: "confirm"; id: string; confirm: Confirm; state: "open" | "confirmed" | "corrected"; correction?: string | undefined }
  | { kind: "search"; id: string; searchId: string; steps: SearchStep[] }
  | { kind: "estimate"; id: string; estimateId: string }
  | { kind: "email"; id: string; draft: EmailDraft; status?: EmailStatus | undefined; state: "open" | "cancelled" }
  | { kind: "error"; id: string; message: string };

/** Canvas views: "report" | `estimate:${id}` | `search:${id}` */
export type CanvasView = string;

export interface AgentState {
  sessionId: string;
  items: TimelineItem[];
  report: Report | null;
  estimates: Record<string, Estimate>;
  busy: boolean;
  canvasOpen: boolean;
  canvasView: CanvasView | null;
}

export type AgentAction =
  | { type: "reset"; sessionId: string }
  | { type: "server"; event: ServerEvent }
  | { type: "user"; item: Extract<TimelineItem, { kind: "user" }> }
  | { type: "busy"; busy: boolean }
  | { type: "form"; requestId: string; state: "submitted" | "dismissed"; summary?: string | undefined }
  | { type: "confirm"; requestId: string; state: "confirmed" | "corrected"; correction?: string | undefined }
  | { type: "email"; requestId: string; state?: "cancelled" | undefined; draft?: Partial<EmailDraft> | undefined; status?: EmailStatus | undefined }
  | { type: "canvas"; open?: boolean | undefined; view?: CanvasView | undefined }
  | { type: "local_error"; message: string };

export const initialState = (sessionId: string): AgentState => ({ sessionId, items: [], report: null, estimates: {}, busy: false, canvasOpen: false, canvasView: null });

let seq = 0;
const uid = (p: string) => `${p}-${++seq}`;

export function agentReducer(state: AgentState, action: AgentAction): AgentState {
  switch (action.type) {
    case "reset": return initialState(action.sessionId);
    case "busy": return { ...state, busy: action.busy };
    case "user": return { ...state, items: [...state.items, action.item] };
    case "local_error": return { ...state, items: [...state.items, { kind: "error", id: uid("err"), message: action.message }] };
    case "canvas": return { ...state, canvasOpen: action.open ?? state.canvasOpen, canvasView: action.view ?? state.canvasView };
    case "form": return { ...state, items: state.items.map(i => i.kind === "form" && i.form.request_id === action.requestId ? { ...i, state: action.state, summary: action.summary } : i) };
    case "confirm": return { ...state, items: state.items.map(i => i.kind === "confirm" && i.confirm.request_id === action.requestId ? { ...i, state: action.state, correction: action.correction } : i) };
    case "email": return { ...state, items: state.items.map(i => i.kind === "email" && i.draft.request_id === action.requestId
      ? { ...i, state: action.state ?? i.state, draft: action.draft ? { ...i.draft, ...action.draft } : i.draft, status: action.status ?? i.status } : i) };
    case "server": return applyEvent(state, action.event);
  }
}

function applyEvent(state: AgentState, ev: ServerEvent): AgentState {
  switch (ev.event) {
    case "message_start":
      if (state.items.some(i => i.kind === "agent" && i.id === ev.data.message_id)) return state;
      return { ...state, items: [...state.items, { kind: "agent", id: ev.data.message_id, text: "", streaming: true }] };
    case "message_delta": {
      const exists = state.items.some(i => i.kind === "agent" && i.id === ev.data.message_id);
      const items = exists ? state.items : [...state.items, { kind: "agent" as const, id: ev.data.message_id, text: "", streaming: true }];
      return { ...state, items: items.map(i => i.kind === "agent" && i.id === ev.data.message_id ? { ...i, text: i.text + ev.data.text } : i) };
    }
    case "message_end":
      return { ...state, items: state.items.map(i => i.kind === "agent" && i.id === ev.data.message_id ? { ...i, streaming: false } : i) };
    case "form": return { ...state, items: [...state.items, { kind: "form", id: ev.data.request_id, form: ev.data, state: "open" }] };
    case "confirm": return { ...state, items: [...state.items, { kind: "confirm", id: ev.data.request_id, confirm: ev.data, state: "open" }] };
    case "email_draft": return { ...state, items: [...state.items.filter(i => !(i.kind === "email" && i.draft.request_id === ev.data.request_id)), { kind: "email", id: ev.data.request_id, draft: ev.data, state: "open" }] };
    case "email_status": return agentReducer(state, { type: "email", requestId: ev.data.request_id, status: ev.data });
    case "estimate": {
      const id = ev.data.estimate_id;
      const items = state.items.some(i => i.kind === "estimate" && i.estimateId === id) ? state.items : [...state.items, { kind: "estimate" as const, id: `est-${id}`, estimateId: id }];
      return { ...state, items, estimates: { ...state.estimates, [id]: ev.data }, canvasOpen: true, canvasView: `estimate:${id}` };
    }
    case "search_step": {
      const s = ev.data;
      const found = state.items.find(i => i.kind === "search" && i.searchId === s.search_id);
      if (!found) return { ...state, items: [...state.items, { kind: "search", id: `search-${s.search_id}`, searchId: s.search_id, steps: [s] }] };
      return { ...state, items: state.items.map(i => i.kind === "search" && i.searchId === s.search_id
        ? { ...i, steps: i.steps.some(x => x.step_id === s.step_id) ? i.steps.map(x => x.step_id === s.step_id ? s : x) : [...i.steps, s] } : i) };
    }
    case "report": return { ...state, report: ev.data.report, canvasOpen: true, canvasView: "report" };
    case "error": return { ...state, items: [...state.items, { kind: "error", id: uid("err"), message: ev.data.message }] };
    case "done": return { ...state, busy: false, items: state.items.map(i => i.kind === "agent" && i.streaming ? { ...i, streaming: false } : i) };
  }
}
