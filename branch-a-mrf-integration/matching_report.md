# A/B and NPI rate matching: implementation report

This report covers the spec `BRANCH_A_B_MRF_DATABASE_MATCHING_IMPLEMENTATION`: canonical field adapter,
A_encounter and B_component lookups, provider NPI lookup with a nearby Top-20 fallback, structured output and
JSONL logs. Every number below was measured on the local database on 2026-10-15. Nothing here was taken from
documentation or from the schema alone.

## 1. Database actually used

| | |
|---|---|
| Path | `data/mrf/rates.sqlite` (gitignored, local only; referenced by path, never copied) |
| Size | 3,145,469,952 bytes (3.1 GB); modified 2026-10-14 21:16 UTC |
| Engine | SQLite 3.45.1, `user_version` 0, opened read-only (`mode=ro`) |
| Snapshot dates | Anthem, Cigna and UHC 2026-10-01; Health Net 2026-09-28; L.A. Care 2026-09-15 |
| Full inspection | [`reports/db_inspection.json`](reports/db_inspection.json), reproducible with `overlay/workflow/inspect_database.py --distinct` |

| Table | Rows | Role |
|---|---:|---|
| `mrf_sources` | 316 (all `complete`) | One MRF file per row: `insurer`, `plan_key`, `network`, `file_date`, `source_url` |
| `negotiated_rates` | 629,842 | Raw rates: code, code type, `billing_class`, `service_codes` (POS), `modifiers`, `negotiated_rate`, `negotiated_type`, `expiration_date`, `provider_group_id` |
| `provider_members` | 14,446,074 | Provider group → `npi`, `tin_sha256`, `zip`, `taxonomy`, `tier`, `ucla_health` |
| `ucla_health_tins` | 147 | UCLA organization TIN hashes |
| `rate_summary` | 9,870 | Per-tier summary (not used by the lookup) |

The indexes used by the lookup are `ix_members_npi(npi)`, `ix_members_group(source_id, provider_group_id)` and
`ix_rates_code(source_id, billing_code, billing_class)`. The A and B aggregate tables live in
`data/mrf/allowed_amounts.csv` (133 A rows and 3,290 B rows). They hold `status`, `allowed_amount_basis`,
`n_providers`, `p10` and `p90`, but no `encounter_key`, no `source_id` and no per-rate expiration date.

### NPI rows confirmed by real joins

Each plan was checked both ways: a sampled rate row was followed through its provider group to its member
NPIs, and one of those NPIs was followed back through its groups to rate rows. The check passed for all five
sample codes (99213, 99203, 80053, 73610, 99284) in every plan.

| plan_key | Sources | Rate rows | Distinct NPIs | NPI→rate joins |
|---|---:|---:|---:|---|
| anthem_ca_pathway_epo | 5 | 27,966 | 47,119 | 5/5 |
| anthem_ca_pathway_hmo | 5 | 28,863 | 47,190 | 5/5 |
| anthem_ca_prudent_buyer_ppo | 4 | 66,329 | 53,280 | 5/5 |
| cigna_national_ppo | 1 | 302,891 | 36,722 | 5/5 |
| healthnet_ambetter_ca | 1 | 7,435 | 25,498 | 5/5 |
| lacare_covered | 299 | 5,886 | 13,368 | 5/5 |
| uhc_ca_choice_plus | 1 | 190,472 | 17,826 | 5/5 |

Every member row has an NPI, a ZIP and a taxonomy (0 nulls in each). The members are only NPIs in the
Los Angeles NPPES set: 905,182 tier A rows and 13,540,892 tier B rows.

## 2. Aliased and transformed columns

The full field-by-field table, with source table, accepted aliases, the resolved column and the transform, is
`canonical_adapter.FIELD_MAP`. `mapping_report()` prints it, and `reports/db_inspection.json` holds it as
`canonical_mapping`.

| Canonical field | Source | Transform |
|---|---|---|
| `provider_npi` | `provider_members.npi` (also accepts `NPI` and `provider_npi`) | identity; a provider id, never a plan id |
| `place_of_service` | `negotiated_rates.service_codes` | comma list → set. Empty = none listed. `CSTM-00` = all POS. Encounter POS `20\|11` becomes an ordered preference tuple |
| `billing_class` | `negotiated_rates.billing_class`, CSV `billing_class` | enum: `prof`→`professional`, `inst`/`facility`→`institutional`, `both` |
| `source_rate_amount` / `source_rate_type` | `negotiated_rate` / `negotiated_type` | one raw contract rate. Never renamed to `allowed_amount` |
| `allowed_amount` | CSV `allowed_amount` | the pipeline's selected price. Placeholder values ≥ 1e9 are rejected |
| `modifiers` vs `modifier_rule` | `negotiated_rates.modifiers` vs `encounters.csv modifier_rule` | Kept separate. The B CSV has no `modifier_rule`, so it is re-derived with `report.component_combos`' rule and labelled `derived` |
| `expiration_date` vs `mrf_file_date` | `negotiated_rates.expiration_date` vs `mrf_sources.file_date` | Kept separate. `9999-12-31` = open-ended |
| `encounter_key` | CSV `encounter`, which is a display label | unique label → key through `encounters.csv`. A missing label gives `encounter_label_unknown`; a duplicated one gives `encounter_key_ambiguous` |
| `plan_key`, `network` | CSV `insurer` + `network` label | resolved through `plan_profiles`, never by renaming the insurer. Unresolved gives `plan_key_unresolved` |
| `billing_code_type` | `billing_codes.csv code_type` | catalog lookup. Never inferred from the digits |

