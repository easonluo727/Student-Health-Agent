// Mirrors docs/frontend-protocol.md. Change both together.

export type SourceKind = "policy" | "mrf" | "web" | "user_form" | "user_message" | "inferred"

export interface Source {
  kind: SourceKind
  label: string
  page?: number | null
  quote?: string | null
  url?: string | null
}

// ---- Requests ----

export type AnswerValue = string | number | string[] | null

export interface Answer {
  value: AnswerValue
  unknown: boolean
}

// A file uploaded with POST {API_BASE}/upload, attached to the next message.
export interface Attachment {
  file_id: string
  name: string
}

export interface UploadResult extends Attachment {
  size: number
}

export type TurnInput =
  | { type: "message"; text: string; attachments?: Attachment[] }
  | { type: "form_response"; request_id: string; answers: Record<string, Answer> }
  | { type: "form_dismiss"; request_id: string }
  | { type: "confirm_response"; request_id: string; confirmed: boolean; correction?: string }

export interface ReplyTarget {
  kind: "dashboard_item"
  ref: string
  label: string
}

// The feature the user picked on the home screen, sent with every message.
export type Mode = "calculate" | "review"

export interface TurnContext {
  mode: Mode
  plan_ids?: string[]
}

export interface TurnRequest {
  session_id: string
  input: TurnInput
  reply_target?: ReplyTarget
  context?: TurnContext
}

// A plan in the verified library, from GET {API_BASE}/plans.
export interface Plan {
  plan_id: string
  name: string
  insurer: string
  plan_year: string
}

// ---- Event payloads ----

export interface Option {
  value: string
  label: string
}

export type QuestionKind = "single" | "multi" | "number" | "text" | "weight"

export interface Question {
  field: string
  kind: QuestionKind
  // Shown once above a run of questions that share it (the priority ratings: "What matters most to you?").
  heading?: string
  label: string
  help?: string
  options?: Option[]
  unit?: string
  min?: number
  max?: number
  scale_labels?: [string, string]
  prefill?: { value: AnswerValue; source: "user_message" }
}

export interface Form {
  request_id: string
  title: string
  intro?: string
  questions: Question[]
}

export interface ConfirmItem {
  label: string
  value: string
  source: SourceKind
}

export interface Confirm {
  request_id: string
  title: string
  items: ConfirmItem[]
}

export type EstimateStatus =
  | "estimated"
  | "needs_information"
  | "needs_review"
  | "not_covered"
  | "invalid_input"

export interface CostRange {
  low?: number
  mid: number
  high?: number
}

export interface EstimateRow {
  label: string
  amount: number | null
  note?: string
  source?: Source
}

export interface Estimate {
  estimate_id: string
  title: string
  plan_name: string
  status: EstimateStatus
  patient_cost: CostRange | null
  rows: EstimateRow[]
  assumptions: string[]
  message?: string | null
}

export type StepStatus = "running" | "done" | "failed"

export interface SearchStep {
  search_id: string
  // "document" when the steps are the agent reading an uploaded PDF; web search otherwise.
  kind?: "web" | "document"
  step_id: string
  status: StepStatus
  text: string
  url?: string | null
  title?: string | null
  // JPEG data URL of the page at this step.
  screenshot?: string | null
}

export interface ReportPlan {
  plan_id: string
  name: string
  insurer: string
  network: string
  // null when the premium can't be priced yet (age, ZIP or student level missing) or isn't published.
  annual_premium: number | null
  source?: Source | null
}

export interface Scenario {
  key: string
  label: string
  description: string
  costs: Record<string, CostRange>
}

export interface ComparisonRow {
  key: string
  label: string
  highlight: boolean
  // detail: how the cell was computed, e.g. "10 x $25.00 = $250.00. Primary care visit: $25.00 copay".
  cells: Record<string, { text: string; detail?: string | null; source?: Source | null }>
}

// How the plans were ranked, computed in Python (plan_db.rank_plans).
export interface RankingFactor {
  factor: string
  label: string
  answer: number | null // the user's 1-5 rating; null = not rated
  weight: number
  used: boolean
  reason: string | null // why a factor is not scored
}

export interface Ranking {
  method: "weighted_score" | "year_total" | "visits_only"
  summary: string
  factors: RankingFactor[]
}

// ---- Care section: one service priced at specific providers near the user (roadmap step 1) ----

// What the agent understood the user is looking for, shown as the dashboard's query bar. Clicking a field asks the
// agent to change it; the dashboard never edits it itself.
export interface CareQuery {
  service: string // e.g. "Knee MRI"
  code?: string | null // billing code, shown as a badge
  description?: string | null
  location?: { label: string; zip?: string | null; lat: number; lng: number } | null
  plan_id?: string | null
  plan_label?: string | null // e.g. "UC SHIP · Network tier"
  max_miles?: number | null
}

// The spread of this service's price across the network's providers. Python computes the bins.
export interface PriceSummary {
  label: string // what the midpoint is, e.g. "Typical negotiated price in your network"
  midpoint: number
  min: number
  max: number
  bins: { price: number; count: number }[]
  source?: Source | null
}

export type ProviderKind = "doctor" | "hospital" | "clinic" | "imaging_center" | "lab" | "urgent_care" | "other"

export interface ProviderQuality {
  score?: number | null // 0-5; null when there is no public score
  label: string // e.g. "4.6 · CMS star rating", or "No public rating"
  source?: Source | null
}

export interface Provider {
  provider_id: string // the NPI when there is one
  name: string
  kind: ProviderKind
  specialty?: string | null
  address: string
  lat: number
  lng: number
  distance_miles?: number | null
  in_network: boolean | null // null = not known
  network_tier?: string | null
  allowed_amount?: number | null // this provider's negotiated rate
  patient_cost?: CostRange | null // what the user pays here, from the calculator
  price_vs_typical?: "lower" | "typical" | "higher" | null
  quality?: ProviderQuality | null
  recommended?: boolean
  reason?: string | null // why the agent recommends this provider
  source?: Source | null
}

export interface CareSection {
  query: CareQuery
  price?: PriceSummary | null
  providers: Provider[]
  note?: string | null // e.g. "What you pay assumes your deductible is met"
}

// The dashboard. The backend sends what it has: `care` (a service priced at providers, with the map) and/or the
// plan comparison (`plans` + `scenarios` + `comparison`, which may be empty when there is only `care`).
export interface Report {
  report_id: string
  title: string
  recommendation: {
    plan_id: string
    headline: string
    reasons: string[]
    excluded: { plan_id: string; reason: string }[]
  } | null
  ranking?: Ranking
  note?: string
  care?: CareSection | null
  plans: ReportPlan[]
  scenarios: Scenario[]
  comparison: ComparisonRow[]
}

export type ServerEvent =
  | { event: "message_start"; data: { message_id: string } }
  | { event: "message_delta"; data: { message_id: string; text: string } }
  | { event: "message_end"; data: { message_id: string } }
  | { event: "form"; data: Form }
  | { event: "confirm"; data: Confirm }
  | { event: "estimate"; data: Estimate }
  | { event: "search_step"; data: SearchStep }
  | { event: "report"; data: { report: Report } }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: Record<string, never> }
