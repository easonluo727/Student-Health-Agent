"""Run every manifest file through import_mrf_rates in parallel, one SQLite shard per file.

    python scripts/mrf/run_all.py --manifest data/mrf/work/manifest.jsonl --workers 4 \
        --shards data/mrf/work/shards [--tier B]

- One process per file (`--workers`, default 4; keep <= 4 per insurer host to stay polite).
- Each shard records its own status; re-running skips shards marked complete and redoes
  failed/partial ones, so a crash never restarts finished files.
- 403/429 from a host stops new work for that host (HostRefused) instead of retrying around it.
- Progress lines go to <shards>/<name>.log; a summary line per finished file goes to stdout.
Afterwards: python scripts/mrf/merge_shards.py --shards data/mrf/work/shards --out data/mrf/rates.sqlite
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)


def shard_name(url: str) -> str:
    base = urlparse(url).path.rsplit("/", 1)[-1].split(".")[0][:80]
    return f"{base}_{hashlib.sha1(url.split('?')[0].encode()).hexdigest()[:8]}"


def _work(job: dict, shards: str, tier: str, profiles: str, codes_csv: str, npi_csv: str) -> dict:
    import contextlib

    import import_mrf_rates as imp
    name = shard_name(job["url"])
    log = open(os.path.join(shards, name + ".log"), "a")
    with contextlib.redirect_stderr(log):
        profile = imp.load_profile(profiles, job["plan_key"])
        res = imp.import_file(os.path.join(shards, name + ".sqlite"), job["url"], profile, imp.load_codes(codes_csv),
                              imp.load_npis(npi_csv, tier), profile.get("network_names"),
                              source_url=job["url"].split("?")[0], file_date=job.get("file_date"),
                              spill_dir=shards, progress_every=120)
    log.close()
    return {"plan_key": job["plan_key"], "shard": name, **res}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--shards", default=os.path.join(ROOT, "data", "mrf", "work", "shards"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tier", choices=["A", "B"], default="B")
    ap.add_argument("--profiles", default=os.path.join(ROOT, "data", "plan_profiles.v1.json"))
    ap.add_argument("--codes-csv", default=os.path.join(ROOT, "data", "mrf", "billing_codes.csv"))
    ap.add_argument("--npi-csv", default=os.path.join(ROOT, "data", "mrf", "la_npis.csv"))
    a = ap.parse_args(argv)
    os.makedirs(a.shards, exist_ok=True)
    with open(a.manifest) as f:
        jobs = [json.loads(line) for line in f if line.strip()]
    # Interleave hosts so parallel workers do not all hit one insurer at once.
    by_host = collections.defaultdict(list)
    for j in sorted(jobs, key=lambda j: -(j.get("bytes") or 0)):
        by_host[urlparse(j["url"]).netloc].append(j)
    order = [j for group in zip(*[iter_pad(v, max(map(len, by_host.values()))) for v in by_host.values()]) for j in group if j]
    refused: set[str] = set()
    t0 = time.time()
    done = 0
    with cf.ProcessPoolExecutor(a.workers) as ex:
        futs = {ex.submit(_work, j, a.shards, a.tier, a.profiles, a.codes_csv, a.npi_csv): j for j in order}
        for fut in cf.as_completed(futs):
            j = futs[fut]
            host = urlparse(j["url"]).netloc
            try:
                r = fut.result()
            except Exception as e:  # worker crashed outside import_file's own handling
                r = {"plan_key": j["plan_key"], "status": "failed", "error": f"{type(e).__name__}: {e}"}
            if "HostRefused" in str(r.get("error")):
                refused.add(host)
                for f2, j2 in futs.items():
                    if urlparse(j2["url"]).netloc == host:
                        f2.cancel()
            done += 1
            print(json.dumps({"done": done, "of": len(jobs), "elapsed_min": round((time.time() - t0) / 60, 1), **r}), flush=True)
    if refused:
        print(f"STOPPED hosts after 403/429: {sorted(refused)} — report, do not evade.", flush=True)


def iter_pad(items, n):
    return list(items) + [None] * (n - len(items))


if __name__ == "__main__":
    main()