The original values are kept on every canonical row under `raw`. Raw TINs are never read; the adapter uses only
`tin_sha256`.

## 3. End-to-end samples (real database, Anthem Prudent Buyer PPO, service date 2026-10-20)

NPIs are masked in this report.

| Case | Status | Amount | Details | Time |
|---|---|---:|---|---:|
| A `urgent_care_flu` | `network_estimate` | $384.70 | basis `ucla_health_rate`, n=2,900, p10 $235.67, p90 $384.70, snapshot 2026-10-01 | 0.07 s |
| B 99203 professional POS 11 | `network_estimate` | $465.29 | basis `ucla_health_rate`, n=2,902, p10 $287.66, p90 $465.29 | <0.01 s |
| NPI 144…72, 99213 POS 11 | `provider_rate` | $220.18 | exact rows from 4 sources, provider-specific | 0.09 s |
| NPI 144…72, `urgent_care_flu` | `provider_rate` | $259.95 | visit $220.18 + flu test $39.77, both exact | 0.06 s |
| NPI 101…04 (Westwood family medicine, not in any PPO group), 99214 POS 11 | `nearby_provider_estimate` | $315.08 | exact reason `no_provider_npi_rows`. Candidates narrowed in this order: 1,861 rate rows → 1,303 after the POS rule → 41,663 member NPIs → 33,179 within 25 mi → Top 20 (scores 0.913–0.956). p10 $110.73, p90 $468.19 | 3.2 s |
| Wrong network (`CA EPO Pathway` under the PPO plan_key) | `rate_unavailable` | null | `network_not_resolved` | <0.01 s |
| L.A. Care (no network filter), 80053 POS 81 | `network_estimate` | $13.78 | p90 was the placeholder 99999999999999, so it is dropped and a warning is added | <0.01 s |

Before this change, a nearby lookup expanded all 803,824 rate × member rows for Anthem 99213 and took 56 s. It
now applies the hard rules to roughly 2,000 rate rows first, expands only the surviving groups, and computes one
representative amount per distinct set of groups. The same lookup now takes 3.2 s.

## 4. Behaviour by spec section

| Spec | Where | Notes |
|---|---|---|
| §1 inspect before use | `inspect_database.py` | Read-only. Reports tables, columns, indexes, counts, the column binding and two-way NPI join checks |
| §2 input contract | `schemas/rate_request.v1` | Exactly one of `encounter_key` or `billing_code`. `network` must be an exact profile network name, or null for plans with no network filter |
| §3 canonical views | `canonical_adapter.py` | `ProviderRates.rows()` returns `canonical_provider_rates`. `canonical_csv_views()` returns A and B. `canonical_encounter_components()` |
| §4 A_encounter | `RateLookup.encounter` | Key: plan_key + exact network + encounter_key. The A total is never added to B rows. With an NPI, the total is the sum of all components, and any missing required component means no total |
| §5 B_component | `RateLookup.component` | Key: plan_key + exact network + code + class + ordered POS. `modifier_rule` is checked against the derived rule. An empty POS on a code priced by POS gives `pos_missing` |
| §6 NPI and Top-20 | `_provider_component`, `_nearby` | See section 5 below |
| §7 dates and semantics | `hard_filter`, `_from_aggregate` | `expiration_date ≥ service_date` is checked on raw rates. Aggregates set `service_date_validated=false` with a warning. Snapshots older than 62 days, or later than as_of, are stale |
| §8 output | `schemas/rate_lookup_result.v1` | Status is one of `provider_rate`, `nearby_provider_estimate`, `network_estimate`, `rate_unavailable`, plus a `reason_code`. A null amount when unavailable is enforced by the schema |
| §9 logs | `trace_log.py` | JSONL with `run_id`, `request_id`, stage, status, adapter and lookup versions, and the DB snapshot. Records where the candidate count reached zero. Logs stack traces on exceptions. A failed write shows in `log_status` and in `warnings` |
| §10 tests | `overlay/tests/test_canonical_lookup.py` and `test_real_database.py` | Tests for acceptance items 1–11, plus real-database smoke tests (skipped when the database is absent) |

