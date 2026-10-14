# AGENTS.md

This file provides guidance to AI coding agents (Codex, Claude Code, etc.) when working with code in this repository.

## What this is

A prototype that estimates out-of-pocket costs for international students under a student health insurance plan. It turns policy PDFs into structured rules, then answers cost questions against those rules. `PRODUCT.md` (mostly in Chinese) holds the product vision. The product is a chat-first matching agent. The main window is a chat. The LLM chooses whether to send one of several predesigned questionnaires, which are defined in a HINT injected into the prompt and rendered as form cards. The agent then matches three sources: the questionnaire answers, the policy-rules database, and the allowed-amount (MRF price) database. Python does all the math. The product covers cost estimates and an "Insurance Arena" plan comparison shown in a dashboard panel beside the chat, served by a FastAPI backend. **None of the frontend, questionnaires, price database, or backend exists yet.** All current code is Python CLI scripts in `workflow/`. `archive/` holds superseded scripts.

## Hard constraint: AMD-only LLMs

**Every LLM call in this project must go to the AMD API. No other provider is allowed, including OpenAI, and this applies during development and testing too.** The `openai` Python package may stay as the client library, pointed at the AMD endpoint via `base_url`. The current code still calls OpenAI models (`gpt-5-nano`, `gpt-6-luna`); migrating it is in progress. Do not add OpenAI defaults or fallbacks.

In practice, "the AMD API" means the team's own vLLM server (an OpenAI-compatible endpoint) on an AMD Developer Cloud MI300X droplet. This project is an entry in the AMD Developer Hackathon: ACT III.

**`docs/technical-plan.md` is the agreed technical path.** It covers the architecture, the insurance catalog and its data sources, deep search, model deployment, and the schedule. Follow it, and update it before changing direction. Plan knowledge lives in a verifiable catalog (`policies/*.json`, rules JSON v2), never in model memory. Never fine-tune on extraction output that a human has not verified, and never let a coding assistant author catalog data: catalog data must come from the AMD model or from humans. `docs/research/hark-interaction-patterns.md` describes the reference UI interaction patterns. Borrow the patterns only; do not copy code or visuals.

Because the runtime models are open-source and weaker than frontier models, put capability into code written at development time. Keep runtime LLM tasks narrow: choosing from fixed options, filling parameters, and writing short text. Constrain outputs with JSON schemas, and keep all numbers in Python.

## Running

There is no build, lint, or test setup. The only dependency in `requirements.txt` is `openai`. `interpret_request.py` also imports `pydantic`. Every LLM call requires `OPENAI_API_KEY`. Run scripts from inside `workflow/`.

```bash
pip install -r requirements.txt pydantic
cd workflow
python import_policy.py      # interactive: PDF path, plan name, start/end page of the Schedule of Benefits
python agent.py              # LLM tool-calling chat agent; flags: --plan prime_100 --rules <path> --model gpt-5-nano
python estimate_pro.py       # older fixed-pipeline alternative to agent.py
```

`import_policy.py` uses `pdftoppm` from poppler, which must be on PATH. It writes `normalized_pdf_insurance_rules.json` to the current directory. `agent_tools.py` and `estimate_pro.py` expect that file in `workflow/`. The only checked-in copy is in `archive/`, so copy it over or run the import first.

To test the calculator on its own, without an LLM, call `calculate_cost(rule_dict, allowed_amount=..., remaining_deductible=...)` directly.

## Architecture

Core design rule from `PRODUCT.md`: **the LLM turns unstructured input into structured data, and Python makes every decision and does all the math.** The agent must never compute costs itself.

Pipeline:

1. **`import_policy.py`**: Converts the PDF page range to 300-dpi PNGs, has a vision model extract each benefit table row, then has a second model call normalize the rows. It tags each rule with `plan` and `source_page`. This is a top-level script with no functions to import.
2. **Rule schema** (one dict per service × network): `plan`, `service`, `network`, `copay_usd`, `plan_pays_percentage`, `patient_pays_percentage`, `deductible_applies`, `prior_authorization`, `coverage_status` (`covered`/`not_covered`/unknown), `source_text`, `source_page`, `needs_review`.
3. **`calculate_cost.py`**: Pure and deterministic, with no I/O and no LLM. It always returns `{status, patient_cost, missing_fields, message}`, where status is one of `estimated`, `needs_information`, `needs_review`, `not_covered`, or `invalid_input`. It is deliberately conservative:
   - It refuses unless `needs_review is False` is set explicitly.
   - It only handles rules that are copay-only or coinsurance-only. Combined copay + coinsurance, or a copay subject to the deductible, returns `needs_review`.
   - It ignores the out-of-pocket max and balance billing.
   - **Percentages use 0–100 units.** The current import prompt asks for whole numbers. The archived JSON still has 0–1 values (e.g. `0.8`), which will fail the "sum to 100" check.
4. **Two front ends over the calculator:**
   - `agent.py` + `agent_tools.py` (current direction): `PolicyTools` loads rules for one plan and exposes three tools to the model: `get_catalog`, `get_policy_rule`, and `calculate_estimate`. The model must retrieve a rule before it calculates. The behavioral contract lives in the long `INSTRUCTIONS` prompt in `agent.py`. Examples: missing amounts are null, never 0. Billed price ≠ allowed amount. Annual deductible ≠ remaining deductible. One service occurrence per estimate. A provisional walkthrough is allowed only when the result is `needs_review`. Edit that prompt carefully, because each line encodes a specific failure that was observed.
   - `guards.py` enforces "ask when unsure" in code, not in the prompt, so keep it in the loop when changing `agent.py` (including the AMD migration). It has three hooks. `pre_tool` blocks any amount the user never stated. `post_tool` withholds an `estimated` result until the user confirms the understood service, network, and amounts in the terminal. `check_reply` rejects replies containing dollar amounts traceable to neither the user nor a tool, and replies that fail to ask for missing fields; after 2 retries it substitutes a deterministic question. Tests: `python -m unittest discover tests`.
   - `estimate_pro.py` + `interpret_request.py` (older): a fixed pipeline. A Pydantic structured-output call matches the request to catalog entries and may return a clarification question. Python then prompts for amounts and calls the calculator.

## Known inconsistencies

- `.github/workflows/run-estimator.yml` runs `python estimate_multiple_2.0.py` from the repo root, but that script now lives in `archive/`, so the workflow is broken as written.
- The code uses model names `gpt-5-nano` and `gpt-6-luna` (in `import_policy.py`).
