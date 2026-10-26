// Scripted event replays for the built-in mock. All numbers here are fixture
// payloads standing in for the backend; components only display them.
import type { Plan, Report, ServerEvent, Source } from "../protocol";

const sbcAnthem: Source = { kind: "policy", label: "SBC p.3 · Anthem Gold 80", page: 3, quote: "Specialist visit: $50 copay; deductible does not apply." };
const sbcOscar: Source = { kind: "web", label: "hioscar.com · SBC p.2", page: 2, quote: "Specialist visit: 40% coinsurance after the $7,500 deductible.", url: "https://www.hioscar.com" };
const sbcBlue: Source = { kind: "policy", label: "SBC p.3 · Blue Shield", page: 3 };
const mrfAnthem: Source = { kind: "mrf", label: "Negotiated rate · Anthem" };
const directory: Source = { kind: "web", label: "Anthem provider directory", url: "https://www.anthem.com/find-care" };
const billPhoto: Source = { kind: "user_message", label: "Your bill photo" };
const sbcUrgent: Source = { kind: "policy", label: "SBC p.4 · urgent care and coinsurance", page: 4 };

export const mockPlans: Plan[] = [
  { plan_id: "anthem-gold-80", name: "Anthem Gold 80", insurer: "Anthem Blue Cross", plan_year: "2027" },
  { plan_id: "oscar-bronze-60", name: "Oscar Bronze 60", insurer: "Oscar", plan_year: "2027" },
  { plan_id: "blue-shield-silver-70", name: "Blue Shield Silver 70", insurer: "Blue Shield of California", plan_year: "2027" },
  { plan_id: "kaiser-silver-70-hmo", name: "Kaiser Silver 70 HMO", insurer: "Kaiser Permanente", plan_year: "2027" },
  { plan_id: "health-net-platinum-90", name: "Health Net Platinum 90", insurer: "Health Net", plan_year: "2027" },
];

/** Split text into a streamed agent message. */
export function say(id: string, text: string): ServerEvent[] {
  const parts = text.match(/.{1,24}(\s|$)|.+/g) ?? [text];
  return [
    { event: "message_start", data: { message_id: id } },
    ...parts.map(p => ({ event: "message_delta", data: { message_id: id, text: p } }) as ServerEvent),
    { event: "message_end", data: { message_id: id } },
  ];
}
const done: ServerEvent = { event: "done", data: {} };
const step = (search_id: string, step_id: string, text: string, extra: Partial<Extract<ServerEvent, { event: "search_step" }>["data"]> = {}): ServerEvent[] => [
  { event: "search_step", data: { search_id, step_id, status: "running", text, ...extra } },
  { event: "search_step", data: { search_id, step_id, status: extra.status ?? "done", text, ...extra } },
];

export const copay = (): ServerEvent[] => [
  ...step("s-copay", "1", "Read specialist benefits · SBC p.3", { kind: "document", title: "Looked up Anthem Gold 80 benefits" }),
  ...say("m-copay", "A specialist visit on Anthem Gold 80 costs you a **$50 copay**. The deductible doesn't apply, so that's what you pay from the first visit of the year."),
  done,
];

export const planQuestions = (): ServerEvent[] => [
  ...say("m-questions", "Three quick questions first — the answer depends mostly on how often you see a specialist."),
  { event: "form", data: {
    request_id: "f-plan", title: "About your care next year",
    questions: [
      { field: "conditions", kind: "multi", label: "Which conditions should the plan cover?", help: "Pick all that apply. I use this to price your expected care.",
        options: [{ value: "asthma", label: "Asthma" }, { value: "allergies", label: "Seasonal allergies" }, { value: "none", label: "Nothing ongoing" }],
        prefill: { value: ["asthma", "allergies"], source: "user_message" } },
      { field: "specialist_visits", kind: "single", label: "How many specialist visits do you expect next year?", help: "Pulmonologist or allergist check-ins count here.",
        options: [{ value: "0-2", label: "0–2" }, { value: "3-5", label: "3–5" }, { value: "6-10", label: "6–10" }] },
      { field: "budget", kind: "number", label: "What's the most you want to spend in a year?", help: "Premiums plus what you pay for care. Leave it blank if you're not sure.", unit: "Yearly budget", min: 0 },
    ],
  } },
  done,
];

