"""Read the 100 held-out bills with Muse Glimmer 30B before and after SFT, on one Modal H100.

vLLM loads the bf16 base once and serves the LoRA from modal_sft.py as a
request-level adapter, so both models see the same weights, prompt, image and
greedy decoding. Predictions are written for scripts/bills/score.py.

    modal run scripts/bills/modal_eval.py
    python scripts/bills/score.py ~/Downloads/bills-eval-100.jsonl preds/glimmer-*.jsonl
"""
import json
import time
from pathlib import Path

import modal

VLLM_VERSION = "0.32.0"
BASE_MODEL = "meta-models/Muse-Glimmer-30B"
BASE_REVISION = "a4e59da52a7bc87ae7251dd5545c0dd437c44b68"
ADAPTER = "/vol/outputs/bills-sft-glimmer-v1"
EVAL_FILE = Path.home() / "Downloads" / "bills-eval-100.jsonl"
PREDS = "fireworks-training-runs/bills-sft-glimmer-v1/preds"  # under the repo root, which is where this runs from

app = modal.App("bills-eval")
vol = modal.Volume.from_name("bills-sft")
image = (
    # A devel image: flashinfer compiles kernels at warm-up and needs nvcc.
    modal.Image.from_registry("nvidia/cuda:12.9.1-devel-ubuntu24.04", add_python="3.12")
    .uv_pip_install(f"vllm=={VLLM_VERSION}")
    .env({"HF_HOME": "/vol/hf"})
)


@app.function(image=image, gpu="H100", volumes={"/vol": vol}, timeout=2 * 3600)
def generate(max_tokens: int = 4096) -> dict:
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest

    rows = [json.loads(l) for l in open("/vol/data/eval_openai.jsonl") if l.strip()]
    prompts = [r["messages"][:-1] for r in rows]
    llm = LLM(
        model=BASE_MODEL,
        revision=BASE_REVISION,
        enable_lora=True,
        max_lora_rank=32,
        max_model_len=8192,
        limit_mm_per_prompt={"image": 1},
        gpu_memory_utilization=0.92,
    )
    vol.commit()  # keep the downloaded weights for the next run
    # Keep special tokens so a reasoning message can be told apart from the answer.
    params = SamplingParams(temperature=0.0, max_tokens=max_tokens, skip_special_tokens=False)
    out = {}
    for name, lora in (("glimmer-base", None), ("glimmer-sft", LoRARequest("bills-sft", 1, ADAPTER))):
        start = time.time()
        results = llm.chat(prompts, params, lora_request=lora)
        elapsed = time.time() - start
        out[name] = {
            "seconds_total": elapsed,
            "rows": [
                {
                    "i": i,
                    "model": name,
                    "output": r.outputs[0].text,
                    "finish_reason": r.outputs[0].finish_reason,
                    "output_tokens": len(r.outputs[0].token_ids),
                    # Batched throughput, not single-request latency.
                    "seconds": elapsed / len(rows),
                }
                for i, r in enumerate(results)
            ],
        }
        Path(f"/vol/outputs/eval-{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in out[name]["rows"]))
        vol.commit()
        print(name, f"{elapsed:.0f}s for {len(rows)} bills")
    return out


@app.local_entrypoint()
def main(max_tokens: int = 4096):
    with vol.batch_upload(force=True) as up:
        up.put_file(EVAL_FILE, "/data/eval_openai.jsonl")
    out = generate.remote(max_tokens)
    preds = Path(PREDS)
    preds.mkdir(parents=True, exist_ok=True)
    for name, res in out.items():
        (preds / f"{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in res["rows"]))
        cut = sum(r["finish_reason"] == "length" for r in res["rows"])
        print(f"{name}: {res['seconds_total']:.0f}s, {cut} replies cut at max_tokens -> {preds / (name + '.jsonl')}")
