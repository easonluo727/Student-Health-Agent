"""Time-boxed probe: stream the start of an in-network MRF and count provider_references[].network_name.

Prints counts only (never raw records). Used during discovery to learn the exact network
strings an insurer writes, so plan_profiles.v1.json can filter on them.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

import ijson

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stream import open_source  # noqa: E402


def probe(src: str, budget: float) -> dict:
    t0 = time.time()
    s = open_source(src)
    names: collections.Counter = collections.Counter()
    order, n_refs, version = [], 0, None
    try:
        for prefix, e, v in ijson.parse(s, use_float=True):
            if prefix == "version":
                version = v
            elif e == "start_array" and prefix in ("provider_references", "in_network"):
                order.append(prefix)
                if prefix == "in_network" and n_refs:
                    break
            elif prefix == "provider_references.item.network_name.item":
                names[v] += 1
            elif prefix == "provider_references.item.provider_group_id":
                n_refs += 1
            if time.time() - t0 > budget:
                break
    finally:
        s.close()
    return {"version": version, "section_order": order, "provider_refs_seen": n_refs,
            "compressed_mb": round(s.stats.compressed_bytes / 1e6, 1), "seconds": round(time.time() - t0, 1),
            "network_names": names.most_common(40)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--budget", type=float, default=60)
    a = ap.parse_args()
    print(json.dumps(probe(a.source, a.budget), indent=1))
