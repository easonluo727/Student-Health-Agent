"""Run one deterministic rate lookup from a rate_request.v1 JSON document.

    python lookup_cli.py '{"insurer": null, "plan_key": "anthem_ca_prudent_buyer_ppo",
                           "network": "CA PPO PRUDENT BUYER", "encounter_key": "urgent_care_flu",
                           "service_date": null, "selected_provider_npi": null}'
    python lookup_cli.py --file request.json [--as-of 2026-10-15] [--top-k 20] [--radius 25]

Prints the rate_lookup_result.v1 JSON. Trace records go to BRANCH_A_LOG_DIR/rate_lookup.jsonl.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import overlay_config

overlay_config.install_import_paths()

from rate_lookup import FallbackConfig, RateLookup  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("request", nargs="?", help="request JSON (or use --file / stdin)")
    ap.add_argument("--file", type=Path)
    ap.add_argument("--as-of", type=dt.date.fromisoformat)
    ap.add_argument("--top-k", type=int, default=FallbackConfig.top_k)
    ap.add_argument("--radius", type=float, default=FallbackConfig.radius_miles)
    ap.add_argument("--no-providers", action="store_true", help="omit the per-provider fallback list from the output")
    a = ap.parse_args()
    text = a.file.read_text() if a.file else (a.request or sys.stdin.read())
    req = json.loads(text)
    paths = overlay_config.load_paths()
    lk = RateLookup.from_paths(paths, as_of=a.as_of, log_path=paths.log_dir / "rate_lookup.jsonl",
                               fallback=FallbackConfig(top_k=a.top_k, radius_miles=a.radius))
    out = lk.lookup(req)
    if a.no_providers and out.get("fallback"):
        out["fallback"].pop("providers", None)
    print(json.dumps(out, indent=2, default=list))


if __name__ == "__main__":
    main()
