# 前后端协议（v1，2026-10-16）

前端（`web/`，Next.js）和 agent 服务（Python FastAPI）之间只有一个接口。前端的 TypeScript 类型在 `web/src/lib/protocol.ts`，改协议时两边一起改。

交互模式参考 `docs/research/hark-interaction-patterns.md`：提问是有编号的结构化对象，答案按字段交回，dashboard 由后端整份推送。

## 接口

`POST {API_BASE}/turn`，请求体是 JSON，响应是 `text/event-stream`。每次用户操作发一次请求；这一轮的所有输出都在这个响应流里，以 `done` 事件结束。

- `API_BASE` 由前端环境变量 `NEXT_PUBLIC_API_BASE` 决定。没设置时用前端自带的 mock：`/api/mock`，按 Demo 流程回放事件。
- 后端需要开 CORS，允许前端的域名。

### 请求

```json
{
  "session_id": "前端生成的 uuid，整个对话不变",
  "input": { "type": "message", "text": "This is my UC SHIP, what do you think?" },
  "reply_target": { "kind": "dashboard_item", "ref": "comparison.deductible", "label": "Deductible" }
}
```

`reply_target` 可以不传。用户点了 dashboard 上的某一项再提问时才带上，agent 据此解释那一项。

`input` 有四种：

| type | 字段 | 含义 |
| --- | --- | --- |
| `message` | `text`, `attachments?` | 用户在输入框里打的字；`attachments` 是已上传的 PDF：`[{ "file_id", "name" }]` |
| `form_response` | `request_id`, `answers` | 提交问卷卡片 |
| `form_dismiss` | `request_id` | 用户关掉问卷卡片，不回答 |
| `confirm_response` | `request_id`, `confirmed`, `correction?` | 确认卡片：确认，或者说明哪里不对 |

`answers` 按字段名交回，每个答案都是 `{ "value": ..., "unknown": false }`。用户选“I don't know”时是 `{ "value": null, "unknown": true }`。`value` 的类型取决于题型：单选是 string，多选是 string[]，数字和权重是 number，填空是 string。

**后端必须把这些答案记为 `source: "user_form"`，数字直接交给 Python，不经过模型转述。**

### 上传 PDF

`POST {API_BASE}/upload`，`multipart/form-data`，字段 `session_id` 和 `file`（只收 PDF，最大 25 MB）。返回 `{ "file_id", "name", "size" }`。前端把 `file_id` 放进下一条 `message` 的 `attachments`。

后端收到带附件的消息后，先读 PDF 再交给模型：用 AMD 模型读第 1 页，只认出计划身份（计划名、保险公司、年份、网络、文件类型），再由 Python 在计划库 `policies/catalog.json` 里匹配。匹配上就把这段对话切到该计划，算钱用的是人工核对过的规则，不用上传文件里读到的数字；匹配不上就如实说明，不估算。读取进度用 `search_step` 事件发送，带 `kind: "document"`，截图是 PDF 页面。

### 响应：SSE 事件

每个事件是 `event: <type>` 加一行 `data: <json>`。

| event | data | 前端怎么显示 |
| --- | --- | --- |
| `message_start` | `{ message_id }` | 新建一条 assistant 消息 |
| `message_delta` | `{ message_id, text }` | 往这条消息后面追加文字（Markdown） |
| `message_end` | `{ message_id }` | 这条消息结束 |
| `form` | `Form` | 问卷卡片，等待用户填写 |
| `confirm` | `Confirm` | 确认卡片：计算之前展示 agent 理解的服务、网络、金额 |
| `estimate` | `Estimate` | 对话里的费用结果：一个数字 + 计算过程表格 |
| `search_step` | `{ search_id, step_id, status, text, url?, title?, screenshot?, kind? }` | 搜索进度：对话里显示一行文字（同一个 `step_id` 再次出现时更新这一行）；带 `screenshot` 时，右侧 “Computer” 面板显示这一步的网页截图 |
| `report` | `{ report: Report }` | 打开或更新 dashboard 面板（整份替换） |
| `error` | `{ message }` | 显示错误，用户可以重试 |
| `done` | `{}` | 这一轮结束，输入框恢复可用 |

`status` 是 `running`、`done` 或 `failed`。

一个问卷卡片或确认卡片发出后，这一轮就应该结束（`done`）。用户回答后，前端用对应的 `request_id` 发下一轮请求。

## 数据结构

### Form（问卷卡片）

```json
{
  "request_id": "form_usage_1",
  "title": "A few questions about how you use care",
  "intro": "I'll use these to price UC SHIP for your situation.",
  "questions": [
    {
      "field": "specialist_visits_per_year",
      "kind": "number",
      "label": "How many specialist visits do you expect this year?",
      "help": "For example, asthma follow-ups with a pulmonologist.",
      "unit": "visits",
      "min": 0,
      "max": 50,
      "prefill": { "value": 2, "source": "user_message" }
    }
  ]
}
```

- `kind`：`single`（单选）、`multi`（多选）、`number`（数字）、`text`（填空）、`weight`（权重）。
- `single` 和 `multi` 必须带 `options: [{ value, label }]`。选项由后端根据数据生成，例如网络层级来自计划 JSON。
- `weight` 是 1–5 的刻度，`scale_labels: ["Don't care", "Very important"]`。
- `prefill`：用户之前说过的信息。前端预先填好，并标明“From your message, please confirm”。
- **每道题前端都会自动加一个“I don't know”选项**，后端不用自己加。

### Confirm（确认卡片）

```json
{
  "request_id": "confirm_usage_1",
  "title": "Here's what I'll price",
  "items": [
    { "label": "Plan", "value": "UC SHIP 2026–27", "source": "user_message" },
    { "label": "Specialist visits", "value": "2 per year, in network", "source": "user_form" }
  ]
}
```

