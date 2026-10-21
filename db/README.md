# Databases (SQLite, read-only)

| database | file | size | where it lives in production |
|---|---|---|---|
| ① policy (`plans`, `benefits`, `plan_networks`) | `data/plans/policy.sqlite` | ~100 KB | committed, deployed with the code |
| ② allowed amounts (MRF rates) | `data/mrf/rates.sqlite` | 3.1 GB | Railway volume, `/data/rates.sqlite`; not read at runtime yet |

Schemas: `schema.sql` (policy) and `scripts/mrf/mrf_rate_store.py` (rates). The agent prices visits from the
summary `data/mrf/allowed_amounts.csv` (built from ② by `scripts/mrf/report.py`); ② itself is kept for
per-provider (NPI) pricing later. Python joins the two through
`plan_networks.mrf_plan_key` → `mrf_sources.plan_key` (`ATTACH` works for ad-hoc SQL).

## Policy database: data flow

```
plan SBC PDF ──scripts/policy/extract_sbc.py (LLM) or the Perplexity prompt──>
                                    data/plans/extracted/<plan_id>/{plans,benefits}.csv   (raw, kept as extracted)
                                    data/plans/sources.csv        (verified_by set → the whole plan is verified)
                                    data/plans/plan_networks.csv  (bridge to the MRF rates)
                                            ▼
                     python scripts/policy/build_seed.py
                                            │  validates, normalizes, adds category / review_flags / calculable
                                            ▼
                               data/plans/policy.sqlite   (rebuilt from scratch; commit it)
```

`extract_sbc.py` finds an SBC's pages itself (page 1 "Important Questions" + the "What You Will Pay" table pages;
an EOC needs `--summary-pages`/`--pages`) and sends each page's image and text layer to the model with thinking
off (`LLM_EXTRA_BODY`); a plan takes about 3 minutes. Python then splits nothing and computes nothing: it only fills plan_id /
source_url / source_page, unifies tier spellings ("UC Family Provider" → "UC Family") and drops rows repeated
across pages.

`data/plans/sources.csv` lists every source document (URL, SHA-256, who extracted it, when, who verified it). Team
decision 2026-10-17: every extraction CSV the team hands over counts as verified, recorded as `verified_by`; a single
row can still opt out with `verified=no`. The PDFs themselves stay out of git (`data/policy_pdfs/`).

## Benefit rows

One row = one price: plan × SBC section × service × component × setting × network × tier.

| column | meaning |
|---|---|
| `category` | grouping from the SBC "Common Medical Event": `office_visits`, `tests`, `drugs`, `outpatient_surgery`, `emergency`, `hospital_stay`, `mental_health`, `pregnancy`, `recovery`, `pediatric`, `plan_level`, `excluded`, `other` |
| `service` | the calculator's service key (12 services, `plan_level`, or `other`) |
| `network` / `network_tier` | `in`/`out`; tier as printed (e.g. `Ashe Center`, `UC Family`, `Network`) |
| `review_flags` | comma-separated reasons the calculator must not use the row as-is: `unverified`, `coverage_unknown`, `missing_cost`, `missing_deductible`, `cap`, `penalty`, `balance_billing` |
| `calculable` | 1 when verified and no flags. The agent only computes costs from these rows. |

`plan_level` rows hold each tier's individual deductible and out-of-pocket limit in `copay_usd`.

## The bridge: `plan_networks`

Benefits say what share the patient pays; MRF rates say what the provider is paid. `plan_networks` says which MRF
rates (`mrf_sources.plan_key` + `network`) price each in-network tier of a plan. A tier with no row (a campus clinic
such as Ashe Center, or out-of-network) has no allowed amount, so only flat copays can be computed there.
Rows for plans that are not extracted yet wait in `data/plans/plan_networks.csv`; their `plan_id` is the one to use
when extracting that plan.

## Rates database on Railway

The service has a volume mounted at `/data` (Hobby plan or above: the free/trial volume is 0.5 GB). Upload once,
outside git:

```bash
railway volume files upload ~/Downloads/rates.sqlite.xz /rates.sqlite.xz
railway ssh -- python -c "import lzma,shutil; shutil.copyfileobj(lzma.open('/data/rates.sqlite.xz'), open('/data/rates.sqlite','wb'))"
```

The volume survives redeploys. Locally, unpack the same archive to `data/mrf/rates.sqlite` (gitignored).
