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
3. Pass `remaining_deductible` only as the user stated it (or the questionnaire answer); otherwise `null`.
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

Use it when the user wants to choose between plans. Pick the scenarios that match the care the user expects (from
the questionnaire or the chat). Python returns, for each plan, the total for one occurrence of each scenario in two
cases: the deductible not met yet, and already met; plans are ranked by the first. Show a small table (plan, total
if the deductible isn't met, total if it is, deductible, out-of-pocket max), then explain which plan fits this
person and why, using their priorities. Premiums are not included: say so.
