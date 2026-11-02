# 前后端协议（v1，2026-10-16）

前端（`web/`，Next.js）和 agent 服务（Python FastAPI）之间只有一个接口。前端的 TypeScript 类型在 `web/src/agent/protocol.ts`，改协议时两边一起改。

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

`context` 也可以不传。用户在首页选了功能后，前端在之后每条 `message` 上都带上它：`{ "mode": "calculate" | "review", "plan_ids": ["uc_ship_ucla_2026"] }`。
- `calculate` 且只选了一个计划：后端把这段对话切到该计划（和上传 PDF 匹配成功时一样）。
- `review`：`plan_ids` 是用户勾选要对比的计划。
- 后端只在 `context` 变化时给模型加一行说明（user 角色），所以重复发送是安全的。

### 计划列表

`GET {API_BASE}/plans` 返回计划：`[{ "plan_id", "name", "insurer", "plan_year", "verified", "uploaded" }]`。来源是计划数据库 `data/plans/policy.sqlite`，所有计划同时可用，`review` 就是 Arena（`compare_plans`）。前端用它渲染 Calculate 和 Review 的计划选择，并把上传且未经核对的计划标出来。

`input` 有四种：

| type | 字段 | 含义 |
| --- | --- | --- |
| `message` | `text`, `attachments?` | 用户在输入框里打的字；`attachments` 是已上传的 PDF：`[{ "file_id", "name" }]` |
| `form_response` | `request_id`, `answers` | 提交问卷卡片 |
| `form_dismiss` | `request_id` | 用户关掉问卷卡片，不回答 |
| `confirm_response` | `request_id`, `confirmed`, `correction?` | 确认卡片：确认，或者说明哪里不对（后端目前不发确认卡片，见下） |

`answers` 按字段名交回，每个答案都是 `{ "value": ..., "unknown": false }`。用户选“I don't know”时是 `{ "value": null, "unknown": true }`。`value` 的类型取决于题型：单选是 string，多选是 string[]，数字和权重是 number，填空是 string。

**后端必须把这些答案记为 `source: "user_form"`，数字直接交给 Python，不经过模型转述。**

### 上传 PDF 或照片

`POST {API_BASE}/upload`，`multipart/form-data`，字段 `session_id` 和 `file`（PDF，或 JPEG、PNG、WebP、HEIC 照片，最大 25 MB；后端按文件开头的字节判断类型）。返回 `{ "file_id", "name", "size", "content_type" }`。前端把 `file_id` 放进下一条 `message` 的 `attachments`。

后端收到带附件的消息后不再自动读取（2026-11-01 起）：agent 自己用文档工具查看文件（看目录、搜索、读页、看图），是保单就先在 `data/plans/policy.sqlite` 里匹配，匹配不上且用户要算钱时用 `extract_plan` 抽取它选的页，生成临时 `plans.csv`、`benefits.csv`，通过 `build_seed.py` 的结构校验后写入共享 policy SQLite；是账单就用 `read_bill`。抽取的 plan 会立刻出现在 `/plans` 和比较 UI 中，但保持 `verified=0` 并标明“自动抽取、未经人工核对”。没有确认的 MRF 网络映射时，只能可靠计算不依赖 negotiated rate 的固定 copay。agent 的每个工具步骤（打开、搜索、读页、看图、抽取的每一页）都用 `search_step` 事件发送，带 `kind: "document"`；运行中的步骤 `status: "running"`，同一个 `step_id` 结束时更新为 `done` 或 `failed`。

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
| `email_draft` | `EmailDraft` | 邮件卡片（申诉信）：用户可以改收件人、主题和正文，点 "Send from Gmail" 才发送（后端待做，见下） |
| `email_status` | `{ request_id, status, message?, sent_at? }` | 邮件发送状态：`sending`、`sent`、`failed` |
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
- `heading`（可选）：一组题共用的题干，前端只在这一组的第一题上方显示一次。目前用于优先级打分题："What matters most to you?"。
- `prefill`：用户之前说过的信息。前端预先填好，并标明“From your message, please confirm”。
- **每道题前端都会自动加一个“I don't know”选项**，后端不用自己加。

### Confirm（确认卡片）

