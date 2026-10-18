---
name: pricing
description: Look up the allowed amount for a service in the price database, then calculate what the user pays from it.
---

Use this skill when the user wants to know what care will cost and hasn't given you the allowed amount. The price database holds the rates insurers negotiated with Los Angeles providers, published in their price transparency files.

### Steps

1. Read the benefit rule with `get_policy_rule`.
2. Call `get_allowed_amount` with that `rule_id` and `option_id: null`.
3. Act on the result:

| Result | What to do |
| --- | --- |
| `success` and `usable: true` | Call `calculate_with_quote` with the `quote_id`. Python keeps the amount; never copy it into another tool. |
| `needs_clarification` | Python has sent the user a one-question form. Stop and wait. When the answer comes back, call `get_allowed_amount` again with the `option_id` the user chose. |
| anything else | Say plainly that the price database can't price this, using its `message`. Then you may ask for the allowed amount with the questionnaire (`allowed_amount`) and use `calculate_estimate`. |

### Explaining a price

- An aggregate rate is a typical rate across the network's providers, not a specific doctor's or hospital's price. Say so.
- The snapshot date is when the insurer published the price list, not the date of service.
- Name the insurer, the network and the snapshot date when you quote a price.
- For several visits, price and calculate each one separately. Don't multiply or add amounts yourself.