const ids = ["anthem-gold-80", "oscar-bronze-60", "blue-shield-silver-70"] as const;
const plansBase: Report["plans"] = [
  { plan_id: ids[0], name: "Anthem Gold 80", insurer: "Anthem Blue Cross", network: "PPO", annual_premium: 5280, source: sbcAnthem },
  { plan_id: ids[1], name: "Oscar Bronze 60", insurer: "Oscar", network: "EPO", annual_premium: 3240, source: sbcOscar },
  { plan_id: ids[2], name: "Blue Shield Silver 70", insurer: "Blue Shield of California", network: "PPO", annual_premium: 4560, source: sbcBlue },
];
const totals: Record<string, number[]> = {
  [ids[0]]: [5640, 5840, 6040, 6240, 6440, 6640],
  [ids[1]]: [3620, 4520, 5410, 6300, 7190, 8080],
  [ids[2]]: [4860, 5400, 5940, 6480, 7020, 7560],
};
const cell = (text: string, source?: Source) => ({ text, source: source ?? null });
const row = (key: string, label: string, a: string, o: string, b: string, highlight = false) =>
  ({ key, label, highlight, cells: { [ids[0]]: cell(a, sbcAnthem), [ids[1]]: cell(o, sbcOscar), [ids[2]]: cell(b, sbcBlue) } });

export const comparisonReport: Report = {
  report_id: "r-compare",
  title: "2027 plan comparison",
  recommendation: {
    plan_id: ids[0],
    headline: "Anthem Gold 80 is the cheapest for you at 6 specialist visits a year, and it caps your worst year at $4,000.",
    reasons: ["$50 specialist copay, no deductible for visits · out-of-pocket max $4,000"],
    excluded: [
      { plan_id: "kaiser-silver-70-hmo", reason: "your pulmonologist, Dr. Kim, isn't in its network." },
      { plan_id: "health-net-platinum-90", reason: "the premium alone is $8,900 a year, over your $7,000 budget." },
    ],
  },
  ranking: { method: "year_total", summary: "Premiums plus what you pay for care, at 0–10 visits a year. You said 6–10.", factors: [] },
  note: "Highlighted rows matter most for asthma. Oscar's drug list for 2027 isn't published yet, so the inhaler price is unknown.",
  plans: plansBase,
  scenarios: [0, 2, 4, 6, 8, 10].map((v, i) => ({
    key: `visits-${v}`, label: String(v), description: `${v} specialist visits`,
    costs: Object.fromEntries(ids.map(id => [id, { mid: totals[id]![i]! }])),
  })),
  comparison: [
    row("premium", "Monthly premium", "$440", "$270", "$380"),
    row("primary_care", "Primary care visit", "$25", "$65", "$45"),
    row("specialist", "Specialist visit (pulmonologist)", "$50", "$190–$230", "$120", true),
    row("inhaler", "Controller inhaler, 30 days", "$15", "No data", "$35", true),
    row("er", "Emergency room", "$350", "Full price until deductible", "$400"),
    row("oop_max", "Worst-case year (out-of-pocket max)", "$4,000", "$9,200", "$8,700"),
  ],
};
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const { ranking: _r, note: _n, ...compareRest } = comparisonReport;
export const buildingReport: Report = { ...compareRest, recommendation: null, scenarios: [], comparison: [] };

const planSteps = [
  "Found 2027 plans for a single adult in 90024 … 9 plans",
  "Removed plans over budget or without your pulmonologist … 3 left",
  "Read the summary of benefits for each plan … 3 SBCs",
  "Searched the web for Oscar Bronze plan documents … 3 pages",
  "Computed yearly cost for 0–10 specialist visits",
] as const;
export const webSearch = (): ServerEvent[] => [
  ...step("s-web", "q", "Oscar Bronze 60 2027 Los Angeles summary of benefits and coverage", { kind: "web", title: "Oscar Bronze plan documents" }),
  ...step("s-web", "p1", "Read page 2 of the summary of benefits", { kind: "web", title: "Bronze 60 — Summary of Benefits and Coverage, 2027", url: "https://www.hioscar.com/plans/2027/bronze-60-sbc" }),
  ...step("s-web", "p2", "Opened the plan finder", { kind: "web", title: "Shop and compare 2027 plans in Los Angeles", url: "https://www.coveredca.com/shop-and-compare" }),
  ...step("s-web", "p3", "2027 drug list not published yet", { kind: "web", title: "2027 drug list (formulary) — not yet published", url: "https://www.hioscar.com/plans/2027/formulary", status: "failed" }),
];
/** Plan comparison up to the partial report (board 4 "page being built"). */
export const comparisonBuilding = (): ServerEvent[] => [
  ...planSteps.slice(0, 3).flatMap((t, i) => step("s-plan", String(i + 1), t, { kind: "document" })),
  ...webSearch(),
  ...step("s-plan", "4", planSteps[3], { kind: "document" }),
  { event: "report", data: { report: buildingReport } },
  { event: "search_step", data: { search_id: "s-plan", step_id: "5", status: "running", text: planSteps[4], kind: "document" } },
];
export const comparison = (): ServerEvent[] => [
  ...comparisonBuilding(),
  { event: "search_step", data: { search_id: "s-plan", step_id: "5", status: "done", text: planSteps[4], kind: "document" } },
  { event: "report", data: { report: comparisonReport } },
  ...say("m-pick", "Pick **Anthem Gold 80**. At 6 specialist visits it costs about **$6,240** for the year, and your worst case is **$4,000** out of pocket."),
  done,
];