后端目前不发这种卡片：它只用在旧的 `calculate_estimate` 工具上，2026-10-20 已随 legacy 模式删除。前端仍保留支持。

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
      "description": "2 specialist visits, 1 urgent care visit",
      "costs": { "uc_ship": { "low": 0, "mid": 0, "high": 0 } }
    }
  ],
  "comparison": [
    {
      "key": "visit.office_visit_established",
      "label": "10 x Primary care visit (established patient)",
      "highlight": true,
      "cells": { "uc_ship": { "text": "$250.00", "detail": "10 x $25.00 = $250.00. Primary care visit: $25.00 copay, deductible doesn't apply", "source": { ... } } }
    }
  ],
  "ranking": {
    "method": "weighted_score",
    "summary": "Score out of 100 = the weighted average of each priority's points. ...",
    "factors": [
      { "factor": "premium", "label": "A low premium", "answer": 4, "weight": 3, "used": true, "reason": null }
    ]
  },
  "note": "Care = each visit priced in turn over the year ..."
}
```

- 后端在每次 `compare_plans` 算完（不缺输入）后发送，由 `workflow/arena_report.py` 从 `plan_db.compare` 的结果生成，没有 LLM 参与。
### Care（dashboard 的"附近的医生和机构"部分，接口已留好，后端待接）

`report.care` 可以不传。有它时 dashboard 显示"Care near you"标签页：顶部是查询栏，下面是筛选、价格分布、地点列表和地图。没有计划对比时 `plans`、`scenarios`、`comparison` 传空数组即可。对应路线图第 1 步（按位置推荐具体医生），所有数字由 Python 算。

```json
{
  "query": {
    "service": "Knee MRI", "code": "73721", "description": "...",
    "location": { "label": "90024 · Westwood", "zip": "90024", "lat": 34.0635, "lng": -118.4455 },
    "plan_id": "anthem_ppo_ucship_ucla_2026_27", "plan_label": "UC SHIP · Network tier", "max_miles": 25
  },
  "price": {
    "label": "Typical negotiated price in your network", "midpoint": 890, "min": 410, "max": 2350,
    "bins": [{ "price": 410, "count": 2 }, { "price": 520, "count": 5 }],
    "source": { "kind": "mrf", "label": "..." }
  },
  "providers": [
    {
      "provider_id": "1234567890", "name": "...", "kind": "imaging_center", "specialty": null,
      "address": "...", "lat": 34.05, "lng": -118.44, "distance_miles": 0.6,
      "in_network": true, "network_tier": "Network",
      "allowed_amount": 620, "patient_cost": { "mid": 124 }, "price_vs_typical": "lower",
      "quality": { "score": 4.6, "label": "4.6 · CMS star rating", "source": { ... } },
      "recommended": true, "reason": "为什么推荐（agent 写）", "source": { ... }
    }
  ],
  "note": "What you pay assumes your deductible is met"
}
```

- `provider_id` 用 NPI。`kind`：`doctor`、`hospital`、`clinic`、`imaging_center`、`lab`、`urgent_care`、`other`。
- `in_network: null` 表示不知道；`patient_cost: null` 表示算不出，前端显示"Price unknown"，地图标签显示"—"（不拿协商价充当用户要付的钱）。
- `quality.score` 是 0–5，没有公开评分时为 `null`，`label` 写"No public rating"。
- `recommended: true` 的地点显示"Agent pick"徽章，地图上高亮。前端默认按后端给的顺序显示，用户可以切换排序（价格、距离、评分）和筛选（距离、评分、只看网络内），这些只改变显示，不改变数字。
- 查询栏只读：用户点某一项，输入框上方出现"Asking about: …"，下一条消息带 `reply_target`，`ref` 是 `care.query.service`、`care.query.location`、`care.query.plan`；点地点卡片上的"Ask"时 `ref` 是 `care.provider.<provider_id>`。

- `scenarios[].costs` 是**年度总花费**，由 Python 计算：`expected` = 年保费 + 按用户填的次数逐次计价的看病花费（deductible 全年累计，受 OOP max 封顶）；`healthy` = 只有年保费；`worst` = 年保费 + OOP max。缺保费输入时只有一个 `expected`，只含看病花费。
- `plans[].annual_premium` 可以是 `null`（保费还算不出来）。
- `comparison` 的每格可以带 `detail`：这个数是怎么算出来的（次数 × 单次费用、用了哪条福利规则、allowed amount 来源）。`source` 指向保单页码和原文。`highlight: true` 的行是用户自己填了次数的就诊。
- `ranking`：排名方式，由 `plan_db.rank_plans` 计算（2026-10-24 起取代"不使用综合评分"）。用户给优先级打了分时 `method` = `weighted_score`：每个优先级上最好的计划 100 分、最差的 0 分，按金额线性插值；权重 = 打分 − 1（1 = 不在乎 = 0），没打分的按 3 算；总分是加权平均。`year_total`：没打分，按年保费 + 看病花费排。`visits_only`：缺保费输入，只按看病花费排，不作推荐。超出月预算的计划排在预算内的计划之后。`factors[].used` 为 false 的优先级不计分，`reason` 说明原因（例如"保留医生"暂时无法核对网络）。

## 前端的固定行为

- 问卷卡片：提交时卡片进入“提交中”状态，按钮全部禁用；提交失败时错误显示在卡片上，可以重试；提交后卡片折叠成一行摘要。
- 确认卡片：“Looks right” 或 “Something's off”（附一句说明）。只在 agent 有猜测时出现，见上文。
- 首页：干净的对话页，输入框上方是模式切换（Calculate / Review / Just chat，Just chat 不带 `context`），下面是几条建议问题。
- dashboard：收到 `report` 后页面分成两栏，左边是 agent 对话栏（420px），右边是 dashboard（只做桌面 web 端），可以关掉、再用"Open report"打开。点击对比表的某一行或 care 查询栏的某一项，输入框上方会出现“Asking about: …”，下一条消息带上 `reply_target`。
- 搜索进度：在对话里显示为步骤列表，正在进行的步骤有加载动画。有截图时，右下角出现浮动小窗口，显示最新一页和当前步骤；搜索结束后自动缩成一行。点放大按钮打开完整的 “Computer” 视图：搜索进行中跟随最新一步；结束后显示所有截图的总览；点击某一张可停下来查看，点 “Back to live” / “Overview” 回到跟随。
- 地图：Leaflet。默认底图是 Esri 浅灰底图加地名层，不需要 key；可以用 `NEXT_PUBLIC_MAP_TILES`、`NEXT_PUBLIC_MAP_LABELS`、`NEXT_PUBLIC_MAP_ATTRIBUTION` 换成别的。

## Google 登录（2026-11-01，可选）

用户可以用 Google 账号登录，不登录也能用全部功能。前端用 Google 自己的按钮（Google Identity Services），拿到 ID token 后交给后端；后端校验后设一个 httpOnly cookie（`th_login`，30 天）。所有请求都带 `credentials: "include"`。

- `GET {API_BASE}/auth/me` → `{ "user": { "user_id", "email", "name", "picture" } | null, "google_client_id": string | null }`。`google_client_id` 为 `null` 表示服务器没配 `GOOGLE_CLIENT_ID`，前端不显示登录按钮（mock 也是这样）。
- `POST {API_BASE}/auth/google`，body `{ "credential": "<Google ID token>" }` → `{ "user" }`，同时设 cookie。token 无效、过期、不是发给本 client 的，或邮箱没验证，返回 401；没配 client id 返回 503。
- `POST {API_BASE}/auth/logout` → `{ "user": null }`，删除 cookie 和这条登录记录。

### 对话归属（2026-11-01）

- 登录用户发的 `/turn` 或 `/upload` 会把这段对话归到他的账号下（匿名时开始的对话也一样，登录后的下一轮就归他）。之后只有他能用：别人或者没登录的人用这个 `session_id` 发 `/turn`、`/upload`，返回 404。所以前端在退出登录时开一段新对话。
- `session_id` 只能是字母、数字和 `-`，8 到 64 个字符（前端的 uuid），否则返回 422。
- 匿名对话最后一轮之后保留一天；登录用户的对话一直保留，直到他自己删除。
- 后端把每一轮用户的输入和发出的事件存成聊天记录：`{ "event": "input", "data": <请求里的 input，有 reply_target 时也放在里面> }`，后面跟这一轮的 SSE 事件（同一条消息的 `message_delta` 合并成一条）。前端按顺序把服务器事件交给 reducer，就能把对话重新显示出来；`input` 事件用来画用户那一侧。

### 我的数据（要登录，没登录返回 401；不是自己的一律 404）

- `GET {API_BASE}/me/sessions` → `{ "sessions": [{ "session_id", "title", "created_at", "updated_at", "uploads" }] }`，最新的在前。`title` 是第一条消息（或第一个附件的文件名），时间是 Unix 秒。
- `GET {API_BASE}/me/sessions/{session_id}` → `{ "session_id", "events": [{ "event", "data" }], "uploads": [Upload] }`。
- `DELETE {API_BASE}/me/sessions/{session_id}` → `{ "deleted": session_id }`，连同聊天记录和文件。
- `GET {API_BASE}/me/uploads` → `{ "uploads": [Upload] }`，`Upload` 是 `{ "file_id", "session_id", "name", "content_type", "size", "sha256", "created_at", "kind", "plan_id", "plan_name", "bill_reviews" }`。`kind` 是 agent 读过之后 Python 记下的：`"plan"`（`find_plan_match` 或 `extract_plan` 认出的计划，带 `plan_id`）、`"bill"`（`read_bill` 读过），没读过是 `null`。`bill_reviews` 是这张账单最近一次 `audit_bill` 的摘要：`{ "bill_id", "status", "plan_id", "plan_name", "provider", "statement_date", "billed", "you_owe", "possible_overcharge", "findings", "appeal_drafted", "updated_at" }`，数字都来自 Python 的核对。
- `GET {API_BASE}/me/uploads/{file_id}` → 文件本身。
- `GET {API_BASE}/me/settings` → `{ "user", "settings": { "zip", "age", "plan_id" } }`，没设的是 `null`。
- `PUT {API_BASE}/me/settings`，body 是要改的字段（`name` 和上面三个，`null` 表示清空；`name` 为 `null` 时用回 Google 的名字）→ 同上。值不合法（ZIP 不是 5 位数、年龄不在 18–64、计划不存在）或字段不认识，返回 422，`detail` 是给用户看的说明，什么都不保存。
- 这些设置就是长期记忆：每一轮 Python 用 ZIP 和年龄填好问卷（不会再问），并把设置、过去的账单审核和上传过的计划写成一段 markdown 放进 prompt（`workflow/memory.py`，用户看不到这段）。登录用户在问卷里回答这两题时，答案也会存回设置，新的覆盖旧的。
- `DELETE {API_BASE}/me` → `{ "user": null, "deleted_sessions" }`：删除账号和它的全部对话、文件、登录记录，并删除 cookie。

## Gmail 申诉邮件（2026-10-26，前端和 mock 已做，后端待做）

agent 起草申诉邮件，从用户自己的 Gmail 发出。**用户不点 "Send from Gmail"，就永远不发。**

- `GET {API_BASE}/integrations` → `{ "gmail": { "connected", "email", "connect_url" } }`。没连 Gmail 时，卡片上的按钮是 "Connect Gmail"，在弹窗里打开 `connect_url`（Gmail OAuth），弹窗关闭后前端重新请求这个接口。后端现在固定返回未连接。
- `email_draft`：`{ request_id, title, from, to[], cc?, subject, body, attachments?, sources? }`。`from` 为 `null` 表示没连 Gmail。发出后这一轮结束（和问卷卡片一样）。
- 用户的回答是新的 `input`：`{ "type": "email_response", "request_id", "action": "send" | "cancel", "draft"?: { to, cc?, subject, body } }`。`draft` 是用户改过的版本，后端按它发送。
- 后端用每个用户自己的 OAuth token 调 Gmail API 发信，然后发 `email_status`。
- **现状（2026-11-01）**：后端会发 `email_draft`（`draft_appeal` 工具，信由 Python 按账单核对结果写），但还不能发信：`/integrations` 返回未连接且没有 `connect_url`，这时卡片显示 "Copy letter"，复制主题和正文。账单核对结果用 `estimate` 事件（标题 "Bill review"，每行是一项收费，`amount` 是用户应付）。

