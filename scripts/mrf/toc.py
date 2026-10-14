"""Stream a TiC table-of-contents file and collect in-network file URLs, deduplicated by URL.

Anthem's TOC is 11+ GB gzipped on one JSON line, so this never loads the file: each
`reporting_structure` item is built on its own and discarded after its URLs are recorded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import ijson

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stream import open_source  # noqa: E402

MAX_SAMPLE_PLANS = 5
MAX_MATCHED_PLANS = 50


@dataclass
class TocFile:
    location: str
    description: str | None
    n_plans: int = 0
    market_types: dict[str, int] = field(default_factory=dict)
    sample_plans: list[dict] = field(default_factory=list)
    matched_plans: list[dict] = field(default_factory=list)


@dataclass
class TocResult:
    header: dict
    files: dict[str, TocFile]
    reporting_structures: int
    in_network_refs: int
    compressed_bytes: int
    decompressed_bytes: int
    wall_seconds: float
    peak_rss_mb: float
    complete: bool


def scan_toc(src: str, keep: Callable[[str, str | None], bool] = lambda loc, desc: True,
             time_budget: float | None = None, opener=open_source, progress_every: float | None = None,
             plan_match: re.Pattern | None = None) -> TocResult:
    """keep(location, description) decides which in-network files to record. plan_match, if set,
    records up to MAX_MATCHED_PLANS plans per file whose name or sponsor matches it."""
    t0 = time.time()
    stream = opener(src)
    header: dict = {}
    files: dict[str, TocFile] = {}
    n_struct = n_refs = 0
    complete = True
    next_progress = t0 + progress_every if progress_every else None
    try:
        events = ijson.parse(stream, use_float=True)
        items = ijson.items(_tee_header(events, header), "reporting_structure.item")
        for rs in items:
            n_struct += 1
            plans = rs.get("reporting_plans") or []
            for f in rs.get("in_network_files") or []:
                n_refs += 1
                loc, desc = f.get("location") or "", f.get("description")
                if not loc or not keep(loc, desc):
                    continue
                tf = files.get(loc)
                if tf is None:
                    tf = files[loc] = TocFile(loc, desc)
                tf.n_plans += len(plans)
                for p in plans:
                    mt = p.get("plan_market_type") or "unknown"
                    tf.market_types[mt] = tf.market_types.get(mt, 0) + 1
                    summary = {k: p.get(k) for k in ("plan_name", "plan_id_type", "plan_id", "plan_market_type")}
                    if len(tf.sample_plans) < MAX_SAMPLE_PLANS:
                        tf.sample_plans.append(summary)
                    if (plan_match and len(tf.matched_plans) < MAX_MATCHED_PLANS
                            and plan_match.search(f'{p.get("plan_name") or ""} {p.get("plan_sponsor_name") or ""}')):
                        tf.matched_plans.append(summary)
            if next_progress and time.time() >= next_progress:
                next_progress += progress_every
                el = time.time() - t0
                print(f"[toc] {el:.0f}s structures {n_struct} refs {n_refs} kept {len(files)} "
                      f"{stream.stats.compressed_bytes/1e9:.2f} GB compressed "
                      f"({stream.stats.compressed_bytes/1e6/el:.1f} MB/s)", file=sys.stderr, flush=True)
            if time_budget is not None and time.time() - t0 > time_budget:
                complete = False
                break
    finally:
        stream.close()
    return TocResult(header, files, n_struct, n_refs, stream.stats.compressed_bytes, stream.stats.decompressed_bytes,
                     round(time.time() - t0, 3), round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                     complete)


def _tee_header(events, header: dict):
    for prefix, e, v in events:
        if "." not in prefix and prefix and e in ("string", "number", "integer", "double"):
            header[prefix] = v
        yield prefix, e, v


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Stream a TOC and record matching in-network file URLs.")
    ap.add_argument("source")
    ap.add_argument("--match", default=None, help="regex on file URL (e.g. '/CA_')")
    ap.add_argument("--plan-match", help="regex on plan_name/plan_sponsor_name to record per file")
    ap.add_argument("--budget", type=float, default=None, help="stop after N seconds (spikes only)")
    ap.add_argument("--out", help="write JSON result here")
    a = ap.parse_args(argv)
    rx = re.compile(a.match) if a.match else None
    r = scan_toc(a.source, (lambda loc, d: bool(rx.search(loc))) if rx else (lambda loc, d: True), a.budget,
                 progress_every=60, plan_match=re.compile(a.plan_match, re.I) if a.plan_match else None)
    print(f"structures {r.reporting_structures} | file refs {r.in_network_refs} | unique kept {len(r.files)} | "
          f"{r.compressed_bytes/1e6:.0f} MB compressed in {r.wall_seconds:.0f}s | RSS {r.peak_rss_mb} MB | complete {r.complete}")
    if a.out:
        with open(a.out, "w") as f:
            json.dump({**{k: v for k, v in r.__dict__.items() if k != "files"},
                       "files": [tf.__dict__ for tf in r.files.values()]}, f, indent=1)


if __name__ == "__main__":
    main()
