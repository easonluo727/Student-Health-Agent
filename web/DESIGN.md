---
version: alpha
name: health-insurance-agent
description: Design system for the health-insurance agent page — the only page in the product. A conversation on the left and, when the agent decides a page is needed, an agent-built canvas on the right, on a warm paper background; results are rounded white cards with a heavy near-black display number, a clear mint action accent and receipt-style breakdowns. Reads like a calm consumer-finance tool, not an AI tool. Spacing, type scale and component patterns adapted from VoltAgent/awesome-design-md (MIT); brand names, proprietary fonts and brand colours replaced.

colors:
  primary: "#8fdcc8"
  on-primary: "#12191a"
  primary-active: "#c3efe3"
  primary-neutral: "#b0e3d6"
  primary-pale: "#e1f4ee"
  ink: "#12191a"
  ink-deep: "#0d3b33"
  body: "#434a48"
  mute: "#808784"
  canvas: "#ffffff"
  canvas-soft: "#f1efe9"
  line: "#e3e1db"
  positive: "#1f8a4f"
  positive-deep: "#0f4d2b"
  warning: "#f2b929"
  warning-deep: "#a35f00"
  warning-content: "#4a3a14"
  warning-pale: "#fbf0dc"
  negative: "#c4342c"
  negative-deep: "#9c241e"
  negative-pale: "#fbe6e4"

typography:
  display-md:
    fontFamily: Inter Tight, Inter, system-ui, sans-serif
    fontSize: 40px
    fontWeight: 900
    lineHeight: 34px
  display-xs:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 24px
    fontWeight: 600
    lineHeight: 31.2px
    letterSpacing: -0.48px
  body-md:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 16px
    fontWeight: 400
    lineHeight: 24px
  body-md-strong:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 16px
    fontWeight: 600
    lineHeight: 24px
  body-sm:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 14px
    fontWeight: 400
    lineHeight: 20px
  body-sm-strong:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 14px
    fontWeight: 600
    lineHeight: 20px
  caption:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 12px
    fontWeight: 400
    lineHeight: 16px
  button-md:
    fontFamily: Inter, system-ui, sans-serif
    fontSize: 16px
    fontWeight: 600
    lineHeight: 24px

rounded:
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  pill: 9999px
  full: 9999px

spacing:
  xxs: 2px
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  2xl: 32px
  3xl: 48px

layout:
  chat-column: 720px
  header-height: 56px
  chat-column-with-canvas: 440px
  canvas-min: 720px

