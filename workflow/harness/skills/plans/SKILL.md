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
| `estimated` | Give `patient_cost`, then the lines (which benefit, the copay or coinsurance, the allowed amount). Say the allowed amount is the median negotiated rate across Los Angeles providers in that network, give its range (`low`–`high`) and its date. |
| `needs_information` | Ask for exactly the `missing_fields` (a deductible question is sent as a form automatically). |
| `needs_review` / `not_covered` | Say so plainly with the `message`, and cite the benefit's page. |

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

Quote premiums exactly as returned (`premium.amount` per `premium.period`, `premium.annual`); never divide, round or
convert them yourself. Say premiums are before any subsidy: Covered California plans may cost much less after the
premium tax credit, which is not computed. When `premium.scaled_from_age` is set, say the premium was scaled from the
published age-40 price. Covered California's 2026 rates are preliminary (as of 2025-08-01).
