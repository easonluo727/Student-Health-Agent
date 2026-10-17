# Matching pipeline: design, current results, failures, and open work

Run reviewed: Prime 500, `prime_500_2026`, on 2026-10-17. The complete machine-readable run record and the generated mapping documents are in [`run-evidence/prime_500_2026/`](run-evidence/prime_500_2026/).

## What the system is intended to do

The input policy extraction produced 86 benefit rules: 43 service-and-setting pairs, each represented once for in-network and once for out-of-network. The automatic mapper groups by service and setting, so it processes 43 mapping candidates rather than 86. The current automatic price path is in-network only; out-of-network MRF lookup remains blocked by design.

The mapper then:

1. Reads the extracted policy metadata and matches the named payer/network to a plan profile. The run selected `uhc_ca_choice_plus` / `Choice-Plus`; the separate plan/network review approved it.
2. Builds a rate-available MRF candidate catalog for that payer/network from `encounters.csv` and `allowed_amounts.csv`.
3. Sends the 43 service candidates to the AMD model in batches of five (nine batches total). All 43 came back; none were omitted.
4. Lets the AMD model propose semantic matches, including multiple options and a clarification question when one policy benefit could mean several concrete services. A second semantic/adversarial veto is intentionally skipped.
5. Validates proposed encounter/component IDs, CPT/HCPCS codes, billing class, place of service, insurer/network, and usable positive rates in Python. Low-confidence or empty proposals are blocked.
6. Runs a deterministic lookup test for every accepted option and writes the mappings, run audit, and stage log.

Code and test snapshots are under [`overlay/`](overlay/); the copied integration overview is [`README.md`](README.md). The canonical working files remain under `branch-a-mrf-integration/overlay/` in this repository; this directory is a collected snapshot, not a move of those source files.

## Result of this run

- 11 of 43 service/setting mappings were accepted; 32 were blocked.
- 20 lookups ran for the accepted options. All returned `network_estimate`, not a selected-provider/NPI rate.
- The plan/network stage passed. The service stage was only partially successful; this is not an end-to-end success for all policy benefits.
- Accepted examples include outpatient physician visits, PT evaluation, chest X-ray, routine labs/STI screening, lumbar MRI, flu shot, therapy visits, preventive visit, and an urgent-care flu encounter. These are the specific catalog options listed in the generated `service_rate_map.v1.json`, not comprehensive coverage of each broad policy benefit.

## Code test status for this collected snapshot

- Focused automatic-mapping and policy-import tests: **20 passed**.
- Full copied overlay suite: **93 tests discovered/run, 4 skipped**; it stops on the nested original-repository tests because the active Python environment does not have `ijson` installed (`ModuleNotFoundError`). This is an environment dependency issue; it does not invalidate the 20 focused test results. Install the repository requirements and rerun the full suite before treating the whole repo as green.

## What the run actually found in the local rate files

The repository currently has `data/mrf/allowed_amounts.csv`, `billing_codes.csv`, and `encounters.csv`. I found no `rates.sqlite` or other `.db`/`.sqlite` file anywhere inside the local repository, so these findings are about the checked-in CSV view—not proof that an external or separately stored full database has no other rates.

For UnitedHealthcare / Choice-Plus, the CSV contains 376 usable rows (358 B-component rows and 18 A-encounter rows), covering 41 distinct CPT/HCPCS codes. The encounter catalog has 19 encounter groups. Fifteen codes with rates are present in `billing_codes.csv` and the allowed-amount CSV but are not referenced by `encounters.csv`, so the automatic mapper does not expose them to the LLM as choices. Examples include:

- `99281`, `99282`, `99283`, and `99285` — emergency-department professional E/M codes. `99284` is also rateable and is present in the catalog, but only inside the ankle-sprain/X-ray encounter.
- `97110` — physical-therapy therapeutic exercise; only the separate evaluation code `97161` is currently in the encounter catalog.
- `90715` — Tdap vaccine; the catalog has a flu-shot option (`90686`) but not this code.
- `99204` and `98006` — moderate-complexity new-patient and telehealth options absent from the encounter definitions.
- `70450` (CT head), `73600` (ankle X-ray), `87502` (influenza PCR), `29515`/`29540` (splint/strapping), `11981` (contraceptive implant insertion), and `J7300` (copper IUD device).

The cause is visible in [`auto_map_policy.py`](overlay/workflow/auto_map_policy.py): the candidate list is assembled from encounter groups and only their listed codes. The model cannot choose a priced code that never enters that candidate catalog. Therefore, the run's statement “no suitable option in the provided catalog” must not be interpreted as a full-database proof of no price.

## All 32 blocked examples from this run

Each item below was returned by the AMD model with `confidence: low` and no usable option; Python then blocked it at the usable-confidence threshold. These are the model's recorded explanations, not independent proof that the full MRF source has no such code.

### Inpatient (10)

