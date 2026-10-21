# Health Insurance Agent

**An AI health-insurance advisor for anyone in the US, with every number traced to a policy page or a negotiated rate.**

The LLM provider is configuration: any OpenAI-compatible endpoint works ([`workflow/llm_client.py`](workflow/llm_client.py)).

## The problem

Whether you get insurance through work, the marketplace or school, or are picking a plan for the first time, the plan documents run past 150 pages, and the one number that matters, *what will my visits actually cost?*, isn't in them: it depends on prices insurers negotiate privately with each hospital. Since 2022, US insurers must publish those prices in machine-readable files, but the files are hundreds of gigabytes of JSON that no one can read.

## What it does

You chat with an advisor that knows your plan and the real prices in Los Angeles. Next on the roadmap: recommending specific in-network doctors near you, priced doctor by doctor, with public quality data (`docs/improvements.md`).

1. **Upload your plan.** Attach the PDF. The model reads the first page, and Python finds the plan in the plan database.
2. **Answer a short questionnaire.** Instead of asking one question at a time, the agent picks questions from a fixed question bank and sends them as a form: your conditions, how often you see a doctor, your budget, what matters to you.
3. **Get real numbers for a visit.** The agent splits the visit into the parts the plan pays separately, looks up the negotiated rate in a database compiled from insurers' price files, applies the plan's copay, coinsurance and deductible, and explains the result. The rate, the benefit and the PDF page are shown with every estimate.
4. **Compare plans (the Arena).** The same visits priced on several plans side by side, with the deductible not met and already met.

## How it works

The core rule: **the model makes judgments, Python makes numbers.** The model decides what to ask, which rule applies and how to explain it. Python computes every dollar amount, and code-level checks reject any reply that contains a number Python didn't produce.

```text
 Next.js chat (web/)  ── SSE ──►  Agent service (workflow/server.py, FastAPI)
                                      │
                                      ▼
                      ┌──────── Agent harness (workflow/harness/) ────────┐
                      │ Main prompt   who the agent is, ground rules      │
                      │ Skills        questionnaire · plans               │
                      │ Hooks         amount and reply checks             │
                      └──────┬──────────────┬──────────────┬──────────────┘
                             │              │              │
                  LLM endpoint          Plan database   Price summary
                  chat · PDF reading    (data/plans/    (data/mrf/, compiled
                                         policy.sqlite)  from insurer MRFs)
```

| Part | What it does | Where |
| --- | --- | --- |
| **Main prompt** | The agent's role, ground rules (numbers only from Python, facts only from the database), and how to work | [`workflow/harness/prompts/system.md`](workflow/harness/prompts/system.md) |
| **Questionnaire skill** | A 22-question bank; the tool schema enumerates the question IDs, so the model can't invent questions. Every question names the database query or calculator input it feeds | [`workflow/harness/skills/questionnaire/`](workflow/harness/skills/questionnaire/) |
| **Plans skill** | `estimate_cost` (one visit on one plan) and `compare_plans` (the Arena). The model picks only plan IDs, scenarios and the network; Python matches and prices | [`workflow/harness/skills/plans/`](workflow/harness/skills/plans/), [`workflow/plan_db.py`](workflow/plan_db.py) |
| **Hooks** | Block amounts the user never stated, and reject replies with untraced dollar amounts | [`workflow/harness/hooks.py`](workflow/harness/hooks.py), [`workflow/guards.py`](workflow/guards.py) |
| **Calculator** | Pure, deterministic cost sharing (copay, coinsurance, deductible). Refuses rules a person hasn't verified | [`workflow/calculate_cost.py`](workflow/calculate_cost.py) |

## Where the LLM runs