`source` 是 `user_message`（用户原话里有）、`inferred`（agent 推断的，前端显示为醒目的 “My guess”）、`user_form`、`policy`、`mrf` 之一，前端会显示出处标签。

**什么时候发确认卡片（2026-10-16 决定）：只在 agent 有猜测时发。**
- 计算要用到的值如果全部来自问卷（`user_form`），**不发**确认卡片，直接计算。提交问卷本身就是确认，问卷里预填的值也已经标了“请确认”。
- 只要有一个值是 agent 推断出来的（`inferred`），例如把“扭伤脚踝去急诊”理解成“急诊 + X 光、网络内、没有住院”，就**先发**确认卡片，用户确认后再计算。
- 后端的 `guards.py` `post_tool` 要按同样的规则判断：不再对每个 `estimated` 结果都要求确认，只对含有推断值的结果要求确认。

### Estimate（费用结果）

```json
{
  "estimate_id": "est_1",
  "title": "Urgent care visit (flu)",
  "plan_name": "UC SHIP",
  "status": "estimated",
  "patient_cost": { "low": 20, "mid": 30, "high": 30 },
  "rows": [
    { "label": "Allowed amount", "amount": 180, "note": "Negotiated rate, CPT 99213", "source": { ... } },
    { "label": "Copay", "amount": 30, "note": "Deductible waived", "source": { ... } }
  ],
  "assumptions": ["In-network urgent care", "Deductible not yet met"],
  "message": null
}
```

- `status` 和 `calculate_cost.py` 一致：`estimated`、`needs_information`、`needs_review`、`not_covered`、`invalid_input`。不是 `estimated` 时 `patient_cost` 为 `null`，前端显示 `message`。
- `patient_cost` 只有 `mid` 时显示一个数；有 `low` 和 `high` 时显示区间（价格是估算时）。

### Source（出处）

```json
{ "kind": "policy", "label": "UC SHIP SBC", "page": 3, "quote": "Specialist visit: $30 copay", "url": null }
```

`kind`：`policy`（保单页码）、`mrf`（价格数据库）、`web`（搜索）、`user_form`、`user_message`、`inferred`。前端把出处显示成可以点开的小标签。

### Report（dashboard）

**report 是 dashboard 的唯一数据来源。** 后端每次重新计算后推送整份 report，前端整份替换。

```json
{
  "report_id": "arena_1",
  "title": "Insurance Arena",
  "recommendation": {
    "plan_id": "uc_ship",
    "headline": "Stay on UC SHIP: lowest cost in every scenario.",
    "reasons": ["..."],
    "excluded": [{ "plan_id": "x", "reason": "No in-network pulmonologists near UCLA" }]
  },
  "plans": [
    { "plan_id": "uc_ship", "name": "UC SHIP", "insurer": "Anthem Blue Cross", "network": "PPO", "annual_premium": 0, "source": { ... } }
  ],
  "scenarios": [
    {
      "key": "expected",
      "label": "Your expected year",
      "description": "2 specialist visits, 1 urgent care, 1 ER visit with X-ray",
      "costs": { "uc_ship": { "low": 0, "mid": 0, "high": 0 } }
    }
  ],
  "comparison": [
    {
      "key": "deductible",
      "label": "Deductible",
      "highlight": false,
      "cells": { "uc_ship": { "text": "$0", "source": { ... } } }
    }
  ]
}
```

- `scenarios[].costs` 是**年度总花费**（年保费 + 预期自付，受 OOP max 封顶），由 Python 计算。场景至少有：`healthy`（健康年）、`expected`（用户的预期使用）、`worst`（意外大病）。
- `comparison` 中 `highlight: true` 的行和用户的病情相关，前端会高亮。
- 不使用综合评分。

## 搜索 worker

`search_worker/worker.py`（FastAPI + Playwright，不调用 LLM）。agent 服务调用它，把它的事件转发给前端。

- `POST /search`，请求体 `{ "insurer": "Oscar Health" }`，响应是 `text/event-stream`。
- `search_step`：`{ step_id, status, text, url, title, screenshot }`。`screenshot` 是 JPEG 的 data URL。agent 服务转发时补上 `search_id`。
- `search_summary`：`{ insurer, status, message, facts, pages }`，`status` 是 `found`、`partial`、`blocked`、`not_found`、`error` 之一。这个事件不发给前端，由 agent 服务据此写回复。
- 固定四步：Bing 搜索并找出官网 → 打开官网 → 给页面上的链接按关键词打分并选一个 → 打开它，用正则读出保费、自付额、自付上限。
- 网站拦截自动化访问时立即停止并如实说明，不伪装浏览器、不绕过。只会拒绝可选 cookie，不登录、不填表单。
- 前端 mock 在设置了环境变量 `SEARCH_WORKER_URL` 时调用真实 worker；没设置时回放写死的搜索剧本。

## 前端的固定行为

- 问卷卡片：提交时卡片进入“提交中”状态，按钮全部禁用；提交失败时错误显示在卡片上，可以重试；提交后卡片折叠成一行摘要。
- 确认卡片：“Looks right” 或 “Something's off”（附一句说明）。只在 agent 有猜测时出现，见上文。
- dashboard：在对话右侧（只做桌面 web 端）。点击对比表的某一行，输入框上方会出现“Asking about: …”，下一条消息带上 `reply_target`。
- 搜索进度：在对话里显示为步骤列表，正在进行的步骤有加载动画。有截图时，右侧面板自动切到 “Computer”：搜索进行中跟随最新一步；结束后显示所有截图的总览；点击某一张可停下来查看，点 “Back to live” / “Overview” 回到跟随。
