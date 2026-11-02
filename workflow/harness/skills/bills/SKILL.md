---
name: bills
description: Check a medical bill the user photographed against that provider's own negotiated rates and the user's plan, and draft an appeal letter.
---

When an upload is a medical bill or an explanation of benefits (EOB), read it with `read_bill`: the photo, or the
PDF page that lists the charges (`page`, found with the documents tools). It returns the `bill_id` and every line as
read (provider, NPI, codes, amounts). The lines were read from the image by a model; if the user says a line was
read wrong, believe the user.

### Checking a bill (`audit_bill`)

1. **Which plan**: pass the plan the user is covered by. If you don't know it, ask (by name, from the plan list).
   A plan with several in-network tiers needs `network_tier`, as for `estimate_cost`.
2. `remaining_deductible`: only as the user stated it, otherwise `null`; `0` when they say it is met. If the result
   is `needs_information`, Python asks the user for it.
3. Python finds the benefit that pays each line, this provider's **own** negotiated rate for the code in the plan's
   network, and the user's share through the calculator. The result shows a bill review card; don't repeat every
   line in your reply.

Each line's `check`:

| `check` | Meaning |
| --- | --- |
| `ok` | The bill asks for no more than the user's share. |
| `billed_above_your_share` | The bill asks for more than the user owes under the plan at the negotiated rate; `possible_overcharge` is the difference. |
| `possible_duplicate` | Same code, date and amount as an earlier line. |
| `no_rate` | No negotiated rate for this provider and code in the network (they may be out of network): say "no data". |
| `no_data` / `no_benefit` | The code or benefit is not in the databases: say "no data". |
| `not_reviewed` | Emergency care or drugs: not reviewed by this product. |
| `unreadable` | The photo didn't show the code or amount. |

How to talk about the result:
- Lead with `totals.possible_overcharge` and the one or two biggest findings, then what to do next.
- A bill sent before the insurer processed the claim shows full prices; the user's real share comes after. Say that
  the check assumes the billed amounts are what the user is asked to pay, and suggest asking for an itemized bill
  and the insurer's explanation of benefits (EOB).
- A negotiated rate means the provider has a contract in that network; it does not prove this claim was in network.
- Never fill a "no data" line with another provider's price or a median.

### Appeal letter (`draft_appeal`)

After `audit_bill` finds something (`findings` not empty), offer the letter; call `draft_appeal` when the user wants
it, or right away if they asked for one. Python writes the letter from the audit, so every number in it is checked.
It appears as an editable card. Nothing is sent: the user copies it, fills in their name and member ID, and sends it
to the provider's billing office themselves.
