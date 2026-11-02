"""Serve the fine-tuned bill reader (Muse Glimmer 30B + the bills-sft LoRA) as an OpenAI-compatible endpoint on Modal.

Same base revision, adapter and vLLM version as modal_eval.py. The server scales to zero: it costs nothing while
idle, and the first request after that waits for a cold start (the 30B weights load from the bills-sft volume, a few
minutes), so call it once before a demo. The agent reaches it through llm_client.bill_readers():

    modal secret create bills-reader BILL_API_KEY=<any long random string>
    .venv/bin/modal deploy scripts/bills/modal_serve.py

    # then, on the agent service (Railway variables) or in .env:
    BILL_BASE_URL=https://<workspace>--bills-reader-serve.modal.run/v1
    BILL_MODEL=bills-reader
    BILL_API_KEY=<the same string>

    # warm it up (and check it answers):
    curl -H "Authorization: Bearer $BILL_API_KEY" $BILL_BASE_URL/models

Stop it with `modal app stop bills-reader`.
"""
import os
import subprocess

import modal

VLLM_VERSION = "0.32.0"
BASE_MODEL = "meta-models/Muse-Glimmer-30B"
BASE_REVISION = "a4e59da52a7bc87ae7251dd5545c0dd437c44b68"
ADAPTER = "/vol/outputs/bills-sft-glimmer-v1"
PORT = 8000
IDLE_MINUTES = 10  # how long a warm server waits for the next bill before scaling to zero

app = modal.App("bills-reader")
vol = modal.Volume.from_name("bills-sft")
image = (
    # A devel image: flashinfer compiles kernels at warm-up and needs nvcc.
    modal.Image.from_registry("nvidia/cuda:12.9.1-devel-ubuntu24.04", add_python="3.12")
    .uv_pip_install(f"vllm=={VLLM_VERSION}")
    .env({"HF_HOME": "/vol/hf"})
)


@app.function(image=image, gpu="H100", volumes={"/vol": vol}, secrets=[modal.Secret.from_name("bills-reader")],
              scaledown_window=IDLE_MINUTES * 60, timeout=24 * 3600)
@modal.concurrent(max_inputs=16)
@modal.web_server(port=PORT, startup_timeout=20 * 60)
def serve():
    subprocess.Popen([
        "vllm", "serve", BASE_MODEL,
        "--revision", BASE_REVISION,
        "--served-model-name", "muse-glimmer-30b",
        "--enable-lora", "--max-lora-rank", "32",
        "--lora-modules", f"bills-reader={ADAPTER}",
        "--max-model-len", "8192",
        "--limit-mm-per-prompt", '{"image": 1}',
        "--gpu-memory-utilization", "0.92",
        "--host", "0.0.0.0", "--port", str(PORT),
        "--api-key", os.environ["BILL_API_KEY"],
    ])
