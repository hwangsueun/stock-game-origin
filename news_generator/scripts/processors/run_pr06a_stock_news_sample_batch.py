from __future__ import annotations

import argparse
import time
from pathlib import Path
from openai import OpenAI


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-jsonl",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests/stock_news_sample_requests.jsonl",
    )
    parser.add_argument(
        "--output-jsonl",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/stock_news_sample_outputs.jsonl",
    )
    parser.add_argument(
        "--batch-id-file",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/batch_id.txt",
    )
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--poll-interval-sec", type=int, default=30)
    args = parser.parse_args()

    input_jsonl = Path(args.input_jsonl)
    output_jsonl = Path(args.output_jsonl)
    batch_id_file = Path(args.batch_id_file)

    if not input_jsonl.exists():
        raise FileNotFoundError(input_jsonl)

    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    client = OpenAI()

    print("[upload]", input_jsonl)
    uploaded = client.files.create(
        file=input_jsonl.open("rb"),
        purpose="batch",
    )
    print("[uploaded file_id]", uploaded.id)

    print("[create batch]")
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"job": "pr06a_stock_news_sample"},
    )
    print("[batch_id]", batch.id)
    print("[status]", batch.status)

    batch_id_file.write_text(batch.id, encoding="utf-8")
    print("[saved batch_id]", batch_id_file)

    if not args.poll:
        print("Batch submitted. Re-run with --poll or use retrieve script later.")
        return

    while True:
        batch = client.batches.retrieve(batch.id)
        print("[status]", batch.status, "completed:", batch.request_counts.completed, "failed:", batch.request_counts.failed)

        if batch.status in {"completed", "failed", "expired", "cancelled"}:
            break

        time.sleep(args.poll_interval_sec)

    if batch.status != "completed":
        print("[not completed]")
        print(batch)
        return

    if not batch.output_file_id:
        raise RuntimeError("Batch completed but output_file_id is empty.")

    print("[download output]", batch.output_file_id)
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
