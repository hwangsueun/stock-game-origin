#!/usr/bin/env python3
"""Extract only requests whose writer payload changed due to market context."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-jsonl", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    selected = []
    for line in args.requests_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        request = json.loads(line)
        payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
        if payload.get("market_context"):
            selected.append(request)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for request in selected:
            f.write(json.dumps(request, ensure_ascii=False) + "\n")
    print(f"[done] context regeneration requests={len(selected)} -> {args.out}")


if __name__ == "__main__":
    main()
