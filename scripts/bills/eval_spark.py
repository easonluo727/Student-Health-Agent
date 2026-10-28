"""Read the 100 held-out bills with the configured API model (Muse Spark), for scripts/bills/score.py.

Uses the project's LLM client (LLM_BASE_URL / LLM_MODEL / LLM_API_KEY), the same
prompt and image as the Glimmer eval, and the fast-call extra body.

    python scripts/bills/eval_spark.py ~/Downloads/bills-eval-100.jsonl
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workflow"))

from llm_client import fast_extra_body, llm_model, load_env, make_client  # noqa: E402

PREDS = ROOT / "fireworks-training-runs" / "bills-sft-glimmer-v1" / "preds"


def main():
    load_env()
    rows = [json.loads(l) for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]
    client, model = make_client(timeout=180.0), llm_model()
    name = model.rsplit("/", 1)[-1]

    def read(i):
        start = time.time()
        try:
            r = client.chat.completions.create(
                model=model, messages=rows[i]["messages"][:-1], temperature=0, extra_body=fast_extra_body()
            )
            text = r.choices[0].message.content or ""
        except Exception as e:  # recorded as an unparseable reply, so the row still counts
            text = f"ERROR {type(e).__name__}: {e}"[:500]
        return {"i": i, "model": name, "output": text, "seconds": time.time() - start}

    with ThreadPoolExecutor(8) as pool:
        out = list(pool.map(read, range(len(rows))))
    PREDS.mkdir(parents=True, exist_ok=True)
    path = PREDS / f"{name}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in out))
    print(f"{name}: {sum(o['output'].startswith('ERROR') for o in out)} errors -> {path}")


if __name__ == "__main__":
    main()
