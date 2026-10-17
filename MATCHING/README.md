# Branch A × MRF rate integration (overlay)

This overlay is a runnable integration of policy benefit rules (Branch A) with the MRF allowed-amount data. It
does not modify `workflow/`, `scripts/mrf/`, `data/` or any root file. Design decisions and open mappings are
in [`decisions.md`](decisions.md). The baseline snapshot is in [`baseline/manifest.json`](baseline/manifest.json).

```text
user describes a service
 → Branch A picks and reads a benefit rule              (original PolicyTools)
 → resolve_rate(rule_id[, option_id, npi])              (policy_mrf_map → exact MRF plan/network,
                                                         service_rate_map → A encounter or B components;
                                                         needs_clarification if several options)
 → RateLookup over the canonical views → quote_id     (aggregate A/B, exact NPI rate, or labelled nearby
                                                         Top-20 estimate; fail closed, JSONL trace)
 → calculate_with_quote(rule_id, quote_id, deductible)  (pure calculate_cost; B costed per component rule)
 → patient cost + rate source + match details
```

## Layout

| Path | Purpose |
|---|---|
| `baseline/manifest.json`, `baseline/files/workflow/*` | Snapshots of the adapted originals, with git commit, blob and sha256, and the recorded pre-existing state |
| `overlay/workflow/overlay_config.py` | Data paths (configurable through `BRANCH_A_*` env vars) and the import order (overlay → original workflow → scripts/mrf) |
| `overlay/workflow/policy_import_v2.py` | PDF import with adjacent-page category context, an adversarial source-image pass, and fail-closed category validation; AMD-only LLM |
| `overlay/workflow/auto_map_policy.py` | Uses AMD for semantic service-to-MRF matching, validates plan/network and codes/classes/POS/rates in Python, and runs deterministic lookup tests; unsupported mappings remain absent |
| `overlay/workflow/schema_check.py` | jsonschema validation against `overlay/schemas/` |
| `overlay/workflow/rate_resolver.py` | `Mappings`, `RateResolver` (benefit rule → verified MRF plan/network → A encounter or B components), `QuoteStore` |
| `overlay/workflow/canonical_adapter.py` | Read-only adapter: alias map (`FIELD_MAP`), `canonical_provider_rates`, `canonical_A_encounters`, `canonical_B_components`, `canonical_encounter_components` |
| `overlay/workflow/rate_lookup.py` | `RateLookup`: A/B aggregate lookups, exact NPI rates, labelled nearby Top-20 fallback, `rate_lookup_result.v1` |
| `overlay/workflow/trace_log.py` | JSONL trace log (run/request ids, stages, candidate counts, no free text) |
| `overlay/workflow/inspect_database.py` | Read-only database inspection with two-way NPI→rate join checks |
| `overlay/workflow/lookup_cli.py` | One lookup from a `rate_request.v1` JSON document |
| `overlay/workflow/build_zip_centroids.py` | Builds `overlay/data/zcta_centroids_ca.csv` from the Census ZCTA Gazetteer |
| `overlay/workflow/branch_a_tools.py` | `resolve_rate`, `calculate_with_quote`, per-component costing, manual fallback labeled `user_provided` |
| `overlay/workflow/quote_guards.py` | `QuoteGuard`: trusts stored quotes, gates clarification, requires NPIs and deductibles to come from the user |
| `overlay/workflow/branch_a_agent.py` | Strict tool definitions, `execute_tool`, and the AMD chat-completions loop |
| `overlay/workflow/build_encounter_labels.py` | Generates and checks `overlay/data/encounter_labels.v1.json` |
| `overlay/workflow/list_pending_mappings.py` | Lists mappings that still need a human |
| `overlay/schemas/*.schema.json` | `policy_metadata.v1`, `benefit_rule.v2`, `policy_mrf_map.v1`, `service_rate_map.v1`, `rate_quote.v1`, `rate_request.v1`, `rate_lookup_result.v1` |
| `overlay/data/*.json` | Mappings: drafts are `pending_review`; the label map is generated |
| `overlay/tests/` | 93 unittest tests; `fixtures.py` builds synthetic CSV, SQLite (incl. a provider-level nearby fixture), mapping and rule data; `test_real_database.py` runs on the local database when present |
| `matching_report.md`, `reports/db_inspection.json` | Database facts, column mapping, end-to-end samples and uncovered conditions |

## Run (from the repository root; nothing is overwritten)

