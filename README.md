# Student Health Agent

**An AI insurance advisor for US students who have to buy their own health insurance, with every number traced to a policy page or a negotiated rate.**

Built for the AMD Developer Hackathon: ACT III (Health and Wellbeing track). Every LLM call runs on an open model (Qwen3.8-27B) served by the AMD API.

## The problem

At 26, a PhD student ages off their parents' plan and has to pick insurance alone. The plan documents run past 150 pages, and the one number that matters, *what will my visits actually cost?*, isn't in them: it depends on prices insurers negotiate privately with each hospital. Since 2022, US insurers must publish those prices in machine-readable files, but the files are hundreds of gigabytes of JSON that no student can read.

## What it does

You chat with an advisor that knows your plan and the real prices in Los Angeles.

1. **Upload your plan.** Attach the PDF. The AMD model reads the first page, recognizes the plan, and the conversation switches to that plan's checked benefit rules.
2. **Answer a short questionnaire.** Instead of asking one question at a time, the agent picks questions from a fixed question bank and sends them as a form: your conditions, how often you see a doctor, your budget, what matters to you.
3. **Get real numbers.** The agent looks up the negotiated rate for the visit in a database compiled from insurers' price files, applies your plan's copay, coinsurance and deductible, and explains the result. The rate, the rule and the PDF page are shown with every estimate.
4. **Ask about other insurers.** For an insurer that isn't in the database, a browser worker searches its official site live, with screenshots of each step.

## How it works

The core rule: **the model makes judgments, Python makes numbers.** The model decides what to ask, which rule applies and how to explain it. Python computes every dollar amount, and code-level checks reject any reply that contains a number Python didn't produce.

```text
 Next.js chat (web/)  ── SSE ──►  Agent service (workflow/server.py, FastAPI)
                                      │
                                      ▼
                      ┌──────── Agent harness (workflow/harness/) ────────┐
                      │ Main prompt   who the agent is, ground rules      │
                      │ Skills        questionnaire · pricing             │
                      │ Tools         policy rules · calculator ·         │
                      │               definitions · live search           │
                      │ Hooks         provenance and reply checks         │
                      └──────┬──────────────┬──────────────┬──────────────┘
                             │              │              │
                 AMD API (Qwen3.8-27B)  Policy rules    Price database
                  chat · PDF reading    (policies/,     (data/mrf/, compiled
                                         workflow/)      from insurer MRFs)
```

| Part | What it does | Where |
| --- | --- | --- |
| **Main prompt** | The agent's role, ground rules (numbers only from Python, facts only from the catalog), and how to work | [`workflow/harness/prompts/system.md`](workflow/harness/prompts/system.md) |
| **Questionnaire skill** | A 22-question bank; the tool schema enumerates the question IDs, so the model can't invent questions. Every question names the database query or calculator input it feeds | [`workflow/harness/skills/questionnaire/`](workflow/harness/skills/questionnaire/) |
| **Pricing skill** | Looks up the allowed amount for a benefit and calculates from it. Python stores the rate under a quote ID; the model never handles the amount | [`workflow/harness/skills/pricing/`](workflow/harness/skills/pricing/) |
| **Hooks** | Block amounts the user never stated, require reading a rule before using it, confirm guesses before calculating, and reject replies with untraced dollar amounts | [`workflow/harness/hooks.py`](workflow/harness/hooks.py), [`workflow/guards.py`](workflow/guards.py) |
| **Calculator** | Pure, deterministic cost sharing (copay, coinsurance, deductible). Refuses rules a person hasn't verified | [`workflow/calculate_cost.py`](workflow/calculate_cost.py) |

## Where AMD runs

Every model call goes to the AMD API through one client ([`workflow/amd_client.py`](workflow/amd_client.py)). There is no other provider and no fallback.

| Task | Model input | Where |
| --- | --- | --- |
| Chat agent: choosing questions, rules and tools; writing replies | Text and tool calls | [`workflow/server.py`](workflow/server.py) |
| Recognizing an uploaded plan | Image of page 1 | [`workflow/harness/reader.py`](workflow/harness/reader.py) |
| Extracting benefit tables from plan PDFs | 300-dpi page images | [`workflow/import_policy.py`](workflow/import_policy.py) |
| Matching plan benefits to billing codes | Text, then validated in Python | [`branch-a-mrf-integration/overlay/workflow/auto_map_policy.py`](branch-a-mrf-integration/overlay/workflow/auto_map_policy.py) |

