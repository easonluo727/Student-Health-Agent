// Mock backend script that replays the demo flow in docs/PRODUCT.md.
// Every number here is a PLACEHOLDER for UI development, not plan data.
// Real numbers come from the FastAPI service (Python calculator + catalogs).

import type { Answer, Report, SearchSummary, ServerEvent, Source, TurnRequest } from "./protocol"

export type MockStep = ServerEvent | { pause: number }

const mockSource = (label: string, page?: number): Source => ({
  kind: "policy",
  label: `${label} (mock)`,
  page: page ?? null,
  quote: "Placeholder quote. The real backend returns the policy text here.",
})
const mockRate: Source = {
  kind: "mrf",
  label: "Allowed-amount DB (mock)",
  quote: "Placeholder negotiated rate.",
}

let counter = 0
const nextId = (prefix: string) => `${prefix}_${Date.now().toString(36)}_${counter++}`

// Split text into small chunks so the UI shows real streaming.
function say(text: string): MockStep[] {
  const id = nextId("msg")
  const steps: MockStep[] = [{ event: "message_start", data: { message_id: id } }]
  for (const chunk of text.match(/[\s\S]{1,12}/g) ?? []) {
    steps.push({ event: "message_delta", data: { message_id: id, text: chunk } })
    steps.push({ pause: 25 })
  }
  steps.push({ event: "message_end", data: { message_id: id } })
  return steps
}

const usageForm: MockStep = {
  event: "form",
  data: {
    request_id: "form_usage",
    title: "How you use care",
    intro: "I'll price UC SHIP against the care you actually expect this year.",
    questions: [
      {
        field: "conditions",
        kind: "multi",
        label: "Any ongoing conditions?",
        options: [
          { value: "asthma", label: "Asthma" },
          { value: "diabetes", label: "Diabetes" },
          { value: "mental_health", label: "Anxiety / depression" },
          { value: "none", label: "None" },
        ],
        prefill: { value: ["asthma"], source: "user_message" },
      },
      {
        field: "specialist_visits_per_year",
        kind: "number",
        label: "Specialist visits you expect this year",
        help: "For example, asthma follow-ups with a pulmonologist.",
        unit: "visits",
        min: 0,
        max: 50,
      },
      {
        field: "urgent_care_visits_per_year",
        kind: "number",
        label: "Urgent care visits you expect",
        help: "Colds, flu, minor injuries.",
        unit: "visits",
        min: 0,
        max: 20,
      },
      {
        field: "er_visits_per_year",
        kind: "number",
        label: "Emergency room visits you expect",
        help: "Last year counts as a guide, e.g. a sports injury.",
        unit: "visits",
        min: 0,
        max: 10,
      },
      {
        field: "regular_rx",
        kind: "single",
        label: "Do you take a maintenance medication?",
        options: [
          { value: "generic", label: "Yes, generic" },
          { value: "brand", label: "Yes, brand-name (e.g. an inhaler)" },
          { value: "no", label: "No" },
        ],
      },
      {
        field: "care_location",
        kind: "single",
        label: "Where do you usually go first?",
        help: "Options come from UC SHIP's network tiers.",
        options: [
          { value: "ashe", label: "Ashe Student Health Center" },
          { value: "uc_family", label: "UC Family provider" },
          { value: "anthem_ppo", label: "Anthem PPO provider" },
          { value: "oon", label: "Out of network" },
        ],
      },
    ],
  },
}

// Sent only when the agent inferred something from chat. Form answers skip it.
const erConfirm: MockStep = {
  event: "confirm",
  data: {
    request_id: "confirm_er",
    title: "I filled in a few things — is this right?",
    items: [
      { label: "Plan", value: "UC SHIP 2026–27", source: "user_message" },
      { label: "Service", value: "ER visit + ankle X-ray (not admitted)", source: "inferred" },
      { label: "Network", value: "In network (UCLA Health ER)", source: "inferred" },
      { label: "Price", value: "UCLA Health negotiated rate", source: "mrf" },
    ],
  },
}

const ER_ESTIMATE = () => ({
  event: "estimate" as const,
  data: {
    estimate_id: nextId("est"),
    title: "ER visit + ankle X-ray",
    plan_name: "UC SHIP",
    status: "estimated" as const,
    patient_cost: { low: 100, mid: 150, high: 210 },
    rows: [
      { label: "Allowed amount (ER)", amount: 1400, note: "Estimated range, no exact rate", source: mockRate },
      { label: "Allowed amount (X-ray)", amount: 120, note: "Negotiated rate", source: mockRate },
      { label: "ER copay", amount: 100, note: "Waived if admitted", source: mockSource("UC SHIP SBC", 4) },
      { label: "Coinsurance on X-ray", amount: 50, note: "Mid estimate", source: mockSource("UC SHIP SBC", 5) },
    ],
    assumptions: ["In-network ER", "Not admitted", "Range because the ER rate is estimated"],
  },
})

