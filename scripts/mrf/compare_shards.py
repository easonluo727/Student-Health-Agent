"""Scope-shortcut test: do two MRF files of the same network carry the same rates?

    python scripts/mrf/compare_shards.py data/mrf/work/shards/A.sqlite data/mrf/work/shards/B.sqlite

For every (billing code, billing class, POS list, modifiers, NPI) present in both shards, compares
the set of dollar rates. Prints counts and the share of identical keys (no raw rows).
"""
from __future__ import annotations

import json
import sqlite3
import sys

Q = """
SELECT r.billing_code, r.billing_class, r.service_codes, r.modifiers, m.npi, r.negotiated_rate
FROM negotiated_rates r JOIN provider_members m
  ON m.source_id = r.source_id AND m.provider_group_id = r.provider_group_id
WHERE r.negotiated_type IN ('negotiated', 'derived', 'fee schedule')
"""


def rate_sets(path: str) -> dict[tuple, frozenset]:
    out: dict[tuple, set] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        for code, cls, pos, mods, npi, rate in con.execute(Q):
            out.setdefault((code, cls, pos, mods, npi), set()).add(round(rate, 2))
    return {k: frozenset(v) for k, v in out.items()}


def compare(a: str, b: str) -> dict:
    ra, rb = rate_sets(a), rate_sets(b)
    common = ra.keys() & rb.keys()
    same = sum(1 for k in common if ra[k] == rb[k])
    by_code: dict[str, list[int]] = {}
    for k in common:
        s = by_code.setdefault(k[0], [0, 0])
        s[0] += 1
        s[1] += ra[k] == rb[k]
    return {"keys_a": len(ra), "keys_b": len(rb), "common_keys": len(common), "identical": same,
            "identical_share": round(same / len(common), 4) if common else None,
            "npis_a": len({k[4] for k in ra}), "npis_b": len({k[4] for k in rb}),
            "by_code": {c: {"common": n, "identical": s} for c, (n, s) in sorted(by_code.items())}}


if __name__ == "__main__":
    print(json.dumps(compare(sys.argv[1], sys.argv[2]), indent=1))
