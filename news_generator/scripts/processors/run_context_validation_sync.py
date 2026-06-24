#!/usr/bin/env python3
"""Run a small context validation set through Chat Completions synchronously."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-jsonl", type=Path, required=True)
    ap.add_argument("--output-jsonl", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--resume", action="store_true", help="Keep successful existing rows and retry only errors/missing IDs")
    args = ap.parse_args()

    all_requests = [json.loads(line) for line in args.input_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = {}
    if args.resume and args.output_jsonl.exists():
        for line in args.output_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if "error" not in row:
                    kept[row["custom_id"]] = row
    requests = [request for request in all_requests if request["custom_id"] not in kept]
    client = OpenAI()

    def run_one(request: dict) -> dict:
        body = dict(request["body"])
        last_error = None
        for attempt in range(4):
            try:
                response = client.chat.completions.create(**body)
                return {"custom_id": request["custom_id"], "response": {"body": response.model_dump()}}
            except Exception as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        return {"custom_id": request["custom_id"], "error": str(last_error)}

    results = list(kept.values())
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, request): request["custom_id"] for request in requests}
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if i % 10 == 0 or i == len(futures):
                print(f"[progress] {i}/{len(futures)}")

    order = {request["custom_id"]: i for i, request in enumerate(all_requests)}
    results.sort(key=lambda row: order[row["custom_id"]])
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    errors = sum("error" in row for row in results)
    print(f"[done] requests={len(results)} kept={len(kept)} retried={len(requests)} "
          f"errors={errors} -> {args.output_jsonl}")


if __name__ == "__main__":
    main()
