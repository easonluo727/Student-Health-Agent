"""Score bill-reading predictions against the generator's answers.

Every number in the eval table comes from here. A prediction is the model's raw
reply; the answer is the assistant JSON of the same row in bills-eval-100.jsonl.

    python scripts/bills/score.py ~/Downloads/bills-eval-100.jsonl preds/*.jsonl

Each predictions file is JSONL with {"i": row index, "model": name,
"output": raw reply, "seconds": time for this row}.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

HEADER_FIELDS = ("provider_name", "provider_npi", "statement_date", "total_billed")
LINE_FIELDS = ("service_date", "code", "description", "units", "billed")
MISSING = object()  # a key the reply left out; never equals an answer, not even null


def parse_prediction(text: str):
    """The bill JSON from a raw reply, or None.

    Takes the first JSON object after the last `<|message|>` marker (Glimmer
    may write a reasoning message first), else the first in the text.
    """
    if not text:
        return None
    marker = text.rfind("<|message|>")
    if marker >= 0:
        text = text[marker + len("<|message|>"):]
    start = text.find("{")
    while start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(text, start)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def same(pred, gold) -> bool:
    """Numbers within half a cent; strings equal after collapsing whitespace; null only matches a stated null."""
    if pred is MISSING:
        return False
    if gold is None or pred is None:
        return gold is None and pred is None
    if isinstance(gold, (int, float)) and not isinstance(gold, bool):
        try:
            return abs(float(pred) - float(gold)) < 0.005
        except (TypeError, ValueError):
            return False
    return " ".join(str(pred).split()) == " ".join(str(gold).split())


def score_bill(pred_text: str, gold: dict) -> dict:
    """Per-field results for one bill. Lines are compared by position; a missing line or key is wrong."""
    pred = parse_prediction(pred_text)
    result = {"json_ok": pred is not None, "fields": {}}
    pred = pred or {}
    for f in HEADER_FIELDS:
        result["fields"][f] = [same(pred.get(f, MISSING), gold.get(f))]
    pred_lines = pred.get("lines") if isinstance(pred.get("lines"), list) else []
    gold_lines = gold.get("lines") or []
    result["line_count_ok"] = len(pred_lines) == len(gold_lines)
    for f in LINE_FIELDS:
        result["fields"]["line." + f] = [
            i < len(pred_lines) and isinstance(pred_lines[i], dict) and same(pred_lines[i].get(f, MISSING), g.get(f))
            for i, g in enumerate(gold_lines)
        ]
    result["all_ok"] = result["line_count_ok"] and all(all(v) for v in result["fields"].values())
    return result


def summarize(scores: list[dict], seconds: list[float]) -> dict:
    """Rates over bills (json_ok, line_count_ok, all_ok) and over field instances (each field)."""
    n = len(scores)
    out = {
        "bills": n,
        "json_ok": sum(s["json_ok"] for s in scores) / n,
        "line_count_ok": sum(s["line_count_ok"] for s in scores) / n,
        "all_ok": sum(s["all_ok"] for s in scores) / n,
    }
    hits, total = defaultdict(int), defaultdict(int)
    for s in scores:
        for f, results in s["fields"].items():
            hits[f] += sum(results)
            total[f] += len(results)
    for f in total:
        out[f] = hits[f] / total[f] if total[f] else None
    out["all_fields"] = sum(hits.values()) / sum(total.values())
    out["seconds_per_bill"] = sum(seconds) / len(seconds) if seconds else None
    return out


def paired(a: list[dict], b: list[dict]) -> dict:
    """How many bills only one of two models got fully right (exact two-sided McNemar p)."""
    from math import comb

    only_a = sum(1 for x, y in zip(a, b) if x["all_ok"] and not y["all_ok"])
    only_b = sum(1 for x, y in zip(a, b) if y["all_ok"] and not x["all_ok"])
    n, k = only_a + only_b, min(only_a, only_b)
    p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"only_first": only_a, "only_second": only_b, "p_value": p}


def main():
    gold_path, *pred_paths = sys.argv[1:]
    golds = [json.loads(json.loads(l)["messages"][-1]["content"]) for l in Path(gold_path).read_text().splitlines() if l.strip()]
    by_model = defaultdict(dict)
    for p in pred_paths:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                by_model[r["model"]][r["i"]] = r
    scored = {}
    for model, rows in by_model.items():
        missing = [i for i in range(len(golds)) if i not in rows]
        if missing:
            sys.exit(f"{model}: no prediction for rows {missing[:10]}")
        scored[model] = [score_bill(rows[i]["output"], golds[i]) for i in range(len(golds))]
        summary = summarize(scored[model], [rows[i]["seconds"] for i in range(len(golds))])
        print(json.dumps({"model": model, **{k: round(v, 4) if isinstance(v, float) else v for k, v in summary.items()}}))
    models = list(scored)
    for i, a in enumerate(models):
        for b in models[i + 1:]:
            print(json.dumps({"pair": [a, b], **paired(scored[a], scored[b])}))


if __name__ == "__main__":
    main()
