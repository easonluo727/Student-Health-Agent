"""Resolve this month's in-network file URLs for each plan profile into a manifest (JSONL).

    python scripts/mrf/build_manifest.py --month 2026-10 --out data/mrf/work/manifest.jsonl \
        [--plan-key anthem_ca_prudent_buyer_ppo ...] [--limit-per-plan N]

Discovery methods (data/plan_profiles.v1.json "discovery"):
  toc_stream        stream the insurer TOC (Anthem: 11+ GB) and keep URLs matching file_regex
  uhc_blob_listing  UHC's official blob listing API, filtered by file_regex
  cigna_latest      Cigna's latest.json -> signed TOC -> file_regex (signed URLs expire; build
                    the manifest right before the run)
  centene_page      Centene's price-transparency page -> newest TOC matching index_regex -> file_regex
                    (Health Net / Ambetter)
  lacare_index      L.A. Care: list the official bucket, read every *_LA_Care_index.json, keep files
                    of plan_id, fetch them from the portal's download URL
  manual            skipped (Blue Shield, Aetna: see data/mrf/discovery/)
Each line: {"plan_key", "url", "bytes", "file_date"}; deduplicated by URL (query string ignored).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from stream import USER_AGENT  # noqa: E402
from toc import scan_toc  # noqa: E402

S = requests.Session()
S.headers["User-Agent"] = USER_AGENT


def _size(url: str) -> int | None:
    try:
        r = S.head(url, timeout=60, allow_redirects=True)
        return int(r.headers["Content-Length"]) if r.ok and "Content-Length" in r.headers else None
    except requests.RequestException:
        return None


@functools.lru_cache(maxsize=4)
def _toc_locations(toc: str) -> tuple[str, ...]:
    """All unique in-network file URLs of one TOC (cached: several plans share Anthem's 11 GB TOC)."""
    return tuple(scan_toc(toc, lambda loc, d: True, progress_every=120).files)


def _centene(profile: dict) -> tuple[list[str], str]:
    page = S.get(profile["toc_url"], timeout=60).text
    rx = re.compile(profile["index_regex"])
    tocs = sorted({h for h in re.findall(r'href="([^"]+\.json)"', page) if rx.search(h)})
    if not tocs:
        return [], ""
    toc = urljoin(profile["toc_url"], tocs[-1])  # names start with YYYY-MM-DD, so the last is newest
    file_date = os.path.basename(toc)[:10]
    return list(_toc_locations(toc)), file_date


def _lacare(profile: dict) -> tuple[list[str], str]:
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    keys, token = [], None
    while True:
        params = {"list-type": "2", "max-keys": "1000", **({"continuation-token": token} if token else {})}
        root = ET.fromstring(S.get(profile["toc_url"], params=params, timeout=60).content)
        keys += [c.find("s:Key", ns).text for c in root.findall("s:Contents", ns)]
        if root.find("s:IsTruncated", ns).text != "true":
            break
        token = root.find("s:NextContinuationToken", ns).text
    names, dates = set(), []
    for key in sorted(k for k in keys if k.endswith("_LA_Care_index.json")):
        toc = S.get(f"https://transparency.lacare.org/{key}", timeout=60).json()
        for rs in toc.get("reporting_structure", []):
            if any(p.get("plan_id") == profile["plan_id"] for p in rs.get("reporting_plans", [])):
                names.update(os.path.basename(f["location"]) for f in rs.get("in_network_files", []))
                dates.append(os.path.basename(key)[:10])
    present = {os.path.basename(k) for k in keys}
    return [f"https://transparency.lacare.org/download/{n}" for n in sorted(names & present)], max(dates, default="")


def discover(profile: dict, month: str) -> list[dict]:
    rx = re.compile(profile["file_regex"])
    file_date = f"{month}-01"
    method = profile["discovery"]
    if method == "toc_stream":
        toc = profile["toc_url"].replace("YYYY-MM", month)
        urls = [u for u in _toc_locations(toc) if rx.search(u.split("?")[0])]
    elif method == "uhc_blob_listing":
        blobs = S.get(profile["toc_url"], timeout=300).json()["blobs"]
        urls = [b["downloadUrl"] for b in blobs if b["name"].startswith(file_date) and rx.search(b["name"])]
    elif method == "cigna_latest":
        latest = S.get(profile["toc_url"], timeout=60).json()
        toc = next(f["url"] for m in latest["mrfs"] if m["kind"] == "TOC" for f in m["files"])
        file_date = next(f["last_updated_on"] for m in latest["mrfs"] if m["kind"] == "TOC" for f in m["files"])
        res = scan_toc(toc, lambda loc, d: bool(rx.search(loc.split("?")[0])))
        urls = list(res.files)
    elif method == "centene_page":
        locs, file_date = _centene(profile)
        urls = [u for u in locs if rx.search(u)]
    elif method == "lacare_index":
        urls, file_date = _lacare(profile)
    else:
        return []
    seen, out = set(), []
    for u in urls:
        key = u.split("?")[0]
        if key not in seen:
            seen.add(key)
            out.append({"plan_key": profile["plan_key"], "url": u, "bytes": _size(u) if len(urls) <= 200 else None,
                        "file_date": file_date})
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM reporting month")
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan-key", action="append")
    ap.add_argument("--profiles", default=os.path.join(ROOT, "data", "plan_profiles.v1.json"))
    ap.add_argument("--limit-per-plan", type=int, help="keep only the N largest files per plan (spikes)")
    a = ap.parse_args(argv)
    with open(a.profiles) as f:
        plans = [p for p in json.load(f)["plans"] if not a.plan_key or p["plan_key"] in a.plan_key]
    with open(a.out, "w") as f:
        for p in plans:
            rows = discover(p, a.month)
            if a.limit_per_plan:
                rows = sorted(rows, key=lambda r: -(r["bytes"] or 0))[: a.limit_per_plan]
            for r in rows:
                f.write(json.dumps(r) + "\n")
            print(f"{p['plan_key']}: {len(rows)} files, {sum(r['bytes'] or 0 for r in rows)/1e9:.1f} GB", flush=True)


if __name__ == "__main__":
    main()
