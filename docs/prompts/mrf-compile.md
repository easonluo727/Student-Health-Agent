# Task: compile a Los Angeles negotiated-rate database from insurer MRFs

> 用法：把这整份文件交给 Claude Code / Codex 作为任务说明。它会分阶段执行，并且在每个阶段结束时停下来汇报，等你确认后再继续。

## Context

You are working in the `amd-health-agent` repository. Read `AGENTS.md`, `PRODUCT.md`, and `docs/technical-plan.md` first.

The product estimates what a US college student will pay for care under a health plan. Out-of-pocket cost is computed by Python as **plan cost-sharing rules × allowed amount**. Cost-sharing rules come from plan PDFs and are out of scope for this task. **This task builds the allowed-amount database**: negotiated rates that insurers publish under the federal Transparency in Coverage (TiC) rule, as In-Network Rate machine-readable files (MRFs).

This is a **traditional data-engineering pipeline. Use no LLM calls inside the pipeline.** The pipeline is a showcase for the README, so it must be reproducible, measured, and well documented.

## Goal

Produce a small SQLite database of real negotiated rates for:
- **Insurers / networks:** see "Scope" below. One California commercial PPO network per insurer, plus HMO or EPO where it is the main California product.
- **Providers:** located in Los Angeles. Start with the UCLA / Westwood area, then all of LA County.
- **Services:** a fixed list of ~15–20 billing codes (see "Billing codes").

Also write a README section that explains the pipeline and reports measured numbers.

## Scope

| Priority | Insurer | Notes |
| --- | --- | --- |
| 1 (required) | **Anthem Blue Cross (California)**, parent Elevance Health | UC SHIP (the UC student plan) uses the Anthem Blue Cross Prudent Buyer PPO network. That network's rates are the demo's prices. Known table-of-contents pattern (verify it is current): `https://antm-pt-prod-dataz-nogbd-nophi-us-east1.s3.amazonaws.com/anthem/YYYY-MM-01_anthem_index.json.gz`. It is about 10 GB gzipped and written as one JSON line. |
| 2 | **Blue Shield of California** | A different company from Anthem Blue Cross. |
| 3 | **UnitedHealthcare** | National. |
| 4 | **Aetna (CVS Health)** | National. |
| 5 | **Cigna** | National. |

Excluded: Kaiser Permanente (integrated delivery, so its rates are not comparable) and Humana (largely out of commercial medical).

Find each insurer's **official** TiC / MRF landing page yourself and record the URL. Do not use third-party mirrors as the source of record. Mirrors may only be used to understand a file's layout.

If an insurer turns out to be impractical (for example, there is no way to isolate California files and the full set is many TB), stop and report instead of brute-forcing it.

## Billing codes (candidates — a human must verify)

The demo persona is a 26-year-old UCLA PhD student with mild asthma, one flu episode at urgent care, and one ankle sprain at the ER with an X-ray. The candidate codes below are a starting point. **Do not treat them as final.** Put them in `data/mrf/billing_codes.csv` with a `verified_by` column left empty, and ask the human to confirm them before Phase 3.

| Service | Candidate CPT / HCPCS |
| --- | --- |
| Office visit, established patient | 99213, 99214 |
| Office visit, new patient | 99203, 99204 |
| Specialist (pulmonology) visit | same E/M codes; record the provider taxonomy |
| Spirometry | 94010, 94060 |
| Rapid flu test | 87804 |
| Flu PCR | 87502 |
| Emergency department visit | 99283, 99284, 99285 |
| Ankle X-ray | 73600, 73610 |
| Ankle strapping / splint | 29540, 29515 |
| Preventive visit (adult) | 99395 |

Urgent care is usually billed with E/M codes at place of service 20. Keep `billing_class` and `service_code` (place of service) so the calculator can pick the right rate. Prescription drugs are **out of scope**. They are in a different TiC file type.

## Rate selection rules (MRF rows are not final until these are applied)

The same code and provider often carry several rates. Pick and combine them deterministically:

- **`billing_class`:** `professional` (physician fee) and `institutional` (facility fee) are separate charges. When an encounter has both (for example an ER visit), include both.
- **`service_code`:** the place of service. Pick the rate that matches the encounter's setting: office 11, urgent care 20, outpatient hospital 22, ER 23. If no place-of-service-specific rate exists, use the rate with no place of service.
- **Modifiers:** imaging splits into a technical component (`TC`) and a professional component (`26`). If there is no global (unmodified) rate, sum TC + 26.
- **Expiration date:** drop expired rates. If several are current, keep the latest.
- **`negotiated_type`:**
  - `negotiated`, `derived`, `fee schedule`: use the dollar amount.
  - `percentage`: the rate is a percent of billed charges, which we don't have. Exclude it and count it as unavailable.
  - `per diem`: exclude it (inpatient only; out of scope).
