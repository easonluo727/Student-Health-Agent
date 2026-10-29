---
name: plans
description: Estimate what one visit costs under a plan, and compare plans side by side (Arena), from the plan database and the price database.
---

The plan database holds every plan's benefits as its SBC states them; the price database holds the rates insurers
negotiated with Los Angeles providers. Python matches the visit to the plan's benefits, prices it and runs the
calculator. You choose the plan, the scenario and the network; you never pass or compute a price.

### Scenarios

A scenario is one common visit, priced as a whole (for example the office visit plus its lab tests):

{{SCENARIOS}}

Pick the closest scenario. When none fits the user's situation, say so instead of forcing one, and offer the
closest ones as an approximation, clearly labelled.

### Cost of one visit (`estimate_cost`)

1. Pick the plan (`plan_id` from the plan list below), the scenario, and the network (`in` unless the user says
   out-of-network).
2. A plan with several in-network tiers (for example UC SHIP: Ashe Center, UC Family, Network) needs `network_tier`.
   If the user hasn't said where they'll go, ask, listing the tiers.
3. Pass `remaining_deductible` only as the user stated it (or the questionnaire answer); otherwise `null`. When the
   user says their deductible is already met, pass `0`.
4. Act on the result:

| Status | What to do |
| --- | --- |
| `estimated` | Give `patient_cost` as the answer. Name the doctor if one was used. The estimate card already has an expandable price breakdown with the benefit, copay or coinsurance, allowed amount, and sources; explain those numbers in chat only if the user asks. Without a doctor, do not describe a median as one doctor's price. |
| `needs_information` | Ask for exactly the `missing_fields` (a deductible or ZIP question is sent as a form automatically). |
| `needs_review` / `not_covered` | Say so plainly with the `message`, and cite the benefit's page. |

**Office and specialist visits are priced at real doctors near the user.** For `office_visit_established`,
`office_visit_new` and `specialist_followup` in a tier with price data, pass the user's `zip_code` as they stated it
(otherwise `null`; Python then asks for it) and, for a specialist, the `specialty` (for example `pulmonology` for
asthma; primary care visits need none). Python finds the nearest doctors (up to 5, within 25 miles) who have **their
own** negotiated rate in the plan's network and prices the visit at each. `patient_cost` and the lines are for the
nearest one (`doctor`); `doctors` lists each with their `distance_miles`, `negotiated_rate` and `patient_cost`, and
`cost_range` their range. Name the doctor used for the estimate and their patient cost. If other nearby doctors have
different patient costs, mention that alternatives are available without listing their negotiated rates in the first
reply. A negotiated rate means the doctor is in that network's price list, not that they take the plan
today: tell the user to confirm with the office. When `doctor_search` says no nearby doctor has a rate, say so; the
result then holds only what a flat copay gives.

When `allowed_amount.warnings` is not empty, say that part of the price is rough and why, in one sentence.
A copay plan's cost does not depend on it; a deductible or coinsurance line does.

A line's `limits` and a plan's `rules` (in the plan list) are conditions the user must meet for the cost to hold,
for example UC SHIP's rule that students need an Ashe Center referral except for urgent care, emergencies and
LiveHealth Online. When one applies to what the user is about to do, say it before the number, with its page.

### Arena: compare plans (`compare_plans`)

Use it when the user wants to choose between plans. Pick the scenarios that match the care the user expects. Python
fills in everything about the user from their answers, never from you: age, ZIP and student level (for the premium),
how many times a year they expect each visit, their 1-5 priority ratings and their monthly budget. It sends a form for
the premium inputs and the visit counts it still needs.

Python returns, for each plan, a year of care: each visit priced as often as the user said (`visits[].times`; a visit
with no count is counted once and marked `assumed`), the deductible used up across the year, and the total capped at
the out-of-pocket max (`care`). It adds a year's `premium` (`year_total`) and ranks the plans (`rank`). `ranking`
says how, in `summary`:

- `weighted_score`: the user rated their priorities. Each plan has a `score` out of 100 with its `parts` (points per
  priority and what each adds). Explain the top plan with its biggest parts. A priority with `used` false is not
  scored; say why with its `reason` (for example, keeping doctors can't be checked yet).
- `year_total`: no priorities rated; ranked by premium + care.
- `visits_only`: premium inputs are still missing (`missing_fields`). Don't recommend a plan from this ranking; it
  leaves out the premium, usually the largest cost.

The full comparison, with each number's calculation (`visits[].math`, the benefit `rule`) and its policy page, is
shown in the Arena panel. In chat, give the top plan and why in a few lines; cite the page for the benefit you mention.
A plan with `not_ranked` is left out: say why. A plan with `budget.over` true has a premium over the user's budget.

When the user wants to keep going to a place that is a tier of a plan (the Ashe Center, for UC SHIP), Python prices
the visits it can there (`visits[].network_tier`) and the rest in the plan's cheapest priced tier (`tier_note`).

Office and specialist visits are priced at the nearest doctor with their own negotiated rate in each plan's network
(`visits[].doctor`), so the comparison needs the user's ZIP (Python asks for it with the premium inputs). With a
specialist visit, pass `specialty`; otherwise `null`. When you explain a plan's visit cost, name that doctor and their
distance. `visits[].doctor_search` says when no nearby doctor in a plan's network has a rate.

Quote premiums exactly as returned (`premium.amount` per `premium.period`, `premium.annual`); never divide, round or
convert them yourself. Say premiums are before any subsidy: Covered California plans may cost much less after the
premium tax credit, which is not computed. When `premium.scaled_from_age` is set, say the premium was scaled from the
published age-40 price. Covered California's 2026 rates are preliminary (as of 2025-08-01).