function count(answers: Record<string, Answer>, field: string) {
  const a = answers[field]
  return a && !a.unknown && a.value != null ? Number(a.value) : null
}

function estimates(answers: Record<string, Answer>): MockStep[] {
  const per = (field: string) => {
    const n = count(answers, field)
    return n == null ? "You weren't sure how many; priced per visit" : `Per visit; you expect ${n}`
  }
  const rows = (allowed: number, cost: number, rule: string) => [
    { label: "Allowed amount", amount: allowed, note: "Negotiated rate", source: mockRate },
    { label: "Deductible applied", amount: 0, note: "Deductible waived", source: mockSource("UC SHIP SBC", 3) },
    { label: "Your share", amount: cost, note: rule, source: mockSource("UC SHIP SBC", 3) },
  ]
  return [
    {
      event: "estimate",
      data: {
        estimate_id: nextId("est"),
        title: "Specialist visit (asthma)",
        plan_name: "UC SHIP",
        status: "estimated",
        patient_cost: { mid: 20 },
        rows: rows(250, 20, "Flat copay"),
        assumptions: ["UC Family provider", per("specialist_visits_per_year")],
      },
    },
    { pause: 250 },
    {
      event: "estimate",
      data: {
        estimate_id: nextId("est"),
        title: "Urgent care (flu)",
        plan_name: "UC SHIP",
        status: "estimated",
        patient_cost: { mid: 25 },
        rows: rows(190, 25, "Flat copay"),
        assumptions: ["In-network urgent care", per("urgent_care_visits_per_year")],
      },
    },
    { pause: 250 },
    ER_ESTIMATE(),
  ]
}

const prefsForm: MockStep = {
  event: "form",
  data: {
    request_id: "form_prefs",
    title: "What matters to you",
    intro: "I'll weigh plans the way you would.",
    questions: [
      {
        field: "monthly_budget",
        kind: "number",
        label: "Max monthly premium you're comfortable with",
        unit: "USD / month",
        min: 0,
        max: 2000,
      },
      {
        field: "weight_premium",
        kind: "weight",
        label: "Low monthly premium",
        scale_labels: ["Don't care", "Very important"],
      },
      {
        field: "weight_worst_case",
        kind: "weight",
        label: "Protection in a bad year (low out-of-pocket max)",
        scale_labels: ["Don't care", "Very important"],
      },
      {
        field: "weight_keep_doctors",
        kind: "weight",
        label: "Keeping UCLA Health doctors in network",
        scale_labels: ["Don't care", "Very important"],
      },
    ],
  },
}