```bash
pip install -r branch-a-mrf-integration/overlay/requirements.txt
python branch-a-mrf-integration/overlay/run_tests.py          # overlay tests (no LLM, no network)
python -m unittest discover tests                             # original tests, unchanged

cd branch-a-mrf-integration/overlay/workflow
python build_encounter_labels.py --check                      # label map matches encounters.csv
python list_pending_mappings.py                               # what still needs human review
python inspect_database.py --distinct --out /tmp/db.json      # read-only DB check before relying on NPI lookups

# one deterministic lookup (A: encounter_key; B: billing_code + billing_class + place_of_service; optional NPI)
python lookup_cli.py --no-providers '{"insurer": null, "plan_key": "anthem_ca_prudent_buyer_ppo",
  "network": "CA PPO PRUDENT BUYER", "encounter_key": "urgent_care_flu", "service_date": null,
  "selected_provider_npi": null}'

# import a policy PDF (AMD endpoint only)
export AMD_BASE_URL=... AMD_API_KEY=... AMD_MODEL=...
python policy_import_v2.py --policy-plan-id uc_ship_2026 --metadata-pdf plan.pdf --metadata-pages 1-3 \
    --benefit-pages 12-15 --out-dir /tmp/uc_ship_2026

# chat agent over imported rules
python branch_a_agent.py --plan uc_ship_2026 --rules /tmp/uc_ship_2026/benefit_rules.json

# automatically match a newly imported policy (AMD semantic matching + deterministic validation),
# create an isolated overlay directory, and test every accepted option against the real rate lookup
python branch-a-mrf-integration/overlay/workflow/auto_map_policy.py \
    --policy-plan-id prime_500_2026 \
    --metadata local_data/policy-import/policy_metadata.json \
    --rules local_data/policy-import/benefit_rules.json \
    --out-dir local_data/policy-import/auto-mapping
```

The automatic mapper writes a run-specific overlay, `auto_mapping_audit.json`, an append-only
`auto_mapping_steps.jsonl` run trace, and checkpoints each completed AMD call so a timeout only retries the
unfinished stage. Benefit services are sent in small batches; omitted items are retried individually, while
explicit no-candidate and still-omitted outcomes are recorded separately. The primary AMD model matches by service meaning rather than exact label wording and can
propose multiple priced options with a clarification question. There is no second-model veto for semantic
breadth. Python still validates the exact plan/network, catalog identifiers, code, billing class, POS, and an
eligible allowed-amount row; a uniquely identifiable leading-punctuation typo in a component name is normalized
and logged. Plan/network identity still receives a separate adversarial review. Out-of-network quotes remain
hard-blocked from in-network MRF rates.
The AMD environment variables are required; no fallback model is used. Set
`BRANCH_A_OVERLAY_DATA=local_data/policy-import/auto-mapping` to run `lookup_cli.py` or the agent against the
generated mappings.

## Collected MATCHING snapshot

This directory was created to collect the current overlay implementation and the Prime 500 run evidence. It
contains a copy of the overlay, its tests/schemas/data, the related design documents, and the run output under
`run-evidence/prime_500_2026/`. The original working files under `branch-a-mrf-integration/` were not moved.

See [`ISSUES_AND_DESIGN.md`](ISSUES_AND_DESIGN.md) for the system flow, all 32 blocked service examples, the
local data audit, and unresolved work. The exact run audit is in
[`run-evidence/prime_500_2026/auto_mapping_audit.json`](run-evidence/prime_500_2026/auto_mapping_audit.json).

The copied test suite can be run from the repository root with:

```bash
./.venv/bin/python MATCHING/overlay/run_tests.py
```

For another mapping run, use the copied mapper with the same AMD-only environment variables and point its
inputs/output to the desired policy files and a new output directory. Do not overwrite the checked-in
`run-evidence/prime_500_2026/` snapshot.

For consecutive Schedule of Benefits pages, the importer supplies the preceding page as category-heading
context and runs a separate visual verification pass over each extracted row. Verified corrections are recorded
in `policy_metadata.json` under `extraction.benefit_row_verification`; missing categories, uncertain corrections,
or unaudited changes stop the import.

To use different data, set `BRANCH_A_RATES_CSV`, `BRANCH_A_ENCOUNTERS_CSV`, `BRANCH_A_PLAN_PROFILES`,
`BRANCH_A_RATES_SQLITE` (needed for NPI lookups), `BRANCH_A_NPI_DIRECTORY` (NPPES locations for the nearby
fallback), `BRANCH_A_ZIP_CENTROIDS`, `BRANCH_A_BILLING_CODES`, `BRANCH_A_LOG_DIR` or `BRANCH_A_OVERLAY_DATA`.

A plan prices nothing until a person sets `review_status: "verified"` and `reviewed_by` on its entries in
`overlay/data/policy_mrf_map.v1.json` and `service_rate_map.v1.json`. Until then the resolver returns
`mapping_pending_review`.
