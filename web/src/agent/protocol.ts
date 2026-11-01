// Wire protocol spoken by the FastAPI agent. Mirrors docs/frontend-protocol.md; change both together.
export type SourceKind = "policy" | "mrf" | "web" | "user_form" | "user_message" | "inferred"
export interface Source { kind: SourceKind; label: string; page?: number | null; quote?: string | null; url?: string | null }

export type AnswerValue = string | number | string[] | null
export interface Answer { value: AnswerValue; unknown: boolean }
export interface Attachment { file_id: string; name: string }
export interface UploadResult extends Attachment { size: number; content_type: string }

export type TurnInput =
  | { type: "message"; text: string; attachments?: Attachment[] }
  | { type: "form_response"; request_id: string; answers: Record<string, Answer> }
  | { type: "form_dismiss"; request_id: string }
  | { type: "confirm_response"; request_id: string; confirmed: boolean; correction?: string }
  | { type: "email_response"; request_id: string; action: "send" | "cancel"; draft?: EmailDraftEdit }

export interface ReplyTarget { kind: "dashboard_item"; ref: string; label: string }
export type Mode = "calculate" | "review"
export interface TurnContext { mode: Mode; plan_ids?: string[] }
export interface TurnRequest { session_id: string; input: TurnInput; reply_target?: ReplyTarget; context?: TurnContext }
export interface Plan {
  plan_id: string; name: string; insurer: string; plan_year: string
  verified: boolean; uploaded: boolean
}

export interface Option { value: string; label: string }
export type QuestionKind = "single" | "multi" | "number" | "text" | "weight"
export interface Question {
  field: string; kind: QuestionKind
  heading?: string // shown once above a run of questions sharing it
  label: string; help?: string; options?: Option[]; unit?: string; min?: number; max?: number
  scale_labels?: [string, string] // weight = 1..5, e.g. ["Don't care", "Very important"]
  prefill?: { value: AnswerValue; source: "user_message" } // show "From your message, please confirm"
}
export interface Form { request_id: string; title: string; intro?: string; questions: Question[] }
export interface ConfirmItem { label: string; value: string; source: SourceKind }
export interface Confirm { request_id: string; title: string; items: ConfirmItem[] }

export type EstimateStatus = "estimated" | "needs_information" | "needs_review" | "not_covered" | "invalid_input"
export interface CostRange { low?: number; mid: number; high?: number } // only mid → one number; low+high → a range
export interface EstimateRow { label: string; amount: number | null; note?: string; source?: Source }
export interface Estimate {
  estimate_id: string; title: string; plan_name: string; status: EstimateStatus
  patient_cost: CostRange | null // null unless status = "estimated"; then show `message`
  rows: EstimateRow[]; assumptions: string[]; message?: string | null
}

export type StepStatus = "running" | "done" | "failed"
export interface SearchStep {
  search_id: string; kind?: "web" | "document"; step_id: string; status: StepStatus; text: string
  url?: string | null; title?: string | null; screenshot?: string | null // JPEG data URL
}

export interface ReportPlan { plan_id: string; name: string; insurer: string; network: string; annual_premium: number | null; source?: Source | null }
export interface Scenario { key: string; label: string; description: string; costs: Record<string, CostRange> } // costs keyed by plan_id; year totals
export interface ComparisonRow { key: string; label: string; highlight: boolean; cells: Record<string, { text: string; detail?: string | null; source?: Source | null }> }
export interface RankingFactor { factor: string; label: string; answer: number | null; weight: number; used: boolean; reason: string | null }
export interface Ranking { method: "weighted_score" | "year_total" | "visits_only"; summary: string; factors: RankingFactor[] }

export interface CareQuery {
  service: string; code?: string | null; description?: string | null
  location?: { label: string; zip?: string | null; lat: number; lng: number } | null
  plan_id?: string | null; plan_label?: string | null; max_miles?: number | null
}
export interface PriceSummary { label: string; midpoint: number; min: number; max: number; bins: { price: number; count: number }[]; source?: Source | null }
export type ProviderKind = "doctor" | "hospital" | "clinic" | "imaging_center" | "lab" | "urgent_care" | "other"
export interface ProviderQuality { score?: number | null; label: string; source?: Source | null } // score 0-5; null → "No public rating"
export interface Provider {
  provider_id: string; name: string; kind: ProviderKind; specialty?: string | null; address: string; lat: number; lng: number
  distance_miles?: number | null; in_network: boolean | null; network_tier?: string | null
  allowed_amount?: number | null; patient_cost?: CostRange | null // null → "Price unknown", map label "—"
  price_vs_typical?: "lower" | "typical" | "higher" | null; quality?: ProviderQuality | null
  recommended?: boolean // "Agent pick" badge, highlighted on map
  reason?: string | null; source?: Source | null
}
export interface CareSection { query: CareQuery; price?: PriceSummary | null; providers: Provider[]; note?: string | null }

export interface Report {
  report_id: string
  title: string // use as the canvas title
  recommendation: { plan_id: string; headline: string; reasons: string[]; excluded: { plan_id: string; reason: string }[] } | null
  ranking?: Ranking; note?: string; care?: CareSection | null
  plans: ReportPlan[]; scenarios: Scenario[]; comparison: ComparisonRow[] // may be empty when only `care` is present
}

// ---- Gmail appeal email (backend not built yet; the mock replays it) ----
export interface EmailDraft {
  request_id: string
  title: string                 // e.g. "Appeal to Anthem Blue Cross"
  from: string | null           // the connected Gmail address; null = Gmail not connected
  to: string[]; cc?: string[]
  subject: string
  body: string                  // plain text; editable in the card
  attachments?: Attachment[]    // e.g. the uploaded bill
  sources?: Source[]            // why each claim in the letter is true, rendered as chips
}
export interface EmailDraftEdit { to: string[]; cc?: string[]; subject: string; body: string }
export type EmailSendStatus = "sending" | "sent" | "failed"
export interface EmailStatus { request_id: string; status: EmailSendStatus; message?: string | null; sent_at?: string | null }
// ---- Google sign-in (optional; GET /auth/me, POST /auth/google, POST /auth/logout) ----
export interface User { user_id: string; email: string; name: string | null; picture: string | null }
export interface AuthState { user: User | null; google_client_id: string | null } // client id null → sign-in off

export interface Integrations { gmail: { connected: boolean; email: string | null; connect_url: string | null } }

export type ServerEvent =
  | { event: "message_start"; data: { message_id: string } }
  | { event: "message_delta"; data: { message_id: string; text: string } }
  | { event: "message_end"; data: { message_id: string } }
  | { event: "form"; data: Form }
  | { event: "confirm"; data: Confirm }
  | { event: "estimate"; data: Estimate }
  | { event: "search_step"; data: SearchStep }
  | { event: "report"; data: { report: Report } }
  | { event: "email_draft"; data: EmailDraft }
  | { event: "email_status"; data: EmailStatus }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: Record<string, never> }
