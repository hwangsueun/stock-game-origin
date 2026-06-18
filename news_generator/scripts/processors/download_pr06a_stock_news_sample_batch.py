from __future__ import annotations

import argparse
import time
from pathlib import Path
from openai import OpenAI


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--batch-id-file",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/batch_id.txt",
    )
    parser.add_argument(
        "--output-jsonl",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/stock_news_sample_outputs.jsonl",
    )
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--poll-interval-sec", type=int, default=30)
    args = parser.parse_args()

    batch_id_file = Path(args.batch_id_file)
    output_jsonl = Path(args.output_jsonl)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    if not batch_id_file.exists():
        raise FileNotFoundError(f"batch_id_file not found: {batch_id_file}")

    batch_id = batch_id_file.read_text(encoding="utf-8").strip()
    if not batch_id:
        raise ValueError(f"batch_id_file is empty: {batch_id_file}")

    client = OpenAI()

    while True:
        batch = client.batches.retrieve(batch_id)
        print("[batch_id]", batch.id)
        print("[status]", batch.status)
        print("[request_counts]", batch.request_counts)

        if batch.status in {"completed", "failed", "expired", "cancelled"}:
            break

        if not args.wait:
            return

        time.sleep(args.poll_interval_sec)

    if batch.status != "completed":
        print("[not completed]")
        print(batch)
        return

    if not batch.output_file_id:
        raise RuntimeError("Batch completed but output_file_id is empty.")

    content = client.files.content(batch.output_file_id)
    output_jsonl.write_text(content.text, encoding="utf-8")
    print("[saved output]", output_jsonl)

    if batch.error_file_id:
        error_path = output_jsonl.with_name("stock_news_sample_errors.jsonl")
        error_content = client.files.content(batch.error_file_id)
        error_path.write_text(error_content.text, encoding="utf-8")
        print("[saved errors]", error_path)


if __name__ == "__main__":
    main()