components:
  # ─── Page chrome ───
  app-header:
    backgroundColor: "{colors.canvas-soft}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm-strong}"
    height: "{layout.header-height}"
    padding: "{spacing.md} {spacing.xl}"
  composer:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    borderColor: "{colors.line}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md} {spacing.lg}"
    width: "{layout.chat-column}"
  composer-send:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.full}"
    padding: "{spacing.sm}"

  # ─── Conversation ───
  user-message:
    backgroundColor: "{colors.primary-pale}"
    textColor: "{colors.ink}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md} {spacing.lg}"
  agent-message:
    textColor: "{colors.ink}"
    typography: "{typography.body-md}"
  step-line:
    textColor: "{colors.body}"
    typography: "{typography.body-sm}"
    doneColor: "{colors.positive}"
    runningColor: "{colors.mute}"
    failedColor: "{colors.warning-deep}"
  source-chip:
    backgroundColor: "{colors.primary-pale}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.caption}"
    rounded: "{rounded.sm}"
    padding: "{spacing.xxs} {spacing.sm}"

  # ─── Cards ───
  chat-card:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    borderColor: "{colors.line}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xl}"
    padding: "{spacing.xl}"
  chat-card-answer:
    typography: "{typography.display-md}"
    textColor: "{colors.ink}"
  chat-card-total:
    typography: "{typography.display-xs}"
    textColor: "{colors.ink}"
  data-table:
    headerBackground: "{colors.canvas-soft}"
    headerTypography: "{typography.caption}"
    bodyTypography: "{typography.body-sm}"
    cellPadding: "{spacing.md} {spacing.lg}"
    rowBorder: "{colors.line}"
  row-overcharged:
    backgroundColor: "{colors.negative-pale}"
    textColor: "{colors.negative-deep}"
  row-no-data:
    backgroundColor: "{colors.warning-pale}"
    textColor: "{colors.warning-content}"
  row-relevant:
    backgroundColor: "{colors.primary-pale}"

  # ─── Controls ───
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button-md}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md} {spacing.xl}"
  button-secondary:
    backgroundColor: "{colors.canvas-soft}"
    textColor: "{colors.ink}"
    typography: "{typography.button-md}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md} {spacing.xl}"
  button-tertiary:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    borderColor: "{colors.ink}"
    typography: "{typography.button-md}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md} {spacing.xl}"
  button-icon-circular:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    rounded: "{rounded.full}"
    padding: "{spacing.sm}"
  text-input:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    borderColor: "{colors.ink}"
    typography: "{typography.body-md}"
    rounded: "{rounded.md}"
    padding: "{spacing.md} {spacing.lg}"
  form-option:
    backgroundColor: "{colors.canvas}"
    borderColor: "{colors.line}"
    selectedBackground: "{colors.primary-pale}"
    selectedBorder: "{colors.ink}"
    typography: "{typography.body-md}"
    rounded: "{rounded.md}"
    padding: "{spacing.md} {spacing.lg}"

  # ─── Status ───
  badge-positive:
    backgroundColor: "{colors.primary-pale}"
    textColor: "{colors.positive-deep}"
    typography: "{typography.body-sm-strong}"
    rounded: "{rounded.pill}"
    padding: "{spacing.xs} {spacing.md}"
  badge-warning:
    backgroundColor: "{colors.warning-pale}"
    textColor: "{colors.warning-content}"
    typography: "{typography.body-sm-strong}"
    rounded: "{rounded.pill}"
    padding: "{spacing.xs} {spacing.md}"
  badge-negative:
    backgroundColor: "{colors.negative-pale}"
    textColor: "{colors.negative-deep}"
    typography: "{typography.body-sm-strong}"
    rounded: "{rounded.pill}"
    padding: "{spacing.xs} {spacing.md}"

  # ─── Overlays ───
  modal-card:
    backgroundColor: "{colors.canvas}"
    rounded: "{rounded.xl}"
    padding: "{spacing.xl}"
    shadow: "0 8px 24px rgba(18,25,26,.12)"
  toast:
    backgroundColor: "{colors.canvas}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md} {spacing.lg}"
    typography: "{typography.body-sm}"
    shadow: "0 4px 12px rgba(18,25,26,.10)"
---


## Overview

### Approved UI update (2026-10-29)

These updates supersede the older palette and controls below. The product is **TallyHealth**. Use the supplied `public/tallyhealth-logo.png` wordmark in the header as a home button; it starts a fresh conversation and clears the draft, selected plans, and mode.

- Palette: background `#F8FAF7`, text `#263B35`, primary actions `#1F4D40` with off-white text, pale surfaces `#E3F4EC`, sage accents `#91B9A6`. The logo retains the supplied artwork's colors.
- Patient cost is the lead number. Technical billing lines, copays, sources, and detailed assumptions are inside a default-closed **More information** disclosure. Do not describe estimates as guaranteed final bills.
- Leave 40 px before the user's next question after an agent response.
- Review and Calculate use a compact plan-selection bar with a selected count. Opening it reveals a bounded, scrollable checkbox list; Done, Escape, or clicking outside closes it. Multiple selections remain selected when closed.
- The message field resizes vertically using its bottom-right handle, from 64 px up to the smaller of 320 px and 40% of viewport height.

