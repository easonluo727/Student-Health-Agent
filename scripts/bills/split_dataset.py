"""Split the bill-reading SFT data into a training file and a held-out eval file.

The eval rows never go to training: they score the untrained base model, the
fine-tuned model and Muse Spark on the same bills.

    python scripts/bills/split_dataset.py ~/Downloads/bills-train-1000.jsonl --eval 100
"""
import argparse
import random
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--eval", type=int, default=100, help="rows held out for eval")
    ap.add_argument("--seed", type=int, default=20261028)
    args = ap.parse_args()

    lines = [l for l in args.src.read_text().splitlines() if l.strip()]
    idx = list(range(len(lines)))
    random.Random(args.seed).shuffle(idx)
    held = set(idx[: args.eval])

    stem = args.src.parent
    train_path = stem / f"bills-train-{len(lines) - len(held)}.jsonl"
    eval_path = stem / f"bills-eval-{len(held)}.jsonl"
    # Keep source order inside each file so rows are easy to trace back.
    train_path.write_text("".join(lines[i] + "\n" for i in range(len(lines)) if i not in held))
    eval_path.write_text("".join(lines[i] + "\n" for i in range(len(lines)) if i in held))
    print(f"train {len(lines) - len(held)} -> {train_path}")
    print(f"eval  {len(held)} -> {eval_path}")


if __name__ == "__main__":
    main()