const mockReport: Report = {
  report_id: "arena_mock",
  title: "Insurance Arena",
  recommendation: {
    plan_id: "uc_ship",
    headline: "Stay on UC SHIP — it costs the least in every scenario and keeps your UCLA doctors.",
    reasons: [
      "Lowest total cost in your expected year (placeholder figures).",
      "Your pulmonologist is a UC Family provider, the cheapest tier.",
      "Out-of-pocket max is the lowest of the plans compared.",
    ],
    excluded: [
      { plan_id: "kaiser_silver", reason: "UCLA Health doctors are out of network." },
    ],
  },
  plans: [
    { plan_id: "uc_ship", name: "UC SHIP", insurer: "Anthem Blue Cross", network: "PPO", annual_premium: 3600, source: mockSource("UC SHIP rates") },
    { plan_id: "anthem_silver", name: "Silver PPO", insurer: "Anthem Blue Cross", network: "PPO", annual_premium: 5400, source: mockSource("Anthem SBC") },
    { plan_id: "blue_shield_silver", name: "Silver 70 PPO", insurer: "Blue Shield of CA", network: "PPO", annual_premium: 5100, source: mockSource("Blue Shield SBC") },
    { plan_id: "kaiser_silver", name: "Silver 70 HMO", insurer: "Kaiser Permanente", network: "HMO", annual_premium: 4300, source: mockSource("Kaiser SBC") },
  ],
  scenarios: [
    {
      key: "healthy",
      label: "Healthy year",
      description: "Annual checkup plus one or two routine visits.",
      costs: {
        uc_ship: { mid: 3620 },
        anthem_silver: { mid: 5450 },
        blue_shield_silver: { mid: 5160 },
        kaiser_silver: { mid: 4340 },
      },
    },
    {
      key: "expected",
      label: "Your expected year",
      description: "2 specialist visits, inhaler refills, 1 urgent care, 1 ER visit with X-ray.",
      costs: {
        uc_ship: { low: 3900, mid: 3960, high: 4030 },
        anthem_silver: { low: 6500, mid: 6800, high: 7150 },
        blue_shield_silver: { low: 6200, mid: 6450, high: 6700 },
        kaiser_silver: { low: 5000, mid: 5150, high: 5300 },
      },
    },
    {
      key: "worst",
      label: "Bad year",
      description: "ER visit followed by a 3-day hospital stay. Capped at the out-of-pocket max.",
      costs: {
        uc_ship: { mid: 7600 },
        anthem_silver: { mid: 14100 },
        blue_shield_silver: { mid: 13800 },
        kaiser_silver: { mid: 12100 },
      },
    },
  ],
  comparison: [
    {
      key: "deductible",
      label: "Deductible",
      highlight: false,
      cells: {
        uc_ship: { text: "$0 in network", source: mockSource("UC SHIP SBC", 1) },
        anthem_silver: { text: "$4,750", source: mockSource("Anthem SBC", 1) },
        blue_shield_silver: { text: "$4,500", source: mockSource("Blue Shield SBC", 1) },
        kaiser_silver: { text: "$3,700", source: mockSource("Kaiser SBC", 1) },
      },
    },
    {
      key: "oop_max",
      label: "Out-of-pocket max",
      highlight: true,
      cells: {
        uc_ship: { text: "$4,000", source: mockSource("UC SHIP SBC", 1) },
        anthem_silver: { text: "$8,750", source: mockSource("Anthem SBC", 1) },
        blue_shield_silver: { text: "$8,700", source: mockSource("Blue Shield SBC", 1) },
        kaiser_silver: { text: "$7,800", source: mockSource("Kaiser SBC", 1) },
      },
    },
    {
      key: "specialist",
      label: "Specialist visit",
      highlight: true,
      cells: {
        uc_ship: { text: "$20 copay (UC Family)", source: mockSource("UC SHIP SBC", 3) },
        anthem_silver: { text: "$90 copay", source: mockSource("Anthem SBC", 2) },
        blue_shield_silver: { text: "$85 copay", source: mockSource("Blue Shield SBC", 2) },
        kaiser_silver: { text: "$85 copay, referral needed", source: mockSource("Kaiser SBC", 2) },
      },
    },
    {
      key: "brand_rx",
      label: "Brand-name drug (inhaler)",
      highlight: true,
      cells: {
        uc_ship: { text: "$35 / fill", source: mockSource("UC SHIP SBC", 6) },
        anthem_silver: { text: "$65 / fill after Rx deductible", source: mockSource("Anthem SBC", 3) },
        blue_shield_silver: { text: "$60 / fill", source: mockSource("Blue Shield SBC", 3) },
        kaiser_silver: { text: "$60 / fill", source: mockSource("Kaiser SBC", 3) },
      },
    },
    {
      key: "er",
      label: "Emergency room",
      highlight: true,
      cells: {
        uc_ship: { text: "$100 copay", source: mockSource("UC SHIP SBC", 4) },
        anthem_silver: { text: "$400 + 20% after deductible", source: mockSource("Anthem SBC", 2) },
        blue_shield_silver: { text: "$400 copay", source: mockSource("Blue Shield SBC", 2) },
        kaiser_silver: { text: "$400 copay", source: mockSource("Kaiser SBC", 2) },
      },
    },
    {
      key: "ucla_network",
      label: "UCLA Health in network",
      highlight: false,
      cells: {
        uc_ship: { text: "Yes (UC Family tier)" },
        anthem_silver: { text: "Yes" },
        blue_shield_silver: { text: "Partial" },
        kaiser_silver: { text: "No" },
      },
    },
  ],
}

function search(insurer: string): MockStep[] {
  const searchId = nextId("search")
  const step = (step_id: string, status: "running" | "done" | "failed", text: string, url?: string): MockStep => ({
    event: "search_step",
    data: { search_id: searchId, step_id, status, text, url: url ?? null },
  })
  return [
    ...searchIntro(insurer),
    step("find", "running", `Searching for ${insurer} individual plans in California…`),
    { pause: 900 },
    step("find", "done", `Found ${insurer}'s plan finder page`, "https://example.com/plans"),
    step("open", "running", "Opening the plan page…"),
    { pause: 900 },
    step("open", "done", "Opened the 2026 plan list (6 plans)", "https://example.com/plans/2026"),
    step("rank", "running", "Scoring links for a Summary of Benefits…"),
    { pause: 700 },
    step("rank", "done", "Picked “Silver PPO — Summary of Benefits and Coverage”"),
    step("extract", "running", "Reading premium, deductible and network…"),
    { pause: 1000 },
    step("extract", "done", "Extracted 5 key facts"),
    ...say(
      `**${insurer} doesn't look like a good fit for you.**\n\n` +
        "- Its cheapest Silver plan costs noticeably more per month than UC SHIP *(placeholder)*.\n" +
        "- UCLA Health isn't listed in its network, so your pulmonologist would be out of network.\n\n" +
        "These facts come from the insurer's website, so I used them to judge fit but not to compute costs.",
    ),
  ]
}

