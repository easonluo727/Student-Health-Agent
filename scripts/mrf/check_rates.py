"""Flag suspicious prices across networks. No LLM.

    python scripts/mrf/check_rates.py [--ratio 4] [--min-providers 10]

For every scenario component (data/mrf/encounters.csv) and every network a plan links to
(policy.sqlite plan_networks), it prices the component the way the agent does and reports:
  - missing     no dollar price in that network;
  - thin        fewer than --min-providers providers behind the price;
  - spread      the highest network median is more than --ratio times the lowest.
Exit code 1 when anything is flagged, so it can run in CI against a rate store.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workflow"))
from plan_db import POLICY_DB, RATES_DB, PlanDB, _encounters  # noqa: E402


def check(ratio: float, min_providers: int, rates: Path = RATES_DB) -> list[str]:
    db = PlanDB(POLICY_DB, rates)
    with sqlite3.connect(f"file:{POLICY_DB}?mode=ro", uri=True) as con:
        keys = [r[0] for r in con.execute("SELECT DISTINCT mrf_plan_key FROM plan_networks WHERE confirmed = 1")]
    flags = []
    for scenario, (label, rows) in _encounters().items():
        for row in rows:
            medians = {}
            for key in keys:
                r = db._price(key, row)
                where = f"{scenario}/{row['component']} ({row['codes']}) in {key}"
                if r.allowed_amount is None:
                    flags.append(f"missing  {where}")
                    continue
                medians[key] = r.allowed_amount
                if r.n_providers < min_providers:
                    flags.append(f"thin     {where}: ${r.allowed_amount:,.2f} from {r.n_providers} provider(s)")
            if len(medians) > 1 and max(medians.values()) > ratio * min(medians.values()):
                low, high = min(medians, key=medians.get), max(medians, key=medians.get)
                flags.append(f"spread   {scenario}/{row['component']}: {high} ${medians[high]:,.2f} vs "
                             f"{low} ${medians[low]:,.2f}")
    return flags


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ratio", type=float, default=4.0)
    ap.add_argument("--min-providers", type=int, default=10)
    a = ap.parse_args(argv)
    flags = check(a.ratio, a.min_providers)
    print("\n".join(flags) or "no flags")
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