export const careReport: Report = {
  report_id: "r-care", title: "Asthma specialists near 90024", recommendation: null, plans: [], scenarios: [], comparison: [],
  care: {
    query: { service: "Asthma specialist", description: "Pulmonology or allergy and immunology", location: { label: "Westwood · 90024", zip: "90024", lat: 34.0635, lng: -118.4455 }, plan_id: ids[0], plan_label: "Anthem Gold 80", max_miles: 6 },
    note: "Dr. Sarah Kim is the closest in-network pulmonologist, 0.6 miles away, and costs you $50 a visit.",
    providers: [
      { provider_id: "kim", name: "Dr. Sarah Kim", kind: "doctor", specialty: "Pulmonology · Westwood", address: "Westwood, Los Angeles, CA", lat: 34.0617, lng: -118.4433, distance_miles: 0.6, in_network: true, patient_cost: { mid: 50 }, price_vs_typical: "typical", quality: { score: 4.7, label: "4.7 from 88 reviews" }, recommended: true, reason: "Closest, and the one you already see. Next opening Nov 4.", source: directory },
      { provider_id: "rahimi", name: "Dr. Ahmed Rahimi", kind: "doctor", specialty: "Allergy and immunology · Brentwood", address: "Brentwood, Los Angeles, CA", lat: 34.0597, lng: -118.4757, distance_miles: 1.4, in_network: true, patient_cost: { mid: 50 }, price_vs_typical: "typical", quality: { score: 4.5, label: "4.5 from 61 reviews" }, reason: "Treats allergic asthma; evening hours on Tuesdays.", source: directory },
      { provider_id: "chen", name: "Dr. Laura Chen", kind: "doctor", specialty: "Pulmonology · Santa Monica", address: "Santa Monica, CA", lat: 34.0195, lng: -118.4912, distance_miles: 4.8, in_network: true, patient_cost: { mid: 50 }, price_vs_typical: "typical", quality: { score: 4.9, label: "4.9 from 212 reviews" }, reason: "Highest patient rating nearby (4.9 from 212 reviews).", source: directory },
      { provider_id: "ortiz", name: "Dr. Michael Ortiz", kind: "clinic", specialty: "Allergy and immunology · Culver City", address: "Culver City, CA", lat: 34.0211, lng: -118.3965, distance_miles: 5.2, in_network: true, network_tier: "Hospital outpatient", patient_cost: { low: 85, mid: 110, high: 140 }, price_vs_typical: "higher", quality: { score: null, label: "No public data" }, reason: "Bills visits as a hospital outpatient clinic.", source: mrfAnthem },
    ],
  },
};
export const specialists = (): ServerEvent[] => [
  ...["Searched Anthem provider directory", "Filtered specialists near 90024", "Checked plan copays", "Reviewed public quality data"].flatMap((t, i) => step("s-care", String(i + 1), t, { kind: "document" })),
  { event: "report", data: { report: careReport } },
  ...say("m-care", "I found 4 in-network asthma specialists within 6 miles of 90024. **Dr. Sarah Kim** is the closest, and a visit costs you the **$50 specialist copay**."),
  done,
];