- **Encounter templates:** one visit can be several codes. Define them in `data/mrf/encounters.csv`, which a human must verify, for example:
  - `urgent_care_flu` = office E/M at POS 20 (professional) + rapid flu test
  - `er_ankle_xray` = ER E/M (professional) + ER E/M (institutional) + ankle X-ray (TC + 26 or global)
  - `specialist_followup` = established E/M (professional, POS 11)
  - `spirometry` = 94010 or 94060
  
  An encounter's allowed amount is the sum of its components.

## The single `allowed_amount` value (required)

Every output row must have exactly one dollar number in a column literally named **`allowed_amount`**. Compute it with this fixed rule, in priority order, and record which rule was used in `allowed_amount_basis`:

1. **`ucla_health_rate`:** a rate for a UCLA Health NPI/TIN (Tier A provider list). If several UCLA providers have rates, take the median of those providers.
2. **`westwood_area_median`:** the median across Tier A providers.
3. **`la_county_median`:** the median across Tier B providers.
4. Otherwise, leave `allowed_amount` empty and set `status = rate_unavailable`.

`status` is `verified_rate` for rule 1, `estimated_rate` for rules 2–3, and `rate_unavailable` for rule 4. The calculator only ever reads `allowed_amount`. Every other column is supporting detail.

## Deliverables (what the human receives)

1. **`data/mrf/allowed_amounts.md`**, a polished Markdown report:
   - A header summary covering insurers covered, MRF month, data scanned (GB), rows scanned and kept, runtime, and coverage (% of encounter × insurer cells with an `allowed_amount`).
   - **Table A — Encounter allowed amounts (the demo uses this).** One row per insurer × encounter. Columns, in this order:
     `insurer` | `network` | `encounter` (plain English) | **`allowed_amount`** | `allowed_amount_basis` | `status` | `components` (codes + billing class summed) | `n_providers` | `p10` | `p90` | `ucla_health_rate` | `mrf_file_date`
   - **Table B — Component allowed amounts.** One row per insurer × billing code × billing class × place of service, with the same `allowed_amount` / basis / status columns plus `negotiated_type_mix` and `n_rates_excluded` (percentage, per diem, or expired).
   - **Table C — Insurer comparison.** Encounters as rows, insurers as columns, and each cell showing only `allowed_amount`, with the cheapest cell in each row in bold.
   - **Data notes:** exclusions, gaps, and the fact that negotiated rate ≠ final allowed amount.
2. **`data/mrf/allowed_amounts.csv`:** Tables A and B in machine-readable form, same columns.
3. **`data/mrf/rates.sqlite`:** full detail behind the report.
4. **`scripts/mrf/lookup.py`:** returns the `allowed_amount` for (insurer, network, encounter or code, optional NPI).

The final deliverables must be small (a few MB). Raw data is streamed and discarded. **Never store more than 200 GB at any point.** The Claude Code cloud VM has only 30 GB of disk, so the full Phase 3 run belongs on a server.

## Constraints

- **Stream everything.** Never download a whole MRF to disk and never load one into memory. Pipe HTTP → gzip decompression → incremental JSON parser (for example `ijson` with the C backend), and discard non-matching records on the fly. Many files are a single JSON line many GB long.
- **Handle `provider_references` in all its forms:** inline at the top of the file, after `in_network`, or in separate files referenced by URL. If the references come after `in_network`, use two passes or a temporary on-disk index of provider-group IDs to NPIs/TINs. Never hold a full file in RAM.
- **Run heavy jobs on a server, not in your context window.** Write scripts, launch them as background processes on a machine with good bandwidth (for example a Railway service or a Linux VM), and monitor their log files. Never print raw MRF content into the conversation; print counts and small samples only. This keeps token cost low.
- **Checkpoint and resume.** Each file's processing must be resumable (record completed file URLs and byte offsets or completion markers), so a crash does not restart a multi-hour job.
- **Be polite to the hosts.** Use limited parallelism (for example 2–4 concurrent downloads per insurer), retry with backoff, and set a descriptive User-Agent. If a host rate-limits or blocks you, stop and report. Do not try to evade it.
- **No fabricated data.** Every row must trace to a specific source file URL and file date. If something cannot be obtained, record `rate_unavailable`; never invent a number.
- Python 3.12+. Put the code in `scripts/mrf/`, data in `data/mrf/` (gitignored except small CSVs and the final SQLite if it is under 50 MB), and tests in `tests/`.
- Secrets, if any, come from environment variables. Commit nothing secret.

## Phases (stop and report after each phase)

