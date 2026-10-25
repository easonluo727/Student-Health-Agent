# AGENTS.md

This file provides guidance to AI coding agents (Codex, Claude Code, etc.) when working with code in this repository.

## What this is

A health-insurance agent for everyone in the US, across all coverage groups: employer plans, individual and marketplace plans, student plans, and people shopping for a plan. The current data covers only a first set of Los Angeles plans; widening it is ongoing work. `docs/PRODUCT.md` (mostly in Chinese) is the source of truth for the product, including the demo flow.

The product is a chat-first matching agent: it estimates what care costs, compares plans (the Arena), and recommends specific doctors. Each turn, the LLM picks questions from a question bank (a HINT injected into the prompt) and sends them as form cards in the chat. It then matches four sources:
- the questionnaire answers, which include the user's weights for each factor;
- the policy-information database: everything in plan PDFs except prices, so far UC SHIP and Covered California plans in Los Angeles, plus each plan's premium (list price, by age and rating region; UC SHIP by student level);
- the allowed-amount database: negotiated rates parsed from insurers' Transparency in Coverage MRFs, stored in SQLite;
- doctor information: who and where (NPPES, by NPI) and how good (public quality data; planned).

**Roadmap and pricing rule (2026-10-20, details in `docs/improvements.md`):** geographic doctor matching → doctor quality ("rate my doctor") → deeper computer search → login → long-term memory of personal information. Prices must never be a network median in the answer: if the user names a doctor, price that doctor; otherwise recommend specific doctors and price each. Python tools find candidates and compute; the agent judges and explains. The current median pricing (below) is an interim step until step 1 is done.

Python computes all numbers and rankings. The LLM compares plans, decides, and writes the reply. When a user names a plan or insurer that is not in the database, the agent says so; it does not guess. Premium subsidies are not computed: the Arena ranks by a year's list premium plus the visits.

The frontend is in `web/` (Next.js + Tailwind + shadcn/ui) and talks to the backend over the SSE protocol in `docs/frontend-protocol.md`. The agent service is `workflow/server.py` (`uvicorn server:app --port 8000` from `workflow/`; set `NEXT_PUBLIC_API_BASE=http://localhost:8000` in `web/.env.local`); it serves chat, form and estimate cards. The dashboard report is not wired yet; with `NEXT_PUBLIC_API_BASE` unset the frontend uses a built-in mock (`web/src/lib/mock-script.ts`, placeholder numbers only). The allowed-amount (price) database pipeline lives in `scripts/mrf/` (no LLM calls; see `scripts/mrf/README.md`); its outputs are in `data/mrf/`.

## Data