The product has **one page: the agent chat**. There is no landing page, no sidebar menu, no fixed dashboard, no tabs and no feature menu. The user talks to the agent; the agent chooses what to do each turn — reply in a sentence, ask questions, call harness tools (database, calculator, web search), or **build a page for this user in a canvas on the right**, like Muse AI; every user's page is different. Every number on screen comes from Python, so the design always shows where a number came from.

The look pairs a clear mint `{colors.primary}` (`#8fdcc8`) action accent with a warm paper canvas `{colors.canvas-soft}` (`#f1efe9`) and a near-black ink `{colors.ink}` (`#12191a`). It reads like a calm consumer-finance tool: generous whitespace, large rounded white cards, and one heavy display number per card. The chat chrome stays plain so the cards carry the identity.

**Key Characteristics:**
- Chat alone: one column, `{layout.chat-column}` 720 px, centred. With a page open: chat `{layout.chat-column-with-canvas}` 440 px on the left, canvas fills the rest on the right.
- A single mint accent, used as a fill only. No second accent.
- Numbers lead: the answer to "how much" is the largest thing on a card, in `display-md` (Inter Tight 900), followed by a receipt-style breakdown.
- Sources are always visible as `source-chip`s next to the numbers they support.
- The agent's work is visible: tool calls appear as live `step-line`s, then fold into "What I did"; any step can be opened to see the process (what was searched, sources, excerpts).
- `{rounded.xl}` 24 px is the card and button radius.
- A full semantic palette (positive, warning, negative) for status only.

## Colors

### Accent
- **Mint** (`{colors.primary}` — `#8fdcc8`): The one action colour: send button, primary card button, selected-state accents. Always as a fill with `{colors.on-primary}` ink text. Too light for text or thin lines on white.
- **Mint Hover** (`{colors.primary-active}` — `#c3efe3`): Lighter mint for hover / active.
- **Mint Neutral** (`{colors.primary-neutral}` — `#b0e3d6`): Mid mint for pressed fills.
- **Mint Pale** (`{colors.primary-pale}` — `#e1f4ee`): User messages, source chips, selected options, highlighted table rows.

### Surface
- **Canvas** (`{colors.canvas}` — `#ffffff`): Card interiors, composer.
- **Canvas Soft** (`{colors.canvas-soft}` — `#f1efe9`): The page background and table headers.
- **Line** (`{colors.line}` — `#e3e1db`): Card borders, table rules, dividers.

### Text
- **Ink** (`{colors.ink}` — `#12191a`): Default text, headings and numbers.
- **Ink Deep** (`{colors.ink-deep}` — `#0d3b33`): Links (underlined) and text accents, source-chip text.
- **Body** (`{colors.body}` — `#434a48`): Secondary text.
- **Mute** (`{colors.mute}` — `#808784`): Captions, placeholders, fine print.

### Semantic
- **Positive** (`{colors.positive}` — `#1f8a4f`) / **Positive Deep** (`#0f4d2b`): cheaper, in network, matches the negotiated rate, step done.
- **Warning** (`{colors.warning}` — `#f2b929`) / **Warning Deep** (`#a35f00`) / **Warning Content** (`#4a3a14`) / **Warning Pale** (`#fbf0dc`): needs review, estimate range, thin price data, no data.
- **Negative** (`{colors.negative}` — `#c4342c`) / **Negative Deep** (`#9c241e`) / **Negative Pale** (`#fbe6e4`): overcharge, not covered, out of network.
- Never use colour alone for meaning: pair it with a word ("Overcharged", "No data").

## Typography

### Font Family
1. **Inter Tight** (SIL OFL) at weight 900 — only for `display-md`, the one answer on a card.
2. **Inter** (SIL OFL) — everything else. Loaded with `font-feature-settings: "calt"`.

Every money figure uses tabular numerals (`font-feature-settings: "tnum"`) so columns line up. Both fonts load through `next/font/google`.

### Hierarchy

