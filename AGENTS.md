# AGENTS.md

This file provides guidance to AI coding agents (Codex, Claude Code, etc.) when working with code in this repository.

## What this is

A health-insurance agent for US college students who must buy their own commercial insurance (for example, PhD students over 26 who can no longer use a parent's plan). The product does not target international students and does not handle school waiver requirements. `docs/PRODUCT.md` (mostly in Chinese) is the source of truth for the product, including the demo flow.

The product is a chat-first matching agent. Each turn, the LLM picks questions from a question bank (a HINT injected into the prompt) and sends them as form cards in the chat. It then matches three sources:
- the questionnaire answers, which include the user's weights for each factor;
- the policy-information database: everything in plan PDFs except prices, covering UC SHIP plus a few major US commercial insurers;
- the allowed-amount database: negotiated rates parsed from insurers' Transparency in Coverage MRFs, stored in SQLite.

Python computes all numbers and rankings. The LLM compares plans, decides, and writes the reply. When a user names an insurer that is not in the database, a Playwright search worker on Railway looks it up and streams text progress. Search results inform the judgment but are never used to compute costs. Everything deploys as one Railway service, `agent-api` (project `student-health-agent`, region iad, from `main`, built by the root `Dockerfile`): the Next.js frontend is exported to static files and served by the FastAPI agent service, which also serves the API under `/api`; the rates database is on the volume `rates-data` at `/data`. Public URL: https://agent-api-production-adc8.up.railway.app. There is no Vercel. There is no Supabase: both databases are read-only SQLite files (`db/README.md`). The policy database `data/plans/policy.sqlite` is built from the extraction CSVs in `data/plans/extracted/` by `scripts/policy/build_seed.py` and deployed with the code; the 3.1 GB rates database sits on a Railway volume (`/data/rates.sqlite`, env `BRANCH_A_RATES_SQLITE`). Premium subsidies are not computed.

The frontend is in `web/` (Next.js + Tailwind + shadcn/ui) and talks to the backend over the SSE protocol in `docs/frontend-protocol.md`. The agent service is `workflow/server.py` (`uvicorn server:app --port 8000` from `workflow/`; set `NEXT_PUBLIC_API_BASE=http://localhost:8000` in `web/.env.local`); it serves chat, form, estimate and confirm cards, and calls the search worker (`search_worker/worker.py`, port 8100) when `SEARCH_WORKER_URL` is set. The dashboard report is not wired yet; with `NEXT_PUBLIC_API_BASE` unset the frontend uses a built-in mock (`web/src/lib/mock-script.ts`, placeholder numbers only). The allowed-amount (price) database pipeline lives in `scripts/mrf/` (no LLM calls; see `scripts/mrf/README.md`); its outputs are in `data/mrf/`.

## Hard constraint: AMD-only LLMs

**Every LLM call in this project must go to the AMD API. No other provider is allowed, including OpenAI, and this applies during development and testing too.** The `openai` Python package may stay as the client library, pointed at the AMD endpoint via `base_url`. All clients are created by `workflow/amd_client.py` (AMD endpoint, model `Qwen3.8-27B`). Do not add OpenAI defaults or fallbacks.

This project is an entry in the AMD Developer Hackathon: ACT III (Health and Wellbeing track). "The AMD API" is an OpenAI-compatible endpoint serving an open model. Organizers say "Token Factory" counts as vLLM; if it turns out unsuitable, the endpoint is the team's own vLLM on an AMD Developer Cloud MI300X. Either way, the code only needs `base_url`, an API key, and a model name. The planned model is Qwen3.5-35B-A3B, which is multimodal, so one model handles both chat and PDF reading. Do not use DeepSeek.

Open problems and planned improvements are tracked in `docs/improvements.md`. **`docs/technical-plan.md` is the agreed technical path.** It covers the architecture, the two databases, search, the models, deployment, and the schedule. Follow it, and update it before changing direction. Plan knowledge lives in a verifiable catalog (`policies/*.json`, rules JSON v2), never in model memory. Never fine-tune on extraction output that a human has not verified, and never let a coding assistant author catalog data: catalog data must come from humans (for example, Perplexity search results that a person checked against the official source, imported with `scripts/policy/import_plan_csv.py`) or from the AMD model. `docs/research/hark-interaction-patterns.md` describes the reference UI interaction patterns. Borrow the patterns only; do not copy code or visuals.

Because the runtime models are open-source and weaker than frontier models, put capability into code written at development time. Keep runtime LLM tasks narrow: choosing from fixed options, filling parameters, and writing short text. Constrain outputs with JSON schemas, and keep all numbers in Python.

## Running

Tests: `python -m unittest discover tests` and `python branch-a-mrf-integration/overlay/run_tests.py`; frontend: `pnpm lint` and `pnpm build` in `web/`. The README has the full local setup. `requirements.txt` lists `openai` plus `ijson` and `requests` for the MRF pipeline (`ijson` must use its C backend, `yajl2_c`). Every LLM call needs `AMD_BASE_URL`, `AMD_API_KEY` and `AMD_MODEL`, read from the environment or the gitignored repo-root `.env`. Qwen's chat template rejects a system/developer message anywhere but first, so mid-conversation feedback must use the `user` role. Run scripts from inside `workflow/`.

Frontend (Node + pnpm). With `NEXT_PUBLIC_API_BASE` unset it uses the built-in mock backend:

```bash
cd web && pnpm install && pnpm dev   # http://localhost:3000; also: pnpm lint, pnpm build
```

```bash
pip install -r requirements.txt
# repo-root .env (gitignored): AMD_BASE_URL=https://developer.amd.com.cn/radeon/api/v1, AMD_API_KEY=..., AMD_MODEL=Qwen3.8-27B
cd workflow
python import_policy.py PDF PLAN START END [--columns ...] [--out FILE]   # or no args for prompts
python agent.py              # LLM tool-calling chat agent; flags: --plan prime_100 --rules <path> --model <AMD model>
```

`import_policy.py` uses `pdftoppm` from poppler, which must be on PATH. It writes `normalized_pdf_insurance_rules.json` to the current directory. `agent_tools.py` expects that file in `workflow/` by default; the current 66-rule `prime_100` catalog is checked in there. Long imports can run as parallel page batches; merge them and give a person the review sheet with `scripts/policy/review_sheet.py`.

To test the calculator on its own, without an LLM, call `calculate_cost(rule_dict, allowed_amount=..., remaining_deductible=...)` directly.

## Architecture

Core design rule from `docs/PRODUCT.md`: **the LLM turns unstructured input into structured data and makes judgments (which questions to ask, which plan fits the user's weights, how to reply). Python computes every number and every ranking, and checks hard facts.** The agent must never compute costs itself.

Pipeline:

1. **`import_policy.py`**: Converts the PDF page range to 300-dpi PNGs, has a vision model extract each benefit table row, then has a second model call normalize the rows. It tags each rule with `plan` and `source_page`. This is a top-level script with no functions to import.
2. **Rule schema** (one dict per service × network): `plan`, `service`, `network`, `copay_usd`, `plan_pays_percentage`, `patient_pays_percentage`, `deductible_applies`, `prior_authorization`, `coverage_status` (`covered`/`not_covered`/unknown), `source_text`, `source_page`, `needs_review`.
3. **`calculate_cost.py`**: Pure and deterministic, with no I/O and no LLM. It always returns `{status, patient_cost, missing_fields, message}`, where status is one of `estimated`, `needs_information`, `needs_review`, `not_covered`, or `invalid_input`. It is deliberately conservative:
   - It refuses unless `needs_review is False` is set explicitly.
   - It handles copay-only, coinsurance-only, copay then coinsurance (no deductible), and a copay after the deductible (the full amount up to the remaining deductible, then the copay). Combined copay + coinsurance subject to the deductible returns `needs_review`.
   - It ignores the out-of-pocket max and balance billing.
   - **Percentages use 0–100 units.** The current import prompt asks for whole numbers.
4. **Plan database mode (the product; `AGENT_MODE=plans`, the server default).** `workflow/plan_db.py` (no LLM) does the matching and costing on `data/plans/policy.sqlite` + `data/mrf/allowed_amounts.csv`: a *scenario* (one common visit from `data/mrf/encounters.csv`) is split into components, each paid under one benefit (`SCENARIOS`); each component is matched to one benefit row (everyday version first, then the component), priced from the plan's network through `plan_networks`, components under the same row are one bill (one copay per visit), and `calculate_cost` runs per row with the remaining deductible used in order. `compare()` is the Arena: the same scenarios on several plans, deductible not met / met, ranked. The `plans` skill (`harness/skills/plans/`) exposes `list_plans`, `estimate_cost` and `compare_plans`; the model only picks plan ids, scenario keys and the network, and passes only a user-stated remaining deductible (guarded). Main prompt: `harness/prompts/system_plans.md`. Tests: `tests/test_plan_db.py`.
5. **The web agent harness** (`workflow/harness/`, used by `server.py`; with `AGENT_MODE=legacy` it runs one plan's rules JSON as below) has four parts. Keep each concern in its own part:
   - **Main prompt** `harness/prompts/system.md`: who the agent is (an insurance advisor, not a one-visit estimator), the ground rules, how to work, and style. The ground rules carry over every rule from `agent.py`'s `INSTRUCTIONS`; each encodes an observed failure, so edit them carefully.
   - **Skills** `harness/skills/<name>/`: `SKILL.md` (frontmatter plus the instructions placed in the prompt) and `skill.py` (`build()` returns the skill's tools). The `questionnaire` skill holds the question bank (`bank.py`) and the `ask_questions` tool; its schema enumerates the bank IDs, so the model can't invent questions. Every question names the database query or calculator input it feeds (`maps_to`).
   - The `pricing` skill wraps the overlay in `branch-a-mrf-integration/` (imported read-only): `get_allowed_amount` stores a rate quote under a `quote_id`, and `calculate_with_quote` costs it, so the model never handles the amount.
   - **Tools** `harness/mcp.py`: MCP-shaped tools (name, description, schema, handler) with no conversation rules: `get_policy_rule`, `calculate_estimate`, `get_definition` (policy definitions on demand instead of in the prompt), `search_insurer`.
   - **Hooks** `harness/hooks.py`: wraps `guards.py` unchanged (the overlay's `QuoteGuard` subclass when pricing is on), and adds lookup-before-calculate, the questionnaire rules (no re-asking answered questions, prefill only values the user stated), questionnaire numbers as trusted calculator inputs, and Python sending the form itself when the calculator reports missing amounts.
   - **Plan upload** `harness/reader.py`: the AMD model reads page 1 of an uploaded PDF for the plan's identity only; Python matches it against `policies/catalog.json` and switches the session to that plan's rules.
6. **Terminal agent** (also used by tests and the overlay for shared helpers):
   - `agent.py` + `agent_tools.py` (terminal agent): `PolicyTools` loads rules for one plan and exposes three tools to the model: `get_catalog`, `get_policy_rule`, and `calculate_estimate`. The model must retrieve a rule before it calculates. The behavioral contract lives in the long `INSTRUCTIONS` prompt in `agent.py`. Examples: missing amounts are null, never 0. Billed price ≠ allowed amount. Annual deductible ≠ remaining deductible. One service occurrence per estimate. A provisional walkthrough is allowed only when the result is `needs_review`. Edit that prompt carefully, because each line encodes a specific failure that was observed.
   - `guards.py` enforces "ask when unsure" in code, not in the prompt, so keep it in the loop when changing `agent.py` (including the AMD migration). It has three hooks. `pre_tool` blocks any amount the user never stated. `post_tool` withholds an `estimated` result until the user confirms the understood service, network, and amounts in the terminal. `check_reply` rejects replies containing dollar amounts traceable to neither the user nor a tool, and replies that fail to ask for missing fields; after 2 retries it substitutes a deterministic question. Tests: `python -m unittest discover tests`.

## Known inconsistencies

- `guards.py` `post_tool` asks for confirmation on every `estimated` result. The agreed rule (`docs/frontend-protocol.md`) is to confirm only when a value was inferred from chat; values from the questionnaire skip confirmation.
- `guards.py` `pre_tool` accepts amounts the user typed, and in the web harness also questionnaire numbers. Rate amounts never reach the model: they stay inside quotes (`calculate_with_quote`).