1. **Room and board expense** — no inpatient room/board or facility-stay rate in the supplied catalog.
2. **Intensive care** — no ICU/critical-care rate in the supplied catalog.
3. **Hospital miscellaneous expenses** — no general hospital miscellaneous/facility ancillary rate.
4. **Routine newborn care** — no newborn/neonatal inpatient service.
5. **Surgery** — no inpatient surgical-procedure rate.
6. **Assistant surgeon fees** — no surgical-assistant or operative professional service.
7. **Anesthetist services** — no anesthesia service.
8. **Registered nurse's services** — no nursing-specific professional service.
9. **Physician's visits** — supplied physician options are office/telehealth or emergency-department services, not inpatient hospital visits.
10. **Pre-admission testing** — individual tests exist, but no pre-admission bundle or inpatient pre-admission encounter.

### Outpatient (8)

11. **Surgery** — no general outpatient surgical procedure/encounter in the supplied catalog.
12. **Day surgery miscellaneous** — no miscellaneous day-surgery code/encounter.
13. **Assistant surgeon fees** — no assistant-surgeon professional fee code in the supplied catalog.
14. **Anesthetist services** — no anesthesia service code in the supplied catalog.
15. **Medical emergency expenses** — the model declined to use a specific ankle-sprain/flu encounter as a generic emergency visit. ED professional CPTs are present in the rate CSV, but no complete ED aggregate/facility price was found; severity and full service scope are unresolved.
16. **Radiation therapy** — no radiation-therapy service in the supplied catalog.
17. **Chemotherapy** — no chemotherapy/oncology infusion service in the supplied catalog.
18. **Prescription drugs** — medical procedure/visit rates do not provide pharmacy benefit/formulary prices.

### Other (14)

19. **Ambulance services** — no emergency-transport code/service in the supplied catalog.
20. **Durable medical equipment** — no DME/medical-supply code/service in the supplied catalog.
21. **Dental treatment** — no dental code/service in the supplied catalog.
22. **Maternity** — no maternity-specific or general obstetric service in the supplied catalog.
23. **Complications of pregnancy** — no related service in the supplied catalog.
24. **Elective abortion** — no abortion-related procedure/service in the supplied catalog.
25. **Reconstructive breast surgery** — no reconstructive/surgical breast service in the supplied catalog.
26. **Diabetes services** — no diabetes-specific encounter/component; generic labs or an office visit are not equivalent without knowing the requested service.
27. **High cost procedure** — this is a broad financial category, not a specific procedure code; a concrete procedure is required.
28. **Transplantation services** — no transplantation-related service/component in the supplied catalog.
29. **Pediatric dental and vision services** — no dental/vision service in the supplied catalog.
30. **Bariatric surgery** — no bariatric/surgical service in the supplied catalog.
31. **Titers** — routine labs/STI screening are not the listed immunization antibody titers; specific titer codes were not in the supplied catalog.
32. **Tuberculosis screening and testing** — no TB-specific test code/service in the supplied catalog.

## Failure modes and unresolved risks

1. **Candidate catalog is narrower than the priced CPT list.** Connect all rate-available CPT/HCPCS rows (and their code descriptions, billing class, POS, and modifiers) to the semantic candidate stage, rather than requiring every useful code to already belong to a hand-curated encounter.
2. **Some policy benefits are categories, not single billable services.** “Medical emergency,” “tests and procedures,” “diabetes services,” and “high cost procedure” need concrete service choices. Ask the user which service they mean; do not silently assign one representative CPT as the full benefit.
3. **ED is only partially priced.** The local data has physician E/M rates for CPT 99281–99285 at POS 23, but the reviewed run did not find a complete ER A-encounter total or the facility component. A physician-only amount must be explicitly labeled partial, never presented as the full ED allowed amount.
4. **Existing accepted mappings can be underinclusive.** For example, PT maps to evaluation but omits available treatment code 97110; injection maps to flu vaccine only despite Tdap 90715 being rateable; outpatient physician choices omit 99204/98006. Accepted means the proposed options passed current checks—not that every clinically relevant code was discovered.
5. **The 20 test results are network estimates.** They do not prove an exact selected-provider NPI price. Provider-specific matching requires the local SQLite/provider-level tables and an NPI with a matching rate; that database was absent from this repo checkout during this inspection.
6. **Some benefit types may need another source.** Pharmacy, dental, vision, ambulance, DME, facility, and specialty services may be outside this curated MRF/encounter sample. Do not invent a rate or cross payer/network; report unavailable or add an explicitly labeled external estimation source.
7. **Mapping validation is not policy interpretation validation.** Python confirms references and rate eligibility, but it does not prove that an example CPT captures every limitation, unit, visit cap, admission condition, or coverage exception in the policy text.

## Recommended next work

1. Expand candidate generation from the rateable CPT/HCPCS rows, not only the 19 encounter definitions; retain exact insurer/network, billing-class, POS, modifier, and date checks.
2. Represent A-encounter totals separately from B-component/CPT prices. For partial ED/other component-only options, expose that scope in the result and do not call it a full encounter total.
3. For broad benefits, have the agent ask a service-specific clarification and show only supported choices. Keep unsupported items blocked with their reason.
4. Add tests for catalog coverage: every rateable code is either surfaced as a candidate or explicitly recorded as intentionally excluded with a reason.
5. Re-run the mapper and review its evidence. Only then decide whether the 32 blocked cases are truly unsupported or newly mappable.
