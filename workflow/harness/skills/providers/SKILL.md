---
name: providers
description: Find specific doctors near the user and price each one under the user's plan(s), from the NPPES provider directory and the price database.
---

Use `find_providers` when the user wants a doctor, asks who is nearby, or asks what a visit costs at a specific
place. It is your choice: a question about a plan's copay alone does not need it.

1. **Where**: pass `zip_code` only as the user stated it (or their questionnaire answer). If you don't know it,
   ask with the `zip` question; never guess one.
2. **What**: pick the `scenario` closest to the visit, and a `specialty` when the visit needs one (for example
   `allergy_immunology` or `pulmonology` for asthma follow-ups, `family_medicine` or `internal_medicine` for a
   sore throat). Use `null` for any specialty. Call once per specialty when two fit.
3. **Plans**: pass the user's plan, or the plans they are comparing (up to 4). A plan with several in-network tiers
   needs its tier in `plan_tiers`, as for `estimate_cost`.
4. `remaining_deductible`: only as the user stated it, otherwise `null`; `0` when they say it is met.
5. `radius_miles`: what the user said, otherwise 10. `sort`: `nearest`, or `lowest_cost` when they care most
   about price.

Every price is **that doctor's own** negotiated rate in the plan's network. When a doctor has no rate
(`rate_status: rate_unavailable`), say "no data" for that doctor; never fill it with another doctor's price or a
median. The status of each price:

| `estimate_status` | Meaning |
| --- | --- |
| `estimated` | `patient_cost` is what the user pays at this doctor. |
| `needs_information` | Ask for `missing_fields` (usually the remaining deductible). |
| `partial_rate_only` | Only the negotiated rate is known, not the user's share; give the rate and say so. |
| `unavailable` / `rate_unavailable` | No price for this doctor. |
| `tier_required` | Ask which tier, then call again. |
| `needs_review` / `not_covered` / `benefit_unavailable` | Say so plainly. |

How to talk about the results:
- A rate means the doctor **has a negotiated rate in that network**; it does not prove they take the plan today.
  Tell the user to confirm with the office before booking.
- Distances are straight-line from the user's ZIP (`user_position.precision`), not driving distance.
- Recommend 1–5 doctors and say why (distance, price, specialty). Show more only when asked.
- `status: directory_unavailable` means the provider directory is not installed yet: say you can't look up
  doctors right now.
