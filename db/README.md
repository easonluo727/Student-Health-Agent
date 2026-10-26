# Databases (SQLite, read-only at runtime)

| database | file | size | where it lives in production |
|---|---|---|---|
| ① policy (`plans`, `benefits`, `plan_networks`) | `data/plans/policy.sqlite` | ~100 KB | committed, deployed with the code |
| ② allowed amounts (MRF rates) | `data/mrf/rates.sqlite` | 3.1 GB | Railway volume, `/data/rates.sqlite` (`RATES_DB`) |
| ③ NPPES provider directory | `data/providers/providers.sqlite` (built offline, not committed) | varies | same Railway volume, `/data/providers.sqlite` (`PROVIDERS_DB`), only after approval |

The directory is an independently built database; it never modifies ① or ②. Its schema, build and validation procedure are in [`scripts/providers/README.md`](../scripts/providers/README.md). The `/providers` screen uses NPPES identity/practice locations and strict NPI-specific rates. A confirmed plan-to-network bridge does not verify a provider's current participation, and a negotiated rate is not a guaranteed paid amount.

Schemas: `schema.sql` (policy) and `scripts/mrf/mrf_rate_store.py` (rates). The agent prices each visit component
from ② at runtime (`workflow/plan_db.py` → `scripts/mrf/allowed.py`). Python joins the two through
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
rates (`mrf_sources.plan_key` + `network`) price each in-network tier of a plan. Each row records its evidence in
`notes` and `confirmed` (yes = a document or the insurer's TOC names this plan's files and network; for example an
HIOS or EIN plan ID in the TOC). Rows that are not confirmed are never used for prices.
`scripts/mrf/toc.py --plans-out` lists every individual-market plan in an insurer's TOC with its files. A tier with no row (a campus clinic
such as Ashe Center, or out-of-network) has no allowed amount, so only flat copays can be computed there.
Rows for plans that are not extracted yet wait in `data/plans/plan_networks.csv`; their `plan_id` is the one to use
when extracting that plan.

## Adding a plan

Every step leaves evidence a person can check. A coding assistant may run the scripts but never types plan data.

1. **Get the documents.** Download the SBC (and the EOC when the SBC leaves something out) from the insurer or
   Covered California. Add a row to `data/plans/sources.csv`: URL, SHA-256, download date. PDFs stay in
   `data/policy_pdfs/` (not in git).
2. **Extract the benefits (Branch A).** `python scripts/policy/extract_sbc.py <pdf> <plan_id>` writes
   `data/plans/extracted/<plan_id>/{plans,benefits}.csv`. A person checks them against the PDF and sets
   `verified_by` in `sources.csv`.
3. **Find the plan's official ID.** Individual and Covered California plans have a 14-character HIOS ID
   (e.g. `92815CA001000` for L.A. Care Covered at product level); employer plans use the employer's EIN
   (e.g. `943067788` for UC SHIP). It is rarely printed on the SBC; the insurer's TOC lists it with the plan name.
4. **Find its price files and network (the bridge).** Stream the insurer's TOC and list its individual plans:
   `python scripts/mrf/toc.py <toc_url> --match '/CA_' --plans-out data/mrf/discovery/<insurer>_ca_individual_plans.csv`
   (Anthem: 11.4 GB, about 30 minutes at 6.5 MB/s). Find the plan by ID or name; its row lists the in-network
   files. The network name comes from those files' `provider_references[].network_name`
   (`scripts/mrf/probe_networks.py`). If several products share the files and the TOC can't tell which network
   is yours, the link is not confirmed: check the insurer's provider directory or the EOC instead.
   Lessons from Anthem (2026-10-20, `data/mrf/discovery/anthem_*`): its TOC attaches every product name to every
   plan ID, so match by ID only; and its individual files hold about 40 networks (five EPO), so the TOC confirmed
   the Silver 70 EPO's files but not its network. Gold 80 HMO's ID was not in the TOC at all. Both networks were
   then confirmed from outside sources, recorded in `notes`: a provider's accepted-plan list (Sutter Health) and the
   insurer's own provider news. Prefer the insurer's documents; a provider's list is acceptable when it names the
   plan family and the network exactly.
5. **Make sure the rate store has that network (Branch B).**
   - Already in `rates.sqlite` (see `mrf_sources.plan_key`): nothing to do.
   - Not there: add a profile to `data/plan_profiles.v1.json`, run the MRF pipeline on those files
     (`scripts/mrf/README.md`; large files need the server, `scripts/mrf/server/README.md`), then upload the new
     `rates.sqlite` to the Railway volume (below).
6. **Record the link.** Add a row to `data/plans/plan_networks.csv`: `plan_id`, `network_tier`, `mrf_plan_key`,
   `mrf_network`, `confirmed` (`yes` only when the evidence names the files and network) and `notes` (the
   evidence: which document or TOC entry, which ID). Unconfirmed rows are never used for prices.
7. **Build, check, ship.** `python scripts/policy/build_seed.py` rebuilds `policy.sqlite`; run
   `python -m unittest discover tests`; price a few scenarios on the new plan (`PlanDB().estimate(...)`) and
   compare with plans of the same metal tier (Covered California standard designs share copays); commit and deploy.

If the network is already in the rate store (for example another metal tier of Health Net Ambetter), steps 5 and
most of 4 are quick. Steps 3, 4 and 6 could become one script (plan name in, evidence-backed link out).

## Rates database on Railway

The service has a volume mounted at `/data` (Hobby plan or above: the free/trial volume is 0.5 GB). Upload once,
outside git:

```bash
railway volume files upload ~/Downloads/rates.sqlite.xz /rates.sqlite.xz
railway ssh -- python -c "import lzma,shutil; shutil.copyfileobj(lzma.open('/data/rates.sqlite.xz'), open('/data/rates.sqlite','wb'))"
```

The volume survives redeploys. Locally, unpack the same archive to `data/mrf/rates.sqlite` (gitignored).