export const bill = (fileId = "file-bill", fileName = "urgent-care-bill.jpg"): ServerEvent[] => [
  ...["Read your bill photo … 4 lines", "Looked up Anthem’s negotiated rates", "Rate for 71046 — not in the published file", "Applied copay and 20% coinsurance"].flatMap((t, i) => step("s-bill", String(i + 1), t, { kind: "document", status: i === 2 ? "failed" : "done" })),
  { event: "estimate", data: {
    estimate_id: "e-bill", title: "Bill review", plan_name: "Anthem Gold 80", status: "estimated",
    patient_cost: { mid: 67.2 },
    rows: [
      { label: "99214 · Office visit, established patient", amount: 50, note: "Billed $285.00 · negotiated rate $168.00", source: billPhoto },
      { label: "94640 · Nebulizer treatment", amount: 12.8, note: "Billed $420.00 · negotiated rate $64.00 × 20% coinsurance", source: mrfAnthem },
      { label: "J7613 · Albuterol, per dose", amount: 4.4, note: "Billed $38.00 · negotiated rate $22.00", source: mrfAnthem },
      { label: "71046 · Chest X-ray, 2 views", amount: null, note: "Billed $310.00 · no negotiated rate on file" },
      { label: "Possible overcharge on 94640", amount: 407.2, note: "$420.00 billed vs $12.80 owed", source: sbcUrgent },
    ],
    assumptions: ["Negotiated rates are published by your insurer; the final claim may differ.", "The X-ray (71046) is excluded until its rate is confirmed."],
  } },
  ...say("m-bill", "You may have been overcharged **$407.20**. One line looks wrong: the nebulizer treatment was billed at full price. Under your plan you should owe **$12.80** for it, not $420. I drafted an appeal email below — nothing is sent until you press Send."),
  { event: "email_draft", data: {
    request_id: "em-appeal", title: "Appeal to Westwood Urgent Care", from: "you@gmail.com",
    to: ["billing@westwoodurgentcare.example"], cc: [],
    subject: "Request for review — September 14, 2026 visit (CPT 94640)",
    body: "To: Westwood Urgent Care Billing Department\n\nPlease review the charge for CPT 94640 (nebulizer treatment). My bill lists $420.00. The negotiated rate is $64.00, and 20% coinsurance would make my responsibility $12.80, a difference of $407.20.\n\nPlease verify the applicable rate against my final explanation of benefits, correct any billing error, and send an itemized revised statement. The rate for CPT 71046 is not available and also needs confirmation.\n\nThank you,\n[Your name]\n[Member ID / claim number]",
    attachments: [{ file_id: fileId, name: fileName }],
    sources: [billPhoto, mrfAnthem, sbcUrgent],
  } },
  done,
];

export const emailSent = (request_id: string, to: string[]): ServerEvent[] => [
  { event: "email_status", data: { request_id, status: "sending" } },
  { event: "email_status", data: { request_id, status: "sent", sent_at: "2026-10-26T22:40:00Z" } },
  ...say("m-sent", `Sent to ${to.join(", ")}. I'll help you follow up if they don't reply within 30 days.`),
  done,
];

/** Pick a script for a free-text message, like the backend router would. */
export function scriptForMessage(text: string, hasImage: boolean): (() => ServerEvent[]) | null {
  if (hasImage || /bill|appeal|账单|申诉/i.test(text)) return () => bill();
  if (/doctor|specialist near|near campus|医生|专科医生/i.test(text)) return specialists;
  if (/copay|specialist visit|共付|自付/i.test(text)) return copay;
  if (/plan|compare|asthma|保险|方案|哮喘/i.test(text)) return planQuestions;
  return null;
}

export interface MockBoard { n: number; title: string; userText?: string; withImage?: boolean; events: () => ServerEvent[]; busy?: boolean; view?: string }
/** Dev-only replay of the eight design states. */
export const mockBoards: MockBoard[] = [
  { n: 1, title: "1 · Empty state", events: () => [] },
  { n: 2, title: "2 · Chat only — quick answer", userText: "What's my copay for a specialist visit on Anthem Gold 80?", events: copay },
  { n: 3, title: "3 · Question card", userText: "I'm a PhD student at UCLA with mild asthma. Which 2027 plan should I pick?", events: planQuestions },
  { n: 4, title: "4 · Canvas — page being built", userText: "Asthma, Seasonal allergies · 6–10 · $7,000", events: comparisonBuilding, busy: true },
  { n: 5, title: "5 · Canvas — plan comparison", userText: "Asthma, Seasonal allergies · 6–10 · $7,000", events: comparison },
  { n: 6, title: "6 · Canvas — asthma specialists", userText: "Can you find me an asthma specialist near campus?", events: specialists },
  { n: 7, title: "7 · Canvas — bill review", userText: "Got this from urgent care after my asthma flare. Does it look right?", withImage: true, events: () => bill() },
  { n: 8, title: "8 · Canvas — web search step detail", userText: "Asthma, Seasonal allergies · 6–10 · $7,000", events: comparison, view: "search:s-web" },
];
