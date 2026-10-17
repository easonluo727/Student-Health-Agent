"""List every mapping that still needs a human decision (plan->MRF network and service->rate).

    python list_pending_mappings.py [--json]
"""
from __future__ import annotations

import argparse
import json

import overlay_config


def pending(paths: overlay_config.OverlayPaths) -> list[dict]:
    out = []
    pmap = json.loads((paths.overlay_data / "policy_mrf_map.v1.json").read_text())
    for e in pmap["entries"]:
        if e["review_status"] != "verified" or not e["mrf_plan_key"]:
            out.append({"kind": "policy_mrf_map", "id": f"{e['policy_plan_id']}/{e['policy_network_category']}",
                        "status": e["review_status"], "question": e.get("question_for_human")})
    smap = json.loads((paths.overlay_data / "service_rate_map.v1.json").read_text())
    for e in smap["entries"]:
        if e["review_status"] != "verified":
            out.append({"kind": "service_rate_map", "id": e["entry_id"], "status": e["review_status"],
                        "question": e.get("clarification_question"),
                        "options": [f"{o['option_id']} ({o['kind']}): {o['label']}" for o in e["options"]]})
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    items = pending(overlay_config.load_paths())
    if a.json:
        print(json.dumps(items, indent=2))
        return
    for i in items:
        print(f"[{i['kind']}] {i['id']}  ({i['status']})")
        if i.get("question"):
            print(f"    question: {i['question']}")
        for o in i.get("options", []):
            print(f"    option:   {o}")


if __name__ == "__main__":
    main()
