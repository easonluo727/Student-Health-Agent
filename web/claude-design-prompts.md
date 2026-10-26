# Claude Design 提示词

用途：Claude Design 已经按 `web/DESIGN.md` 生成了风格，下面三步让它在画布上做出组件和页面。分三次发，每步做完先检查再发下一步：出现渐变、发光、等宽字体、全大写小标题、侧边菜单，就当场让它改。

设计稿里的页面是**范例，不是固定模板**：上线后页面由 agent 根据用户需求现场搭建。设计稿用来定样式和组件，也用作 agent 提示词里的页面范例。设计稿里的数字都是占位的样例数据。

---

## 第 1 步：组件库

```
Using the design system we just created from DESIGN.md, build a component sheet on the canvas for our health-insurance agent. Desktop only, 1440 px wide. Show each component in all its states, labelled.

1. Chat: user message, agent message (no bubble), composer (attach button, send button; empty / typing / disabled), app header (product name + "New chat").
2. Agent step lines: running (pulsing dot), done (check), failed (warning dot + reason), and the folded row "What I did · 4 steps", collapsed and expanded.
3. Source chips: "SBC p.3", "Negotiated rate · Anthem", "Your answer", "anthem.com".
4. Question card: single choice, multiple choice, free-text input, a prefilled answer with "From what you said — please confirm", the "Not sure" option, and a Continue button.
5. Confirm card: inferred values, each editable, and a "Looks right" button.
6. Badges: In network, Best fit, Estimate range, Needs review, No data, Overcharged, Not covered, Out of network.
7. Buttons: primary, secondary, tertiary, circular icon; default / hover / disabled.
8. Data table: header, normal row, relevant row, overcharged row (struck-through amount), no-data row.
9. Uploaded-file thumbnail (PDF and bill photo), modal, toast.

Follow DESIGN.md strictly: one mint accent used only as a fill, no gradients, no glow, no monospace or all-caps labels. Never show $0 for an unknown amount; write "No data".
```

## 第 2 步：主要页面

```
Now compose full desktop screens (1440×900) from those components. Use realistic sample data for a 26-year-old UCLA PhD student in Los Angeles (zip 90024) with mild asthma. Mark the numbers as sample data.

Every step is the agent's choice: it may answer in one sentence, ask questions, look things up, search the web, or build a page in the right-hand canvas. Only steps it actually took are shown, and the canvas only opens when it builds a page.

1. Empty state: one sentence, the composer centred, three example prompts. Nothing else.
2. Chat only, no canvas: the user asks "What's my copay for a specialist visit on Anthem Gold 80?"; one done step line ("Looked up Anthem Gold 80 benefits"); the agent answers in two sentences with a source chip. This shows the agent can answer without building a page.
3. Chat + question card: the agent asks about conditions, visits per year and budget before comparing plans.
4. Chat + canvas, page being generated: chat narrowed to 440 px on the left with the step lines; on the right, a plan-comparison page half built (top sections done, lower sections as grey placeholder blocks).
5. Chat + canvas, finished plan-comparison page: a one-sentence answer, 3 plans as rows with yearly totals, the recommended plan marked "Best fit", a line chart of yearly cost against specialist visits per year (0–10), a scenario table, excluded plans with reasons.
6. Chat + canvas, doctor page: the user asked for an asthma specialist nearby; a small map with pins and 4 doctors as rows (name, specialty, distance, this doctor's price for the user, one-line reason). One doctor has "No public data" for quality.
```

## 第 3 步：账单审核和搜索过程

```
Two more desktop screens with the same components.

1. Bill review: the user uploaded a photo of a medical bill (thumbnail in their message). The canvas shows the bill corrected: header (provider, date, plan); 4 lines with code, description, billed amount, negotiated rate and what the user should owe; one line overcharged (billed amount struck through, "Should be $X under your plan"); one line "No negotiated rate on file". Footer: "You may have been overcharged $Y" and a "Draft appeal letter" button, with the fine print "Negotiated rates are published by your insurer; the final claim may differ."
2. Step detail for a web search: the user clicked a step line "Searched the web for Oscar Bronze plan documents". The canvas shows what was searched, 3 sources as rows (favicon, title, URL) each with the short excerpt the agent read, and the sentences of the answer each source supports highlighted in pale mint, with a close button to return to the page. No screenshots.
```

---

## 做完以后

- 把设计稿导出给前端，按设计稿和 `web/DESIGN.md` 改 `web/` 里的代码。
- 把第 2、3 步的页面整理成 agent 提示词里的"页面范例"。
