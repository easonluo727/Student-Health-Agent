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
 → Python RateResolver returns a quote (quote_id)       (CSV aggregates or SQLite provider rates; fail closed)
 → calculate_with_quote(rule_id, quote_id, deductible)  (pure calculate_cost; B costed per component rule)
 → patient cost + rate source + match details
```

## Layout

| Path | Purpose |
|---|---|
| `baseline/manifest.json`, `baseline/files/workflow/*` | Snapshots of the adapted originals, with git commit, blob and sha256, and the recorded pre-existing state |
| `overlay/workflow/overlay_config.py` | Data paths (configurable through `BRANCH_A_*` env vars) and the import order (overlay → original workflow → scripts/mrf) |
| `overlay/workflow/policy_import_v2.py` | PDF import that writes metadata and the benefit schedule separately, plus a pending MRF map; AMD-only LLM |
| `overlay/workflow/schema_check.py` | jsonschema validation against `overlay/schemas/` |
| `overlay/workflow/rate_resolver.py` | `Mappings`, `CsvRates`, `SqliteRates`, `RateResolver` (A_encounter and B_component), `QuoteStore` |
| `overlay/workflow/branch_a_tools.py` | `resolve_rate`, `calculate_with_quote`, per-component costing, manual fallback labeled `user_provided` |
| `overlay/workflow/quote_guards.py` | `QuoteGuard`: trusts stored quotes, gates clarification, requires NPIs and deductibles to come from the user |
| `overlay/workflow/branch_a_agent.py` | Strict tool definitions, `execute_tool`, and the AMD chat-completions loop |
| `overlay/workflow/build_encounter_labels.py` | Generates and checks `overlay/data/encounter_labels.v1.json` |
| `overlay/workflow/list_pending_mappings.py` | Lists mappings that still need a human |
| `overlay/schemas/*.schema.json` | `policy_metadata.v1`, `benefit_rule.v2`, `policy_mrf_map.v1`, `service_rate_map.v1`, `rate_quote.v1` |
| `overlay/data/*.json` | Mappings: drafts are `pending_review`; the label map is generated |
| `overlay/tests/` | 50 unittest tests; `fixtures.py` builds synthetic CSV, SQLite, mapping and rule data |

## Run (from the repository root; nothing is overwritten)

```bash
pip install -r branch-a-mrf-integration/overlay/requirements.txt
python branch-a-mrf-integration/overlay/run_tests.py          # overlay tests (no LLM, no network)
python -m unittest discover tests                             # original tests, unchanged

cd branch-a-mrf-integration/overlay/workflow
python build_encounter_labels.py --check                      # label map matches encounters.csv
python list_pending_mappings.py                               # what still needs human review

# import a policy PDF (AMD endpoint only)
export AMD_API_BASE_URL=... AMD_API_KEY=... AMD_MODEL=...
python policy_import_v2.py --policy-plan-id uc_ship_2026 --metadata-pdf plan.pdf --metadata-pages 1-3 \
    --benefit-pages 12-15 --out-dir /tmp/uc_ship_2026

# chat agent over imported rules
python branch_a_agent.py --plan uc_ship_2026 --rules /tmp/uc_ship_2026/benefit_rules.json
```

To use different data, set `BRANCH_A_RATES_CSV`, `BRANCH_A_ENCOUNTERS_CSV`, `BRANCH_A_PLAN_PROFILES`,
`BRANCH_A_RATES_SQLITE` (needed for NPI lookups) or `BRANCH_A_OVERLAY_DATA`.

A plan prices nothing until a person sets `review_status: "verified"` and `reviewed_by` on its entries in
`overlay/data/policy_mrf_map.v1.json` and `service_rate_map.v1.json`. Until then the resolver returns
`mapping_pending_review`.
