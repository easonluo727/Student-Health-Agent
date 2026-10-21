# MRF compile pipeline

Part of [Health Insurance Agent](../../README.md). The full-run runbook is in [`server/README.md`](server/README.md); the rate-selection rules are in the docstring of [`allowed.py`](allowed.py).

Builds the **allowed-amount database** from insurers' Transparency in Coverage in-network rate files (MRFs). It is a traditional data-engineering pipeline written in plain Python, and **it makes no LLM calls**. The cost calculator later multiplies plan cost-sharing by the single `allowed_amount` this pipeline produces.

```text
insurer TOC / listing ──► file selection ──► streaming filter (2 passes) ──► SQLite shards ──► merge ──► rate selection ──► report + lookup
 (Anthem 11.4 GB TOC,     (/CA_* URLs,        pass 1: in_network, target      (resumable,       rates.sqlite   (POS, modifiers,      allowed_amounts.md/.csv
  UHC blob API,            dedupe by URL)       codes only → needed group IDs   one per file)                  expiry, type, tiers)   scripts/mrf/lookup.py
  Cigna signed TOC)                             pass 2: provider_references,
                                                needed ∩ network ∩ LA NPIs)
```

**How it stays small and fast**

- **Streaming.** The pipeline never downloads a whole file. HTTP is piped through gzip into the parser, so memory stays flat: 208–233 MB peak, most of it the fixed 347k-NPI set.
- **Two passes instead of buffering.** An Anthem California file lists about 10⁸ LA-NPI memberships across about 55k provider groups. The 16 target codes reference only about 2k of those groups. Pass 1 learns which groups are needed, and pass 2 keeps only those.
- **Byte-level fast path** (`fastscan.py`). Even a C JSON tokenizer tops out around 30 MB/s of JSON, and UHC's 9.4 GB file is about 200 GB of JSON. Item boundaries are therefore found with a regex on the item's first key, and only target items are parsed. On a real Anthem file, this path produced output identical to the ijson path (7,758 rates and 547,859 members) in 246 s instead of 566 s. When a file's layout makes the shortcut unsafe, the parser falls back to ijson.
- **Filtering.** Groups are kept only on an exact `network_name` match (for example `CA PPO PRUDENT BUYER`, not `... EXCLUDING SUTTER`) and only when they contain LA NPIs. The LA NPI set comes from NPPES. Tier A is the UCLA/Westwood ZIPs; Tier B is every ZCTA with at least half its land in LA County.
- **Privacy.** TINs are SHA-256 hashed inside the parser, keeping only the last four digits. No raw TIN is stored.
- **Resumability.** Each file is processed into its own shard with periodic commits and a `complete`/`failed` status. Re-running skips finished files.

**Reproduce** (Python 3.12+, `pip install -r requirements.txt`; `ijson` must report the `yajl2_c` backend):

```bash
python -m unittest discover tests
python scripts/mrf/build_npi_set.py --nppes-zip <NPPES_Data_Dissemination_*.zip> \
    --crosswalk tab20_zcta520_county20_natl.txt --out data/mrf/la_npis.csv
python scripts/mrf/build_manifest.py --month 2026-10 --out data/mrf/work/manifest.jsonl
python scripts/mrf/run_all.py --manifest data/mrf/work/manifest.jsonl --workers 4
python scripts/mrf/merge_shards.py --shards data/mrf/work/shards --out data/mrf/rates.sqlite
python scripts/mrf/report.py --db data/mrf/rates.sqlite
python scripts/mrf/lookup.py anthem_ca_prudent_buyer_ppo er_ankle_xray
```

To import a single file: `python scripts/mrf/import_mrf_rates.py --input <url> --plan-key <key> [--code 99213 ...]`. The full-run runbook is in [`server/README.md`](server/README.md).

**Measured numbers** (2026-10 files, measured on the 4-vCPU dev VM, about 26 MB/s download through its proxy). "Read" counts both passes plus the header probe, so it is about twice the file size:

| plan | file | read GB | JSON GB | items / refs scanned | rate rows kept | member rows | min | MB/s | peak RSS MB |
|---|---|---|---|---|---|---|---|---|---|
| Anthem Prudent Buyer | `CA_CPTNMED0001` | 3.6 | 14.2 | 41,871 / 55,927 | 7,838 | 586,168 | 3.0 | 20.4 | 220 |
| Anthem Prudent Buyer | `CA_MPUCMED0000` | 3.4 | 13.4 | 41,183 / 55,427 | 7,758 | 547,920 | 2.8 | 20.5 | 209 |
| Anthem Prudent Buyer | `CA_PPUCMED0000` (ijson path, reference) | 3.4 | 13.4 | 41,183 / 55,417 | 7,758 | 547,859 | 9.4 | 6.0 | 229 |
| Anthem Prudent Buyer | `CA_PPUCMED0001` | 3.8 | 14.7 | 41,183 / 57,337 | 7,886 | 599,676 | 2.5 | 24.9 | 218 |
| Cigna National PPO | `…_national-ppo_in-network-rates` | 1.6 | 35.2 | 1,152,909 / 66,395 | 162,900 | 47,551 | 15.7 | 1.7 | 246 |
| UHC of California Choice Plus | `…_Choice-Plus_8_in-network-rates` | 9.4 | 188.3 | 18,011 / 33,258 | 89,904 | 22,076 | 13.5 | 11.7 | 302 |
| **Total** | 6 files | **25.3** | **279.4** | | **284,044** | **2,351,250** | **46.8** | | |

**Expanded run (branch `mrf-expand`).** This run covers 7 networks: Anthem Prudent Buyer, Anthem Pathway EPO and HMO (Covered California), UHC California Choice Plus, Cigna National PPO, Ambetter from Health Net (`Exchange CA`), and L.A. Care Covered (299 contract files). It used 41 billing codes and 19 encounters, read 316 files (70 GB compressed, 444 GB of JSON), and kept 629,842 rate rows covering 68,333 LA NPIs. The import took 73.6 min summed over files, and the report took 63 min. 128 of 133 encounter × network cells have an `allowed_amount`. L.A. Care lists 37% of its rows at $0, which reflects capitated IPA contracts. These rows are excluded as non-prices.

For the first six-file run, the final SQLite was 515 MB: 284k rate rows, 2.35M member rows, and 53,893 distinct LA NPIs. The Anthem table of contents (11.4 GB, 147k reporting structures, 43M file references, 167 unique `CA_*` files totalling 221 GB) streamed in 436 s with 46 MB RSS. Running all 167 Anthem California files means reading about 440 GB, which takes about 1–2 h with 8 workers on a server (see the runbook).

**Limitations**

- A negotiated rate is not the final allowed amount. Claim edits, bundling, and multiple-procedure reductions change what is actually paid.
- UC SHIP is self-funded and uses Anthem Blue Cross Prudent Buyer PPO network rates. The demo uses the TOC files listed for the plan `BLUE CROSS PPO - REGENTS OF THE UNIVERSITY OF CALIFORNIA` (EIN 943067788). The same network's rates differ between files: for the same code and NPI, only 85% of keys matched between a UC file and a non-UC file.
- This is a monthly snapshot (2026-10). Billing codes, encounter templates, and POS fallbacks are candidates until a person verifies them (`verified_by` in `data/mrf/*.csv`).
- Blue Shield of California (blocked by bot protection on the HealthSparq portal) and Aetna (TLS through the dev proxy) are not included yet. See `data/mrf/discovery/`.
- Medicare benchmarks are never mixed into these tables.