## 5. Nearby fallback, exactly as implemented

The fallback runs only when the user gave an NPI and that NPI has no exact rate after the hard rules.

**Hard rules.** These exclude candidates; they never lower a score. They are applied in this order, and each
stage's count is logged:

1. same plan_key (and so the same network), enforced by the query;
2. exact code, enforced by the query;
3. `billing_class` matches, or the rate's class is `both`;
4. snapshot no older than 62 days and not in the future;
5. dollar `negotiated_type` (not percentage or per diem);
6. amount > 0;
7. amount below the 1e9 placeholder threshold;
8. not expired on the service date;
9. POS applicable: a requested POS is listed, or the rate lists none, or it lists `CSTM-00`;
10. modifiers allowed by the `modifier_rule`;
11. the NPI is a member of a surviving group, and is not the selected NPI;
12. the NPI's ZIP is known in the Census ZCTA file;
13. haversine distance between ZCTA internal points is at most 25 miles.

**Representative amount.** Each provider gets one amount from `scripts/mrf/allowed.per_provider`: modifier rule,
POS preference, latest expiration, then the median across all of that NPI's groups and TINs. Its trace ids are
kept.

**Score.** Each sub-score is 0–1. The weights are a heuristic, not a probability.

| Sub-score | Weight | Rule |
|---|---:|---|
| distance | 0.35 | 1 − d/25 |
| POS | 0.25 | 1.0 for the first requested POS; 0.8 for a later preference; 0.6 for an all-POS or unlisted rate |
| taxonomy | 0.15 | 1.0 for the same NPPES taxonomy as the selected NPI; 0.6 for the same entity type; 0.3 otherwise; 0.5 if the anchor taxonomy is unknown |
| freshness | 0.15 | 1 − age/62 days |
| source quality | 0.10 | negotiated 1.0, fee schedule 0.9, derived 0.8 |

**Selection.** Candidates are sorted by score, then distance, then NPI. At most 3 NPIs per hashed TIN are kept,
up to 20 distinct NPIs in total.

**Result.** The unweighted median of the representative amounts, with p10, p90 and n. Fewer than 5 providers
adds a low-sample warning. Zero providers gives `no_eligible_fallback_provider` and a null amount, and the log
records the stage where the count reached zero. The anchor location comes from the NPPES directory
(`la_npis.csv`), or else from the NPI's own MRF membership.

## 6. Conditions not covered

1. **Placeholder rates in the PR #4 data.** L.A. Care publishes `99999999999999` as a "rate": 992 rows over 39
   codes, 351 of them labelled `fee schedule` or `derived`. `scripts/mrf/allowed.py` does not exclude it. As a
   result, 8 rows of `allowed_amounts.csv` have a placeholder `allowed_amount`, and 64 rows have one in the
   amount, p10, p90 or UCLA column. Medians in other L.A. Care cells may also be pulled up.
   - This overlay refuses those values.
   - The proper fix is a one-line exclusion in `allowed.py` (PR #4), followed by regenerating the report. It was
     not made here, because the overlay may not edit `scripts/`.
2. **No per-rate validity in aggregates.** The CSV has no expiration date, so `network_estimate` cannot be
   checked against a future service date. Only the raw SQLite path checks validity.
3. **No effective (start) dates.** MRFs publish only `expiration_date`, so a rate that starts after the
   snapshot cannot be detected.
4. **Aggregates have no source ids.** CSV rows carry only `mrf_file_date`, so `source_ids` is empty for
   `network_estimate` results.
5. **Location is approximate.**
   - It uses the NPPES primary practice ZIP and Census ZCTA internal points, with a fixed 25-mile radius.
   - The user cannot give their own location; the anchor is always the selected provider.
   - NPIs outside California ZCTAs, or with an unknown ZIP, are excluded.
6. **Facility vs. professional.** An institutional component is never assumed to be billed by the user's
   individual NPI. Its exact lookup fails, and it falls back to nearby facilities. The result is labelled
   `mixed_provider_and_nearby`. No hospital NPI is inferred.
7. **Code alternatives.** For an NPI lookup, alternatives such as spirometry 94010|94060 are tried in mapping
   order, and the choice is recorded. The aggregate B path needs a single code (`code_alternatives_need_provider`).
8. **Real plans still do not price through the agent.** `policy_mrf_map` and `service_rate_map` stay
   `pending_review` until a person verifies them. `lookup_cli.py` is the direct entry point for an
   already-normalized request.
9. **Deployment.** The 3.1 GB SQLite database is not deployable to Vercel. NPI lookups and nearby estimates work
   only where the database is present. A trimmed provider table is future work.
10. **Log retention.** `trace_log.prune()` deletes logs older than 30 days, but nothing schedules it.
