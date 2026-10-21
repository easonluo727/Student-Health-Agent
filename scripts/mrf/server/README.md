# Full MRF compile on a server (Phase 3)

The dev VM has a 30 GB disk and four CPUs, so the full run belongs on a Linux server. The
pipeline streams every file and keeps only filtered rows, so its disk use stays small: SQLite shards
of a few hundred MB, the NPPES zip (1.2 GB, which you can delete after step 3), and temporary spill files.

## 1. Pick a machine

- Linux, Python 3.12+, **8+ vCPUs**, 16 GB RAM, 50 GB disk, good bandwidth.
  Each file is read twice (two passes) and each worker process uses one core. With the byte-level fast
  path, Anthem files are network-bound. Cigna's file is CPU-bound, at about 1.7 MB/s compressed,
  because it holds 1.15M small items.
- **AWS us-east-1** (EC2 `c7i.2xlarge` or similar) sits next to Anthem's S3 bucket
  (`antm-pt-prod-dataz-nogbd-nophi-us-east1`). Any VPS works, but Anthem downloads are faster in
  us-east-1.
- Do not use a Railway service for this job: it has no persistent disk for shards between restarts and is billed by
  memory over a multi-hour run.

## 2. Set up

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv git tmux
git clone https://github.com/mr-jude-justin/amd-health-agent.git && cd amd-health-agent
git checkout mrf-compile
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -c "import ijson; print(ijson.backend)"   # must print yajl2_c (the C backend)
python -m unittest discover tests
```

## 3. Build the LA NPI set (about 5 min)

```bash
mkdir -p data/mrf/raw data/mrf/work
curl -o data/mrf/raw/nppes.zip https://download.cms.gov/nppes/NPPES_Data_Dissemination_October_2026_V2.zip
curl -o data/mrf/raw/zcta_county.txt https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt
python scripts/mrf/build_npi_set.py --nppes-zip data/mrf/raw/nppes.zip \
    --crosswalk data/mrf/raw/zcta_county.txt --out data/mrf/la_npis.csv
rm data/mrf/raw/nppes.zip
```

Use the NPPES file for the same month as the MRFs. Its name is listed at
https://download.cms.gov/nppes/NPI_Files.html.

## 4. Build the manifest

```bash
python scripts/mrf/build_manifest.py --month 2026-10 --out data/mrf/work/manifest.jsonl
```

This takes about 8 minutes, most of it spent streaming Anthem's 11 GB TOC. It resolves:

| plan_key | files | size |
|---|---|---|
| `anthem_ca_prudent_buyer_ppo` | 167 `CA_*` files | 221 GB |
| `uhc_ca_choice_plus` | 1 | 9.4 GB |
| `cigna_national_ppo` | 1 (signed URL) | 1.6 GB |

Cigna URLs are signed and expire, so build the manifest right before step 5.

**Scope shortcut.** The spike compares rates for the same code and NPI across two Anthem files.
If they match, you only need the UC Regents files for the demo:

```bash
grep -E 'CA_(PPUC|MPUC|CPTN)MED' data/mrf/work/manifest.jsonl > data/mrf/work/manifest.anthem_uc.jsonl
grep -v anthem data/mrf/work/manifest.jsonl >> data/mrf/work/manifest.anthem_uc.jsonl
```

## 5. Run (resumable)

```bash
tmux new -s mrf
python scripts/mrf/run_all.py --manifest data/mrf/work/manifest.jsonl --workers 8 \
    --shards data/mrf/work/shards --tier B 2>&1 | tee data/mrf/work/run_all.log
```

- You get one SQLite shard per file. A shard marked `complete` is skipped on re-run, and a failed or partial
  one is redone. If the job crashes, run the same command again.
- Progress lines go to `data/mrf/work/shards/<file>.log` every 2 min. They contain counts only, never raw data.
- Keep the request rate polite. Each worker streams one file. Hosts are interleaved, so at most a few
  workers hit one insurer at a time. A 403 or 429 stops that host. **Do not evade a block. Report it.**
- Measured on the dev VM: one Anthem California file takes 2.5–3 min. That is two passes over about 1.8 GB at
  20–25 MB/s, limited by the network, not the CPU. All 167 files means about 440 GB read, roughly **1–1.5 h with
  8 workers** if the server sustains about 100 MB/s, or proportionally longer otherwise. UHC's 9.4 GB file takes
  about 14 min and Cigna's about 16 min, both single process.

## 6. Merge, report, publish

```bash
python scripts/mrf/merge_shards.py --shards data/mrf/work/shards --out data/mrf/rates.sqlite
ls -lh data/mrf/rates.sqlite
```

Commit `rates.sqlite` only if it is under 50 MB. Otherwise publish it as a release asset and upload it to the
Railway volume (`db/README.md`); the agent reads it directly.

## Blocked insurers

- **Blue Shield of California:** files sit behind a bot-protection challenge on the HealthSparq portal. A
  person opens the portal in a normal browser and copies the table-of-contents URL into
  `data/plan_profiles.v1.json` (`toc_url`). Then set `discovery: toc_stream`, a `file_regex`,
  and `network_names`. Use `scripts/mrf/probe_networks.py <file-url>` to learn the exact network string.
- **Aetna:** TLS failed through the dev VM's proxy. Retry the portal from the server, then follow the same steps as
  for Blue Shield.
