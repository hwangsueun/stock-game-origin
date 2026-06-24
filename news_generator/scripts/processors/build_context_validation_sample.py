#!/usr/bin/env python3
"""Build a deterministic integrated-context validation request sample."""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def context_kind(request: dict) -> str:
    payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
    context = payload.get("market_context") or {}
    if context.get("gdelt_context"):
        return "gdelt"
    if context:
        return "market"
    return "control"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-jsonl", type=Path, required=True)
    ap.add_argument("--output-jsonl", type=Path, required=True)
    ap.add_argument("--market-count", type=int, default=30)
    ap.add_argument("--control-count", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260619)
    args = ap.parse_args()

    buckets = {"gdelt": [], "market": [], "control": []}
    for line in args.input_jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            request = json.loads(line)
            buckets[context_kind(request)].append(request)

    rng = random.Random(args.seed)
    selected = list(buckets["gdelt"])
    selected += rng.sample(buckets["market"], min(args.market_count, len(buckets["market"])))
    selected += rng.sample(buckets["control"], min(args.control_count, len(buckets["control"])))
    rng.shuffle(selected)

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as f:
        for request in selected:
            f.write(json.dumps(request, ensure_ascii=False) + "\n")
    print(f"[done] selected={len(selected)} gdelt={len(buckets['gdelt'])} "
          f"market={min(args.market_count, len(buckets['market']))} "
          f"control={min(args.control_count, len(buckets['control']))} -> {args.output_jsonl}")


if __name__ == "__main__":
    main()