| Token | Size | Weight | Line Height | Letter Spacing | Use |
|---|---|---|---|---|---|
| `{typography.display-md}` | 40px | 900 | 34px | 0 | The one answer on a card ("$47"). At most one per card. |
| `{typography.display-xs}` | 24px | 600 | 31.2px | -0.48px | Per-plan / per-doctor totals; the empty-state sentence. |
| `{typography.body-md}` | 16px | 400 | 24px | 0 | Agent prose, user messages, form options. |
| `{typography.body-md-strong}` | 16px | 600 | 24px | 0 | Card titles, numbers inside prose, "You pay" row. |
| `{typography.body-sm}` | 14px | 400 | 20px | 0 | Tables, breakdown rows, step lines. |
| `{typography.body-sm-strong}` | 14px | 600 | 20px | 0 | Amounts in breakdowns, badges, header. |
| `{typography.caption}` | 12px | 400 | 16px | 0 | Source chips, table headers, fine print. |
| `{typography.button-md}` | 16px | 600 | 24px | 0 | Button labels. |

### Principles
- **Weight 900 for the answer, weight 600 for emphasis, 400 for reading.**
- Table headers in `caption`, sentence case. No monospace and no all-caps letter-spaced eyebrows anywhere.

## Layout

### Spacing System
- **Base unit**: 4 px.
- **Tokens**: `{spacing.xxs}` 2 px · `{spacing.xs}` 4 px · `{spacing.sm}` 8 px · `{spacing.md}` 12 px · `{spacing.lg}` 16 px · `{spacing.xl}` 24 px · `{spacing.2xl}` 32 px · `{spacing.3xl}` 48 px.
- **Between turns**: `{spacing.xl}` 24 px.
- **Card interior**: `{spacing.xl}` 24 px.

### Page structure
- **Header**: `app-header`, 56 px, product name on the left; on the right "New chat" and the account control (Google's own "Sign in with Google" button, or the signed-in user's avatar and name with a sign-out icon button). Nothing else.
- **Conversation**: one column, `{layout.chat-column}` 720 px, centred on `{colors.canvas-soft}`. Question and confirm cards are full column width.
- **Canvas (agent-built page)**: opens on the right only when the agent builds a page, after its tool calls are done. Chat shrinks to `{layout.chat-column-with-canvas}` 440 px; the canvas takes the rest (at least `{layout.canvas-min}` 720 px), white `{colors.canvas}` with a 1 px `{colors.line}` left edge, its own scroll, and a slim top bar: page title, page switcher when there is more than one page, close button. Closing leaves an "Open page" `button-secondary` in the chat turn that made it.
- **Page generation**: the page appears section by section as the agent writes it; a section that is still being written shows a `{colors.canvas-soft}` placeholder block of the right height, never a spinner. When a page fails the number check and is rewritten, the canvas keeps the last good version until the new one is ready.
- **Composer**: pinned to the bottom, 720 px, `{rounded.xl}`, 1 px `{colors.line}` border, soft shadow `0 1px 2px rgba(18,25,26,.06)`. Attach button (plan PDF, bill photo) on the left as `button-icon-circular`; `composer-send` on the right.
- **Empty state (first visit / new chat)**: the composer centred vertically in the column, one `display-xs` sentence above it ("Ask what care will cost, which plan fits you, or drop in a bill."), and 3 example prompts as text buttons below. Nothing else.
- Desktop web only (≥ 1024 px). No mobile layout.

### Touch Targets
Buttons render ~48 px tall (12 vertical padding + 24 line); form options are 44 px.

### Images
No photography or illustration. The only images are insurer logos in plan rows, the small map on the doctor card, and thumbnails of uploaded documents.

## Elevation & Depth

| Level | Treatment | Use |
|---|---|---|
| Level 0 — Flat | No shadow, no border. | Agent messages, step lines. |
| Level 1 — Hairline | 1 px `{colors.line}` border on white. | Chat cards, form options, tables. |
| Level 1b — Ink hairline | 1 px `{colors.ink}` border. | `button-tertiary`, `text-input`, selected `form-option`. |
| Level 2 — Floating | Soft shadow. | Composer, `modal-card`, `toast` only. |

