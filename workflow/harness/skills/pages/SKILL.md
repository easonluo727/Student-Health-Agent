---
name: pages
description: Open a page on the canvas beside the chat for a tool result the user should see rather than read: doctors on a map, plans side by side, a cost breakdown, a checked bill.
---

Some tool results come back with a `result_id` (`doctors_1`, `compare_2`, `estimate_3`, `bill_4`). Those can be
shown as a page on the canvas with `show_page`. Python builds the page from the stored result: its layout and every
number. You choose whether to show it, which items to recommend, and one sentence on each.

**When to show a page** (your choice; a page is for what text can't show well):
- `doctors_*`: always, when doctors were found. People need to see where they are.
- `compare_*`: always. Several plans side by side are hard to read as text.
- `bill_*`: always. The user checks the bill line by line.
- `estimate_*`: when the breakdown matters (several parts, a deductible, a doctor's own rate). A single copay
  ("$30 for a specialist visit") is better as one sentence; don't open a page for it.
- Nothing to show (a greeting, a definition, a question back to the user): no page.

**How:**
1. Call the data tool first, then `show_page` with its `result_id`. Show the result you just got, not an older one.
2. For a doctor page, `highlight` 1–5 doctors you recommend, best first, by `npi` from that result; `notes` gives
   each one short reason (distance, what the user pays, specialty), at most one sentence. Only amounts from the
   tool result. For other pages, leave `highlight` and `notes` empty.
3. `title`: short and specific ("Pulmonologists near 90024"), or null for the default.
4. Then reply in chat with the conclusion in one or two sentences and say the details are on the page. Don't
   repeat the page's list or table in the chat.

Never list doctors (names, addresses, phones, map links) in the chat; they belong on a doctor page. When the
user asks about doctors you already found, call `show_page` with that `result_id` again (for example with new
picks), or search again if they want something different (another specialty, a free preventive visit).

Showing the same `result_id` again replaces that page (for example with different picks). A new search gets a new
`result_id` and a new page; the user can switch between pages.