### Phase 0 — Parser against official samples (local, no big downloads)
1. Get the official example files from the CMS price-transparency guide repository on GitHub (`CMSgov/price-transparency-guide`), covering in-network rates, provider references (inline and external), and the table of contents.
2. Write `scripts/mrf/parse.py`: a streaming parser that yields normalized rows from an in-network file, given a set of billing codes and a set of NPIs. Support gzip and plain JSON, and file paths or URLs.
3. Write `tests/test_mrf_parse.py` (stdlib `unittest`, to match the repo) covering: inline provider references, external provider references, references after `in_network`, multiple `negotiated_prices` per code, the `negotiated_type` variants (negotiated, derived, fee schedule, percentage, per diem), and `billing_code_type` filtering.
4. **Report:** test results, and any schema-version differences you noticed.

### Phase 1 — Discovery per insurer (light downloads only)
For each insurer:
1. Find the official TiC page and the current month's table-of-contents file(s).
2. Stream the table of contents and find the reporting plans and in-network file URLs relevant to **California commercial PPO** (and the main HMO/EPO if applicable). Record plan names, plan IDs/EINs where present, network names, and file URLs in `data/mrf/discovery/<insurer>.json`.
3. Measure the table-of-contents size and parse time.
4. **Report:** a table with, for each insurer, the TOC size, the number of candidate California files, their total compressed size, and how files are split (by state, by plan, by TIN, or other). Recommend which insurers are practical. **Wait for confirmation.**

### Phase 2 — Spike: one real file per insurer
1. Build the LA NPI set from the NPPES bulk file (the public CMS download). Filter by practice-location ZIP codes:
   - Tier A: UCLA / Westwood (90024, 90025, 90095, 90049, 90064, 90067)
   - Tier B: all LA County ZIPs
   
   Save the result as `data/mrf/la_npis.csv` with NPI, name, taxonomy, ZIP, and tier.
2. For each insurer, process **one** California in-network file end to end with the Tier A NPIs and the candidate codes.
3. Measure: compressed bytes read, rows scanned, rows kept, wall time, throughput (MB/s), and peak RAM.
4. **Report** the measurements and an extrapolated total time for all candidate California files. **Wait for confirmation**, including the human-verified billing code list.

### Phase 3 — Full compile (on the server)
1. Run all approved files with checkpointing, using Tier B NPIs.
2. Load the results into `data/mrf/rates.sqlite` with these tables:
   - `sources(source_id, insurer, network_name, plan_ids, file_url, file_date, toc_url, bytes_read, rows_scanned, rows_kept, processed_at)`
   - `rates(insurer, network_name, billing_code, billing_code_type, npi, tin, provider_taxonomy, zip, tier, negotiated_rate, negotiated_type, billing_class, service_code, billing_code_modifier, expiration_date, source_id)`
   - `rate_summary(insurer, network_name, billing_code, billing_class, service_code, tier, n_providers, p10, median, p90, min, max)`: a view or table used for regional low / mid / high estimates
3. Add data-quality checks: no negative rates; flag rates far outside the 1st–99th percentile per code; flag percentage-type rates (they need a base amount and are not dollar amounts); report the share of codes with zero matches.
4. **Report** the final counts per insurer and code, and any gaps.

### Phase 4 — Integration hook and README
1. Write `scripts/mrf/lookup.py` with this function: `lookup_allowed_amount(insurer, network, encounter_or_code, npi=None)`. It returns `{allowed_amount, allowed_amount_basis, status, components, source}`, following "The single `allowed_amount` value" rules above.
2. Generate the deliverables listed under "Deliverables": `allowed_amounts.md`, `allowed_amounts.csv`, and the final SQLite.
3. Add a README section titled "MRF compile pipeline". Include a diagram of the pipeline (TOC → file selection → streaming filter → SQLite → summary), the measured numbers from Phases 2–3 (TB/GB scanned → MB kept, rows scanned/kept, time, throughput), exact reproduction commands, and limitations: negotiated rate ≠ final allowed amount; UC SHIP is self-funded and uses Anthem PPO network rates; this is a monthly snapshot.

## Acceptance criteria
- [ ] `python -m unittest discover tests` passes, including the new MRF tests.
- [ ] Anthem Blue Cross California PPO rates exist for every human-verified billing code in Tier A, or each missing code is explained.
- [ ] Every row of Tables A and B has a single dollar value in the `allowed_amount` column (or `status = rate_unavailable`), with `allowed_amount_basis` filled in.
- [ ] `allowed_amounts.md` renders cleanly on GitHub, and Table C lets a reader compare insurers at a glance.
- [ ] Every row in `rates` traces to a `sources` row with a real URL and file date.
- [ ] Processing is resumable, and the memory footprint stays flat as file size grows (no whole-file loads).
- [ ] The README section reproduces the pipeline end to end and reports real measured numbers.
- [ ] No LLM calls anywhere in the pipeline, and no fabricated values.

## Reporting style
At the end of each phase, give a short report: what was done, the measured numbers, problems found, and a recommendation for the next step. Keep raw data out of the report. If a decision belongs to the human (scope cuts, the billing code list, dropping an insurer), ask; do not decide alone.