## The data

**Price database (allowed amounts).** A streaming Python pipeline, with no LLM, read insurers' Transparency in Coverage files: 316 files, 70 GB compressed, 444 GB of JSON. It kept 629,842 negotiated rates for 41 billing codes across 68,333 Los Angeles providers in 7 networks (Anthem PPO Prudent Buyer, which UC SHIP uses; Anthem Pathway EPO and HMO; UnitedHealthcare Choice Plus; Cigna National PPO; Ambetter from Health Net; L.A. Care). They are grouped into 19 common student visits, such as urgent care for the flu or an ER visit for a sprained ankle. The summary table [`data/mrf/allowed_amounts.csv`](data/mrf/allowed_amounts.csv) is checked in; the full 3.1 GB SQLite database stays local. Details: [`scripts/mrf/README.md`](scripts/mrf/README.md).

**Policy rules.** One rule per service and network, extracted from official plan PDFs by the AMD model ([`workflow/import_policy.py`](workflow/import_policy.py)), then checked by a person against the PDF ([`scripts/policy/review_sheet.py`](scripts/policy/review_sheet.py)). The calculator refuses any rule that a person hasn't marked verified. Plans the agent recognizes are listed in [`policies/catalog.json`](policies/catalog.json).

**Matching rules to prices.** Which network a plan uses, and which billing codes a benefit covers, are mappings that a person approves before they can price anything ([`branch-a-mrf-integration/`](branch-a-mrf-integration/)).

## Run it

Requirements: Python 3.12+, Node 20+ with pnpm, and poppler (`brew install poppler`) for PDF rendering.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r search_worker/requirements.txt
.venv/bin/playwright install chromium

# AMD API settings (gitignored)
cat > .env <<'EOF'
AMD_BASE_URL=https://developer.amd.com.cn/radeon/api/v1
AMD_API_KEY=...
AMD_MODEL=Qwen3.8-27B
SEARCH_WORKER_URL=http://localhost:8100
EOF

# 1. Search worker (port 8100)
(cd search_worker && ../.venv/bin/uvicorn worker:app --port 8100) &
# 2. Agent service (port 8000)
(cd workflow && ../.venv/bin/uvicorn server:app --port 8000) &
# 3. Frontend (port 3000)
cd web && pnpm install && echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local && pnpm dev
```

Open http://localhost:3000. Without `NEXT_PUBLIC_API_BASE`, the frontend runs against a built-in mock backend instead.

To use another plan from the catalog, start the agent service with `AGENT_PLAN=<plan_id> AGENT_RULES=<rules file>`.

## Tests

```bash
.venv/bin/python -m unittest discover tests                    # calculator, guards, harness, MRF parser (63 tests)
.venv/bin/python branch-a-mrf-integration/overlay/run_tests.py  # rule-to-price matching (90 tests)
cd web && pnpm lint && pnpm build
```

No test calls the LLM or the network.

## Repository layout

| Path | Contents |
| --- | --- |
| [`web/`](web/) | Next.js frontend: chat, form cards, estimate cards, live search panel |
| [`workflow/`](workflow/) | Agent service, harness, calculator, guards, policy PDF import |
| [`search_worker/`](search_worker/) | Playwright worker that searches insurers' official sites (no LLM) |
| [`scripts/mrf/`](scripts/mrf/) | Price database pipeline (no LLM) |
| [`scripts/policy/`](scripts/policy/) | Human review and import of policy rules |
| [`branch-a-mrf-integration/`](branch-a-mrf-integration/) | Matching policy rules to prices, and the mappings people approved |
| [`data/`](data/) | Compiled price tables and insurer profiles |
| [`policies/`](policies/) | Plan catalog, policy definitions and rules |
| [`docs/`](docs/) | Product definition, technical plan, frontend–backend protocol |
| [`tests/`](tests/) | Unit tests |

## Limitations

- **Coverage is narrow.** Prices exist for 19 common visits in Los Angeles. Surgery, hospital stays and prescription drug prices aren't in the database.
- **A negotiated rate is an estimate.** Claim edits and bundling change what is finally paid. Prices are typical rates across a network, not a quote from a specific doctor.
- **Few plans are fully priced.** Each plan needs its benefit rules checked by a person and its network mapped to the price data. Until then, the agent explains the plan's terms but won't produce an official number.
- **No premium subsidies, out-of-pocket maximum, or balance billing** in the calculator yet.
- It is not medical advice and doesn't guarantee coverage.