Every model call goes through one client ([`workflow/llm_client.py`](workflow/llm_client.py)), configured by `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. The model must read images (plan PDFs). The chat loop uses the Responses API.

| Task | Model input | Where |
| --- | --- | --- |
| Chat agent: choosing questions, plans and scenarios; writing replies | Text and tool calls | [`workflow/server.py`](workflow/server.py) |
| Recognizing an uploaded plan | Image of page 1 | [`workflow/harness/reader.py`](workflow/harness/reader.py) |
| Extracting benefit tables from plan PDFs (offline) | Page images and text | [`scripts/policy/extract_sbc.py`](scripts/policy/extract_sbc.py) |

## The data

**Price database (allowed amounts).** A streaming Python pipeline, with no LLM, read insurers' Transparency in Coverage files: 316 files, 70 GB compressed, 444 GB of JSON. It kept 629,842 negotiated rates for 41 billing codes across 68,333 Los Angeles providers in 7 networks (Anthem PPO Prudent Buyer, which UC SHIP uses; Anthem Pathway EPO and HMO; UnitedHealthcare Choice Plus; Cigna National PPO; Ambetter from Health Net; L.A. Care). They are grouped into 19 common visits, such as urgent care for the flu or an ER visit for a sprained ankle. The agent prices directly from this 3.1 GB SQLite database (not in git; on Railway it is a volume): the median over every Los Angeles provider in the plan's network, with a p10–p90 range. Details: [`scripts/mrf/README.md`](scripts/mrf/README.md).

**Plan database.** One row per price a plan states (service × network × tier), extracted from official plan PDFs ([`scripts/policy/extract_sbc.py`](scripts/policy/extract_sbc.py)) and built into [`data/plans/policy.sqlite`](data/plans/policy.sqlite) by [`scripts/policy/build_seed.py`](scripts/policy/build_seed.py). The calculator refuses any row that isn't verified or has a review flag. [`data/plans/plan_networks.csv`](data/plans/plan_networks.csv) says which price network each plan uses. Details: [`db/README.md`](db/README.md).

## Run it

Requirements: Python 3.12+, Node 20+ with pnpm, and poppler (`brew install poppler`) for PDF rendering.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# LLM settings (gitignored); any OpenAI-compatible endpoint
cat > .env <<'EOF'
LLM_BASE_URL=...
LLM_API_KEY=...
LLM_MODEL=...
# optional, for the fast no-reasoning calls; Qwen on vLLM:
# LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}
EOF

# 1. Agent service (port 8000)
(cd workflow && ../.venv/bin/uvicorn server:app --port 8000) &
# 2. Frontend (port 3000)
cd web && pnpm install && echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local && pnpm dev
```

Open http://localhost:3000. Without `NEXT_PUBLIC_API_BASE`, the frontend runs against a built-in mock backend instead.

## Tests

```bash
.venv/bin/python -m unittest discover tests   # calculator, plan database, guards, harness, MRF pipeline (80 tests)
cd web && pnpm lint && pnpm build
```

No test calls the LLM or the network.

## Repository layout

| Path | Contents |
| --- | --- |
| [`web/`](web/) | Next.js frontend: chat, form cards, estimate cards, document-reading panel |
| [`workflow/`](workflow/) | Agent service, harness, plan database matching, calculator, guards |
| [`scripts/mrf/`](scripts/mrf/) | Price database pipeline (no LLM) |
| [`scripts/policy/`](scripts/policy/) | Plan PDF extraction and the plan database build |
| [`data/`](data/) | Plan database, compiled price tables and insurer profiles |
| [`db/`](db/) | Database schema and data flow |
| [`docs/`](docs/) | Product definition, frontend–backend protocol, open problems |
| [`tests/`](tests/) | Unit tests |

## Limitations

- **Coverage is narrow.** Prices exist for 19 common visits in Los Angeles. Surgery, hospital stays and prescription drug prices aren't in the database.
- **A negotiated rate is an estimate.** Claim edits and bundling change what is finally paid. For now prices are the median across a network's Los Angeles providers, not a quote from a specific doctor; doctor-level pricing is the next step.
- **Few plans.** The plan database holds 11 Los Angeles plans. An uploaded plan that isn't in it can't be priced yet.
- **No premium subsidies, out-of-pocket maximum, or balance billing** in the calculator yet.
- It is not medical advice and doesn't guarantee coverage.
