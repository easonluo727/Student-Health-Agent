"""QLoRA SFT of Muse Glimmer 30B on bill images, on one Modal H100, with Axolotl.

Fireworks dataset uploads are disabled for now, so the bill-reading model trains
here instead. The language layers get LoRA; the vision tower stays frozen
(Meta's post-training docs recommend this). Config follows Axolotl's
examples/muse-glimmer/qlora-vision.yaml (v0.21.1).

    modal run scripts/bills/modal_sft.py::prep     # convert + upload the JSONL (no GPU)
    modal run --detach scripts/bills/modal_sft.py::train

Data, weights cache and outputs live on the Modal volume `bills-sft`.
"""
import json
from pathlib import Path

import modal

AXOLOTL_IMAGE = "axolotlai/axolotl:0.21.1-py3.12-cu130-2.14.1"
BASE_MODEL = "meta-models/Muse-Glimmer-30B"
BASE_REVISION = "a4e59da52a7bc87ae7251dd5545c0dd437c44b68"
RUN = "bills-sft-glimmer-v1"
DOWNLOADS = Path.home() / "Downloads"
FILES = {"train": "bills-train-900.jsonl", "eval": "bills-eval-100.jsonl"}

app = modal.App("bills-sft")
vol = modal.Volume.from_name("bills-sft", create_if_missing=True)
image = modal.Image.from_registry(AXOLOTL_IMAGE).env({"HF_HOME": "/vol/hf", "WANDB_MODE": "disabled"})

CONFIG = {
    "base_model": BASE_MODEL,
    "revision_of_model": BASE_REVISION,
    "processor_type": "AutoProcessor",
    "plugins": ["axolotl.integrations.cut_cross_entropy.CutCrossEntropyPlugin"],
    "load_in_4bit": True,
    "skip_prepare_dataset": True,
    "remove_unused_columns": False,
    "sample_packing": False,
    "eot_tokens": ["<|eot|>"],
    "datasets": [{"path": "/vol/data/train.jsonl", "ds_type": "json", "type": "chat_template", "field_messages": "messages"}],
    "test_datasets": [{"path": "/vol/data/eval.jsonl", "ds_type": "json", "type": "chat_template", "field_messages": "messages", "split": "train"}],
    "output_dir": f"/vol/outputs/{RUN}",
    "adapter": "qlora",
    "sequence_len": 4096,
    "pad_to_sequence_len": False,
    "lora_r": 32,
    "lora_alpha": 16,
    "lora_dropout": 0.05,
    # Language layers only: the vision tower, adapter and projection stay frozen.
    "lora_target_modules": r"model\.language_model\.layers\.\d+\.(self_attn\.(q|k|v|o)_proj|mlp\.(gate|up|down)_proj)",
    "gradient_accumulation_steps": 4,
    "micro_batch_size": 1,
    "num_epochs": 2,
    "optimizer": "adamw_bnb_8bit",
    "lr_scheduler": "cosine",
    "learning_rate": 0.0002,
    "warmup_ratio": 0.1,
    "weight_decay": 0.0,
    "bf16": "auto",
    "tf32": True,
    "gradient_checkpointing": True,
    "attn_implementation": "flash_attention_2",
    "logging_steps": 1,
    "evals_per_epoch": 1,
    "saves_per_epoch": 1,
    "seed": 20261028,
}


def convert(row: dict) -> dict:
    """OpenAI `image_url` data URLs -> Axolotl `{"type": "image", "base64": ...}` parts, order kept.

    String content becomes a one-part text list. HF datasets stores mixed
    content as a Json column and decodes it on read, so a bare string that is
    itself JSON (every assistant answer here) comes back as a dict, the chat
    template drops that turn, and every label is masked.
    """
    msgs = []
    for m in row["messages"]:
        content = m["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        else:
            parts = []
            for p in content:
                if p["type"] == "image_url":
                    url = p["image_url"]["url"] if isinstance(p["image_url"], dict) else p["image_url"]
                    parts.append({"type": "image", "base64": url.split(",", 1)[1]})
                else:
                    parts.append(p)
            content = parts
        msgs.append({"role": m["role"], "content": content})
    return {"messages": msgs}


@app.local_entrypoint()
def prep():
    out = Path("/tmp/bills-sft")
    out.mkdir(exist_ok=True)
    for split, name in FILES.items():
        rows = [json.loads(l) for l in (DOWNLOADS / name).read_text().splitlines() if l.strip()]
        (out / f"{split}.jsonl").write_text("".join(json.dumps(convert(r)) + "\n" for r in rows))
        print(f"{split}: {len(rows)} rows")
    with vol.batch_upload(force=True) as up:
        for split in FILES:
            up.put_file(out / f"{split}.jsonl", f"/data/{split}.jsonl")
    print("uploaded to volume bills-sft:/data/")


@app.function(image=image, gpu="H100", volumes={"/vol": vol}, timeout=6 * 3600)
def train_remote():
    import subprocess

    import yaml

    cfg = Path(f"/vol/outputs/{RUN}.yaml")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump(CONFIG, sort_keys=False))
    try:
        subprocess.run(["axolotl", "train", str(cfg)], check=True)
    finally:
        vol.commit()


@app.function(image=image, cpu=4, memory=16384, volumes={"/vol": vol}, timeout=1800)
def check_labels(n: int = 900) -> dict:
    """Load the data the way training does and count the tokens each row trains on (CPU only)."""
    from datasets import load_dataset
    from transformers import AutoProcessor
    from axolotl.processing_strategies import get_processing_strategy
    from axolotl.utils.collators.mm_chat import MultiModalChatDataCollator

    proc = AutoProcessor.from_pretrained(BASE_MODEL, revision=BASE_REVISION)
    strategy = get_processing_strategy(proc, None, "tokenizer_default", model_type="muse_glimmer")
    collate = MultiModalChatDataCollator(tokenizer=proc.tokenizer, processing_strategy=strategy)
    report = {}
    for split in FILES:
        ds = load_dataset("json", data_files=f"/vol/data/{split}.jsonl", split="train")
        rows = min(n, len(ds))
        lengths, kept = [], []
        for i in range(rows):
            b = collate([ds[i]])
            lengths.append(int(b["input_ids"].shape[1]))
            kept.append(int((b["labels"] != -100).sum()))
        b = collate([ds[0]])
        report[split] = {
            "rows": rows,
            "rows_with_no_trained_tokens": sum(k == 0 for k in kept),
            "trained_tokens_min_max": [min(kept), max(kept)],
            "seq_len_max": max(lengths),
            "row0_trained_text": proc.tokenizer.decode(b["input_ids"][0][b["labels"][0] != -100])[:300],
        }
    return report


@app.local_entrypoint()
def check(n: int = 900):
    print(json.dumps(check_labels.remote(n), indent=2))


@app.local_entrypoint()
def train():
    report = check_labels.remote(900)
    print(json.dumps(report, indent=2))
    for split, r in report.items():
        if r["rows_with_no_trained_tokens"] or r["seq_len_max"] > CONFIG["sequence_len"]:
            raise SystemExit(f"{split}: labels or lengths are wrong; not starting the GPU run")
    train_remote.remote()