White cards on the paper canvas are the main depth cue.

## Shapes

| Token | Value | Use |
|---|---|---|
| `{rounded.sm}` | 8px | Source chips. |
| `{rounded.md}` | 12px | Text inputs, form options. |
| `{rounded.lg}` | 16px | Map and thumbnails inside cards. |
| `{rounded.xl}` | 24px | Cards, buttons, composer, user messages. |
| `{rounded.pill}` | 9999px | Status badges. |
| `{rounded.full}` | 9999px | Circular icon buttons, send button. |

## Components

### Messages
- **`user-message`**: right-aligned, `{colors.primary-pale}`, `{rounded.xl}`, max 80% of the column.
- **`agent-message`**: no bubble. Plain `body-md` in `{colors.ink}`, full column width. Short paragraphs; numbers in the text are `body-md-strong`.

### Agent step trace (what makes it agentic)
- While working, each tool call is one `step-line`: a 14 px status dot + text, for example "Finding Anthem family doctors near 90024 … 42 found". Running = `{colors.mute}` pulsing dot; done = `{colors.positive}` check; failed = `{colors.warning-deep}` dot with the reason.
- When the turn finishes, the lines fold into a single row "What I did · 4 steps" (`caption`, `{colors.mute}`), expandable. Expanded steps show their sources.
- Driven by the existing `search_step` events (`docs/frontend-protocol.md`).

### Step detail (opening a step)
- Clicking a `step-line` (or a step inside "What I did") opens its detail in the canvas, replacing the page until closed.
- **Web search**: the search, then each source the model saw as a row (favicon, title, URL in `caption`) with the excerpt it read, and the sentences of the answer each source supports, highlighted with `{colors.primary-pale}`. No screenshots.
- **Database / calculator**: what was looked up, the inputs, the result, and the policy quote or rate source it used.
- Built from the existing `search_step` events and `web/src/components/computer/`; search data comes from Meta's built-in `web_search` tool (sources and citations).

### Source chip
- `source-chip`. Text says where the number came from: "SBC p.3", "Negotiated rate · Anthem", "Your answer". Click opens the source (PDF page or explanation) in a `modal-card`.

### Agent-built pages
- The agent decides what goes on the page for this user. The cards below are **examples to compose from, not fixed templates**; the agent picks, orders and combines them, and may add a chart or table when the question calls for it (for example yearly cost against number of specialist visits).
- A page starts with a one-sentence answer in `body-md-strong`, then the sections. Use the same tokens, cards, badges, tables and source chips as everywhere else; no custom colours or fonts.
- Charts: `{colors.ink}` for the recommended or user's own option, `{colors.mute}` for the others, `{colors.line}` gridlines, values labelled directly rather than via a legend where possible.
- Every number on the page must come from that turn's tool data; Python rejects the page otherwise. Search-sourced facts carry a source chip linking to the page.

### Form card (questions)
- `chat-card`. Title in `body-md-strong`, one line why the agent is asking in `body-sm` `{colors.body}`.
- Options are full-width `form-option` rows; selected = `{colors.primary-pale}` fill and 1 px `{colors.ink}` border. Every question has a "Not sure" option, visually equal to the others. Free-text answers use `text-input`.
- Prefilled answers carry a `caption` note "From what you said — please confirm".
- One `button-primary` "Continue" bottom-right.

### Cost card (one visit)
- Top: label in `body-sm` `{colors.body}` ("Strep throat visit · Dr. Lee · Anthem Gold 80"), then the answer in `chat-card-answer` ("$47"), then one line of context ("Deductible already met").
- Below a 1 px `{colors.line}` rule: a receipt. Each row = item on the left, amount right-aligned in tabular `body-sm-strong`, `source-chip` under the item. Last row "You pay" in `body-md-strong`.
- Ranges show as "$40–$62" with a `badge-warning` "Estimate range". Missing data shows the words "No data", never $0.