There is no Supabase: both databases are read-only SQLite files (`db/README.md`). **To add an insurance plan, follow "Adding a plan" in `db/README.md`.**
- **Policy database** `data/plans/policy.sqlite` (about 100 KB, in git): built from the extraction CSVs in `data/plans/extracted/` by `scripts/policy/build_seed.py`. Plan PDFs are extracted into those CSVs by `scripts/policy/extract_sbc.py` (LLM), or a person fills them from a checked search (the Perplexity prompt).
- **Rate store** `rates.sqlite` (3.1 GB, not in git; env `RATES_DB`, default `data/mrf/rates.sqlite`, `/data/rates.sqlite` on Railway): every negotiated rate the MRF pipeline kept, per network, billing code and Los Angeles provider. The agent prices from it directly with `scripts/mrf/allowed.py`: per component, the median over every Los Angeles provider in the network, with p10–p90 over the same providers (interim; `allowed.component(..., npi=...)` already returns one provider's rate for doctor-level pricing). Results are cached per day; `server.py` warms the cache in the background at startup (about 2–3 minutes, peak about 750 MB). Without the file, only flat copays are computed.

## Deployment

One Railway service, `agent-api` (project `student-health-agent`, region iad, deployed from `main` of `easonluo727/Student-Health-Agent`, built by the root `Dockerfile`). The Dockerfile exports the frontend to static files (`STATIC_EXPORT=1`, `web/out`) and starts FastAPI, which serves the frontend at `/` and the API under `/api` on the same origin, so no CORS is needed. Public URL: https://agent-api-production-adc8.up.railway.app. The volume `rates-data` at `/data` holds `rates.sqlite` (uploaded with `railway volume files upload`; volumes need the Hobby plan or above), plus the conversations (`sessions.sqlite`, `workflow/sessions.py`, kept a day) and uploaded PDFs (`uploads/`). There is no Vercel.

## LLM provider

The LLM provider is configuration, not code. All clients are created by `workflow/llm_client.py`, which talks to any OpenAI-compatible endpoint set by `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. Optional `LLM_EXTRA_BODY` (JSON) is sent with the fast calls that should skip reasoning (PDF reading and extraction); for Qwen on vLLM it is `{"chat_template_kwargs": {"enable_thinking": false}}`. Each competition or deployment picks its provider with these variables. Do not hard-code a provider, and do not create clients anywhere else. The agent loop in `server.py` uses the Responses API, so the endpoint must support it.

Open problems and planned improvements are tracked in `docs/improvements.md`; update it when you fix one or find a new one. Plan knowledge lives in the policy database, never in model memory. Anyone may add or correct plan data (a person, a coding assistant, or `scripts/policy/extract_sbc.py`), but every row must carry its source: `source_url`, `source_page` and a verbatim `quote` from the official document, so it can be checked. Never fine-tune on extraction output that a human has not verified. `docs/research/hark-interaction-patterns.md` describes the reference UI interaction patterns. Borrow the patterns only; do not copy code or visuals.

Assume the runtime model may be a weaker open model, so put capability into code written at development time. Keep runtime LLM tasks narrow: choosing from fixed options, filling parameters, and writing short text. Constrain outputs with JSON schemas, and keep all numbers in Python.

## Running

Tests: `python -m unittest discover tests`; frontend: `pnpm lint` and `pnpm build` in `web/`. The README has the full local setup. `requirements.txt` lists `openai` plus `ijson` and `requests` for the MRF pipeline (`ijson` must use its C backend, `yajl2_c`). Every LLM call needs `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`, read from the environment or the gitignored repo-root `.env`. Some chat templates (Qwen's) reject a system/developer message anywhere but first, so mid-conversation feedback must use the `user` role. Run the server from inside `workflow/`. PDF reading uses poppler (`pdftoppm`, `pdftotext`, `pdfinfo`), which must be on PATH.

```bash
cd web && pnpm install && pnpm dev   # http://localhost:3000, mock backend unless NEXT_PUBLIC_API_BASE is set
pip install -r requirements.txt
cd workflow && uvicorn server:app --port 8000
```

To test the calculator on its own, without an LLM, call `calculate_cost(rule_dict, allowed_amount=..., remaining_deductible=...)` directly; for a whole visit, `plan_db.PlanDB().estimate(plan_id, scenario)`.

## Architecture

Core design rule from `docs/PRODUCT.md`: **the LLM turns unstructured input into structured data and makes judgments (which questions to ask, which plan fits the user's weights, how to reply). Python computes every number and every ranking, and checks hard facts.** The agent must never compute costs itself.

1. **`calculate_cost.py`**: Pure and deterministic, with no I/O and no LLM. Input is one benefit rule (`copay_usd`, `plan_pays_percentage`, `patient_pays_percentage`, `deductible_applies`, `coverage_status`, `needs_review`, ...). It always returns `{status, patient_cost, missing_fields, message}`, where status is one of `estimated`, `needs_information`, `needs_review`, `not_covered`, or `invalid_input`. It is deliberately conservative:
   - It refuses unless `needs_review is False` is set explicitly.
   - It handles copay-only, coinsurance-only, copay then coinsurance (no deductible), and a copay after the deductible (the full amount up to the remaining deductible, then the copay). Combined copay + coinsurance subject to the deductible returns `needs_review`.
   - It ignores the out-of-pocket max and balance billing.
   - **Percentages use 0–100 units.**
2. **`plan_db.py`** (no LLM) does the matching and costing on `data/plans/policy.sqlite` + the rate store: a *scenario* (one common visit from `data/mrf/encounters.csv`, which lists its components and their billing codes) is split into components, each paid under one benefit (`SCENARIOS`); each component is matched to one benefit row (everyday version first, then the component), priced in the plan's network (`plan_networks.mrf_plan_key` → rate store; only rows with `confirmed` = 1, meaning the evidence in `notes` names the network), components under the same row are one bill (one copay per visit), and `calculate_cost` runs per row with the remaining deductible used in order. `compare()` is the Arena: the same scenarios on several plans, deductible not met / met, ranked. Tests: `tests/test_plan_db.py`.
3. **The agent harness** (`workflow/harness/`, used by `server.py`) has four parts. Keep each concern in its own part:
   - **Main prompt** `harness/prompts/system.md`: who the agent is, the ground rules, how to work, and style. Each ground rule encodes an observed failure (missing amounts are null, never 0; billed price ≠ allowed amount; annual deductible ≠ remaining deductible), so edit them carefully.
   - **Skills** `harness/skills/<name>/`: `SKILL.md` (frontmatter plus the instructions placed in the prompt) and `skill.py` (`build()` returns the skill's tools). The `questionnaire` skill holds the question bank (`bank.py`) and the `ask_questions` tool; its schema enumerates the bank IDs, so the model can't invent questions. Every question names the database query or calculator input it feeds (`maps_to`). The `plans` skill exposes `list_plans`, `estimate_cost` and `compare_plans`; the model only picks plan ids, scenario keys and the network, and passes only a user-stated remaining deductible.
   - **Tools** `harness/mcp.py`: the MCP-shaped tool type (name, description, schema, handler) and strict argument parsing, with no conversation rules.
   - **Hooks** `harness/hooks.py`: wraps `guards.py` unchanged, and adds the questionnaire rules (no re-asking answered questions, prefill only values the user stated), questionnaire numbers as trusted calculator inputs, and Python sending the form itself when the calculator reports missing inputs.
   - **Plan upload** `harness/reader.py`: the model reads page 1 of an uploaded PDF for the plan's identity only; Python matches it against the plan database. Benefit numbers always come from the database, never from the upload.
4. **`guards.py`** enforces "ask when unsure" in code, not in the prompt. `pre_tool` blocks any amount the user never stated (here: the remaining deductible). `check_reply` rejects replies containing dollar amounts traceable to neither the user nor a tool, and replies that fail to ask for missing fields; after 2 retries it substitutes a deterministic question. Its `post_tool` confirmation step only applies to the old `calculate_estimate` tool, which the harness no longer offers, so it never fires. Tests: `tests/test_guards.py`.