// Reply for a live search, written from the worker's facts by template (no LLM).
export function searchReply(summary: SearchSummary): MockStep[] {
  const lines = [`**${summary.message}**`]
  if (summary.facts.length) {
    lines.push("", ...summary.facts.map((f) => `- ${f.label}: ${f.value}`))
  }
  const source = summary.pages.at(-1)
  if (source) lines.push("", `Source: [${source.title || source.url}](${source.url})`)
  if (summary.status === "found" || summary.facts.length) {
    lines.push("", "These facts come from the insurer's website, so I use them to judge fit but not to compute costs.")
  } else {
    lines.push("", "You can check the insurer's site yourself, or ask about a plan that's in my database.")
  }
  return say(lines.join("\n"))
}

export function searchIntro(insurer: string): MockStep[] {
  return say(`${insurer} isn't in my database yet, so I'll look it up live.\n\n`)
}

const KNOWN = /uc ?ship|anthem|blue shield|kaiser/i
const RECOMMEND = /recommend|other|alternative|compare|better/i
const INSURER = /\b((?:oscar|cigna|aetna|unitedhealthcare|united healthcare|uhc|molina|health net|ambetter|centene|humana|wellcare|sharp|la care|l\.a\. care)(?: health(?:care)?| insurance)?)\b/i

// The insurer to search for, when the message names one that isn't in the database.
export function searchTarget(request: TurnRequest): string | null {
  if (request.input.type !== "message" || request.reply_target) return null
  const match = request.input.text.match(INSURER)
  if (!match || KNOWN.test(request.input.text)) return null
  return match[1].replace(/\b\w/g, (c) => c.toUpperCase())
}

export function scriptFor(request: TurnRequest): MockStep[] {
  const { input } = request
  if (input.type === "form_response" && input.request_id === "form_usage") {
    return [
      ...say("Thanks. Here's what each of those would cost you on UC SHIP. Every number comes from the calculator, with its source.\n"),
      ...estimates(input.answers),
      ...say(
        "\n**Overall, UC SHIP handles your year well.** Copays are flat for the visits you expect, " +
          "and the ER visit is the only one with real uncertainty. Want me to compare it with commercial plans?",
      ),
    ]
  }
  if (input.type === "form_response" && input.request_id === "form_prefs") {
    return [
      ...say("I compared 4 plans against your weights. The full comparison is in the Arena panel."),
      { event: "report", data: { report: mockReport } },
      { pause: 200 },
      ...say(
        "\n\n**Recommendation: stay on UC SHIP.** It's cheapest in all three scenarios and keeps your UCLA doctors. " +
          "Kaiser is cheaper than the other commercial plans, but it drops UCLA Health, which you said matters a lot.",
      ),
    ]
  }
  if (input.type === "confirm_response" && input.request_id === "confirm_er") {
    if (!input.confirmed) {
      return say(`Thanks — I'll redo it with that change: “${input.correction ?? ""}”. *(Mock: the real agent re-prices here.)*`)
    }
    return [
      ...say("Here's the breakdown.\n"),
      ER_ESTIMATE(),
      ...say("\nThe ER copay is fixed; the range comes from the X-ray coinsurance and an estimated ER rate."),
    ]
  }
  if (input.type === "form_dismiss") {
    return say("No problem. I'll work with what you've told me so far, and mark any guesses as assumptions.")
  }
  if (input.type !== "message") return say("Okay.")

  const text = input.text
  if (request.reply_target) {
    return say(
      `**${request.reply_target.label}:** this row shows each plan's rule as written in its policy, ` +
        "with the page number on each cell. *(Mock explanation — the real agent explains the specific values.)*",
    )
  }
  const insurer = searchTarget(request)
  if (insurer) return search(insurer)
  if (/how much|cost|price/i.test(text) && /\b(er|emergency|ankle|x-?ray)\b/i.test(text)) {
    return [
      ...say("I need to make a few assumptions from what you said. Please check them before I calculate."),
      erConfirm,
    ]
  }
  if (RECOMMEND.test(text)) {
    return [...say("Sure. A couple of questions first, so I can weigh the plans the way you would."), prefsForm]
  }
  if (/uc ?ship|my (plan|insurance)/i.test(text)) {
    return [
      ...say("Happy to review UC SHIP. To price it for *your* year, I need to know how you use care."),
      usageForm,
    ]
  }
  return say(
    "I can help with two things:\n\n" +
      "1. **Cost estimates** — what you'd pay for a visit or a year of care on your plan.\n" +
      "2. **Insurance Arena** — find the plan that fits your health and budget best.\n\n" +
      "Try: *“This is my UC SHIP, what do you think?”*",
  )
}