### Plan comparison card (Arena)
- First line: the recommendation in one sentence, `body-md-strong`.
- Then 2–4 plans as rows (not columns): insurer logo, plan name, `chat-card-total` year total, a one-line reason, status badge. The recommended plan has a 2 px `{colors.ink}` left border and a `badge-positive` "Best fit".
- Below the rows, the scenario `data-table`; rows relevant to the user's conditions use `row-relevant`.
- Excluded plans listed at the bottom in `body-sm` `{colors.mute}` with the reason.
- No composite scores ("8.5/10"). Show money and risk.

### Doctor card
- 1–5 doctors as rows: name in `body-md-strong`, specialty and distance in `body-sm`, this doctor's price for the user in `chat-card-total` on the right, a one-line reason from the agent.
- Optional small map (200 px tall, greyscale tiles, `{colors.ink}` pins, `{rounded.lg}`) at the top of the card.
- Missing quality data says "No public data"; never blank, never implied bad.

### Bill review card (marked-up bill)
- Looks like the bill, corrected. Header: provider, date, plan.
- Each line: code + description, billed amount, negotiated rate, what the user should owe. An overcharged line: billed amount struck through, `row-overcharged`, note "Should be $X under your plan". A line with no data: `row-no-data`, "No negotiated rate on file".
- Footer: "You may have been overcharged $Y" in `chat-card-total` `{colors.negative-deep}` (or "No problems found" in `{colors.positive-deep}`), then `button-primary` "Draft appeal letter".
- Under the footer, `caption`: "Negotiated rates are published by your insurer; the final claim may differ."
- The drafted letter opens as a new `chat-card` with "Copy" (`button-secondary`) and "Download" (`button-tertiary`).

### Confirm card
- Only when the agent inferred something. Lists the inferred values, each editable, and one `button-primary` "Looks right".

### Uploaded document
- An uploaded plan PDF or bill shows in the user's turn as a thumbnail (`{rounded.lg}`, 1 px `{colors.line}`) with the file name in `caption`.

### Buttons
- **`button-primary`**: mint fill, ink label. At most one per card.
- **`button-secondary`**: paper fill, ink label.
- **`button-tertiary`**: white with 1 px ink border.
- **`button-icon-circular`**: white circle, ink icon (attach, close).

### Badges
- `badge-positive` "In network", "Best fit"; `badge-warning` "Estimate range", "Needs review", "No data"; `badge-negative` "Overcharged", "Not covered", "Out of network".

### Overlays
- `modal-card` for source details and long text (the appeal letter preview). `toast` for "Copied". Nothing else floats; the canvas is a panel, not an overlay.

## Motion
- Cards fade and rise 8 px over 200 ms when they arrive. Step lines appear instantly.
- The only looping animation is the running-step dot.
- No glow, no scanlines, no shimmer, no gradient sweeps, no typing-cursor theatre.

## Reuse
- Rebuild the existing `web/src/components/dashboard/` pieces (Arena, care map) as chat cards rather than starting over.

## Do's and Don'ts

### Do
- Lead every card with the answer, then show how it was computed, with a `source-chip` next to every number.
- Use `{rounded.xl}` 24 px for cards and buttons.
- Keep white cards on the paper canvas; surface contrast plus a hairline carries depth.
- Use the semantic palette for status, and never repurpose mint as a success colour.
- Say "No data" or "Needs review" plainly when Python could not compute something.
- Keep agent prose short; let the card carry the numbers.

### Don't
- Don't add a landing page, sidebar menu, fixed dashboard, tabs or feature menu. The only panel is the canvas, and only the agent opens it.
- Don't use the AI-template look: centred hero headline, monospace or all-caps eyebrows, gradients, glow, sparkle icons, robot avatars.
- Don't introduce a second accent colour.
- Don't use mint as text or thin lines on white; it is a fill colour.
- Don't render CTAs as sharp rectangles.
- Don't show $0 for an unknown amount, and don't show composite scores.
- Don't put more than one `display-md` answer on a card.
