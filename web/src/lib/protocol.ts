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

export type TurnInput =
  | { type: "message"; text: string }
  | { type: "form_response"; request_id: string; answers: Record<string, Answer> }
  | { type: "form_dismiss"; request_id: string }
  | { type: "confirm_response"; request_id: string; confirmed: boolean; correction?: string }

export interface ReplyTarget {
  kind: "dashboard_item"
  ref: string
  label: string
}

export interface TurnRequest {
  session_id: string
  input: TurnInput
  reply_target?: ReplyTarget
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
  step_id: string
  status: StepStatus
  text: string
  url?: string | null
  title?: string | null
  // JPEG data URL of the page at this step (live search worker only).
  screenshot?: string | null
}

// Final result of the live search worker (search_worker/worker.py). The agent
// service turns it into the assistant's reply; it is not sent to the browser.
export interface SearchSummary {
  insurer: string
  status: "found" | "partial" | "blocked" | "not_found" | "error"
  message: string
  facts: { label: string; value: string }[]
  pages: { title: string; url: string }[]
}

export interface ReportPlan {
  plan_id: string
  name: string
  insurer: string
  network: string
  annual_premium: number
  source?: Source
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
  cells: Record<string, { text: string; source?: Source }>
}

export interface Report {
  report_id: string
  title: string
  recommendation: {
    plan_id: string
    headline: string
    reasons: string[]
    excluded: { plan_id: string; reason: string }[]
  } | null
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
