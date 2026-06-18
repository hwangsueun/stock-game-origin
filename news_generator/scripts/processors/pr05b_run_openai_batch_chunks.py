#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run OpenAI Batch requests in small sequential chunks.

Purpose
-------
This script is for cases where a full OpenAI Batch JSONL exceeds the organization's
"enqueued token limit". It splits the request JSONL into chunks, submits only one
batch at a time, waits for completion, downloads the output/error files, then moves
on to the next chunk.

Typical usage
-------------
python scripts/processors/pr05b_run_openai_batch_chunks.py run \
  --input-jsonl data/processed/gdelt_context/gdelt_stock_context_cards_test_v3_openai_requests_all.jsonl \
  --work-dir data/processed/gdelt_context/batch_chunks_auto \
  --merged-output-jsonl data/processed/gdelt_context/gdelt_stock_context_cards_test_v3_batch_output_all.jsonl \
  --merged-error-jsonl data/processed/gdelt_context/gdelt_stock_context_cards_test_v3_batch_errors_all.jsonl \
  --initial-chunk-size 50 \
  --min-chunk-size 10 \
  --poll-seconds 60

Environment
-----------
Set OPENAI_API_KEY in your shell or pass --env-file .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
RETRYABLE_BATCH_ERROR_CODES = {"token_limit_exceeded"}


@dataclass
class ChunkState:
    chunk_no: int
    start_line: int
    end_line: int
    chunk_size: int
    input_jsonl: str
    output_jsonl: str
    error_jsonl: str
    state_json: str
    status: str = "created"
    file_id: Optional[str] = None
    batch_id: Optional[str] = None
    output_file_id: Optional[str] = None
    error_file_id: Optional[str] = None
    created_at: Optional[int] = None
    completed_at: Optional[int] = None
    failed_at: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env_file(path: Optional[Path]) -> None:
    if not path:
        return
    if not path.exists():
        raise FileNotFoundError(f"env file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_openai_client():
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError(
            "openai package is not installed. Install with: pip install openai"
        ) from exc
    return OpenAI()


def read_jsonl_lines(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"input jsonl not found: {path}")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"input jsonl is empty: {path}")
    return lines


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def response_to_dict(obj: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return obj
    return json.loads(json.dumps(obj, default=lambda x: getattr(x, "__dict__", str(x))))


def extract_first_batch_error(batch: Any) -> Tuple[Optional[str], Optional[str]]:
    d = response_to_dict(batch)
    errors = d.get("errors")
    if not errors:
        return None, None
    data = errors.get("data") if isinstance(errors, dict) else None
    if not data:
        return None, str(errors)
    first = data[0]
    return first.get("code"), first.get("message")


def validate_requests(lines: List[str]) -> Dict[str, Any]:
    methods = {}
    urls = {}
    models = {}
    custom_ids = set()
    dupes = 0
    bad = []
    for idx, line in enumerate(lines, start=1):
        try:
            obj = json.loads(line)
        except Exception as exc:
            bad.append((idx, f"invalid json: {exc}"))
            continue
        method = obj.get("method")
        url = obj.get("url")
        body = obj.get("body") or {}
        custom_id = obj.get("custom_id")
        model = body.get("model")
        methods[method] = methods.get(method, 0) + 1
        urls[url] = urls.get(url, 0) + 1
        models[model] = models.get(model, 0) + 1
        if custom_id in custom_ids:
            dupes += 1
        custom_ids.add(custom_id)
        if method != "POST":
            bad.append((idx, f"method should be POST, got {method}"))
        if not isinstance(body, dict):
            bad.append((idx, "body is not object"))
        if not custom_id:
            bad.append((idx, "missing custom_id"))
    return {
        "line_count": len(lines),
        "methods": methods,
        "urls": urls,
        "models": models,
        "duplicate_custom_ids": dupes,
        "bad_count": len(bad),
        "bad_examples": bad[:10],
    }


def make_chunk_files(
    lines: List[str],
    work_dir: Path,
    chunk_size: int,
    prefix: str = "chunk",
) -> List[ChunkState]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    work_dir.mkdir(parents=True, exist_ok=True)
    states: List[ChunkState] = []
    for start in range(0, len(lines), chunk_size):
        end = min(start + chunk_size, len(lines))
        chunk_no = len(states) + 1
        stem = f"{prefix}_{chunk_no:04d}_{start+1:06d}_{end:06d}"
        input_path = work_dir / f"{stem}_input.jsonl"
        output_path = work_dir / f"{stem}_output.jsonl"
        error_path = work_dir / f"{stem}_errors.jsonl"
        state_path = work_dir / f"{stem}_state.json"
        input_path.write_text("\n".join(lines[start:end]) + "\n", encoding="utf-8")
        state = ChunkState(
            chunk_no=chunk_no,
            start_line=start + 1,
            end_line=end,
            chunk_size=end - start,
            input_jsonl=str(input_path),
            output_jsonl=str(output_path),
            error_jsonl=str(error_path),
            state_json=str(state_path),
        )
        write_json(state_path, asdict(state))
        states.append(state)
    return states


def submit_batch(
    client: Any,
    state: ChunkState,
    endpoint: str,
    completion_window: str,
    metadata_prefix: str,
) -> ChunkState:
    input_path = Path(state.input_jsonl)
    with input_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="batch")
    file_id = uploaded.id

    batch = client.batches.create(
        input_file_id=file_id,
        endpoint=endpoint,
        completion_window=completion_window,
        metadata={
            "job": metadata_prefix,
            "chunk_no": str(state.chunk_no),
            "start_line": str(state.start_line),
            "end_line": str(state.end_line),
        },
    )
    bd = response_to_dict(batch)
    state.file_id = file_id
    state.batch_id = bd.get("id")
    state.status = bd.get("status", "submitted")
    state.created_at = bd.get("created_at")
    write_json(Path(state.state_json), asdict(state))
    return state


def retrieve_batch(client: Any, state: ChunkState) -> ChunkState:
    if not state.batch_id:
        raise ValueError("batch_id missing")
    batch = client.batches.retrieve(state.batch_id)
    bd = response_to_dict(batch)
    state.status = bd.get("status", state.status)
    state.output_file_id = bd.get("output_file_id")
    state.error_file_id = bd.get("error_file_id")
    state.completed_at = bd.get("completed_at")
    state.failed_at = bd.get("failed_at")
    code, msg = extract_first_batch_error(batch)
    state.error_code = code
    state.error_message = msg
    write_json(Path(state.state_json), asdict(state))
    return state


def print_state(state: ChunkState) -> None:
    print(
        f"[{now_iso()}] chunk={state.chunk_no} lines={state.start_line}-{state.end_line} "
        f"size={state.chunk_size} status={state.status} batch_id={state.batch_id} "
        f"error_code={state.error_code or ''}"
    )
    if state.error_message:
        print(f"  error_message: {state.error_message}")


def wait_for_batch(client: Any, state: ChunkState, poll_seconds: int) -> ChunkState:
    while True:
        state = retrieve_batch(client, state)
        print_state(state)
        if state.status in TERMINAL_STATUSES:
            return state
        time.sleep(max(5, poll_seconds))


def download_file_content(client: Any, file_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = client.files.content(file_id)
    # New SDK returns an HttpxBinaryResponseContent-like object with write_to_file.
    if hasattr(content, "write_to_file"):
        content.write_to_file(str(out_path))
        return
    # Fallbacks.
    if hasattr(content, "text"):
        out_path.write_text(content.text, encoding="utf-8")
        return
    if isinstance(content, bytes):
        out_path.write_bytes(content)
        return
    out_path.write_text(str(content), encoding="utf-8")


def download_batch_outputs(client: Any, state: ChunkState) -> ChunkState:
    state = retrieve_batch(client, state)
    if state.status != "completed":
        raise RuntimeError(f"cannot download non-completed batch: {state.status}")
    if state.output_file_id:
        download_file_content(client, state.output_file_id, Path(state.output_jsonl))
    else:
        Path(state.output_jsonl).write_text("", encoding="utf-8")
    if state.error_file_id:
        download_file_content(client, state.error_file_id, Path(state.error_jsonl))
    else:
        Path(state.error_jsonl).write_text("", encoding="utf-8")
    write_json(Path(state.state_json), asdict(state))
    return state


def append_file(src: Path, dst_handle: Any) -> int:
    if not src.exists():
        return 0
    count = 0
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                dst_handle.write(line if line.endswith("\n") else line + "\n")
                count += 1
    return count


def merge_outputs(work_dir: Path, merged_output: Path, merged_error: Path) -> Dict[str, int]:
    states = sorted(work_dir.glob("*_state.json"))
    merged_output.parent.mkdir(parents=True, exist_ok=True)
    merged_error.parent.mkdir(parents=True, exist_ok=True)
    out_count = 0
    err_count = 0
    with merged_output.open("w", encoding="utf-8") as out_f:
        for state_path in states:
            data = read_json(state_path)
            out_count += append_file(Path(data["output_jsonl"]), out_f)
    with merged_error.open("w", encoding="utf-8") as err_f:
        for state_path in states:
            data = read_json(state_path)
            err_count += append_file(Path(data["error_jsonl"]), err_f)
    return {"output_lines": out_count, "error_lines": err_count, "state_files": len(states)}


def run_sequential(args: argparse.Namespace) -> None:
    load_env_file(Path(args.env_file) if args.env_file else None)
    client = get_openai_client()

    input_path = Path(args.input_jsonl)
    work_dir = Path(args.work_dir)
    merged_output = Path(args.merged_output_jsonl)
    merged_error = Path(args.merged_error_jsonl)

    lines = read_jsonl_lines(input_path)
    validation = validate_requests(lines)
    print("=" * 100)
    print("[request validation]")
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    if validation["bad_count"]:
        raise ValueError("request jsonl validation failed")

    remaining_start = 0
    chunk_size = int(args.initial_chunk_size)
    min_chunk_size = int(args.min_chunk_size)
    completed_states: List[ChunkState] = []
    attempt_no = 0

    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "manifest.json"
    write_json(manifest_path, {
        "input_jsonl": str(input_path),
        "total_lines": len(lines),
        "initial_chunk_size": chunk_size,
        "min_chunk_size": min_chunk_size,
        "started_at": now_iso(),
    })

    while remaining_start < len(lines):
        attempt_no += 1
        end = min(remaining_start + chunk_size, len(lines))
        chunk_no = len(completed_states) + 1
        stem = f"chunk_{chunk_no:04d}_{remaining_start+1:06d}_{end:06d}"
        state = ChunkState(
            chunk_no=chunk_no,
            start_line=remaining_start + 1,
            end_line=end,
            chunk_size=end - remaining_start,
            input_jsonl=str(work_dir / f"{stem}_input.jsonl"),
            output_jsonl=str(work_dir / f"{stem}_output.jsonl"),
            error_jsonl=str(work_dir / f"{stem}_errors.jsonl"),
            state_json=str(work_dir / f"{stem}_state.json"),
        )
        Path(state.input_jsonl).write_text("\n".join(lines[remaining_start:end]) + "\n", encoding="utf-8")
        write_json(Path(state.state_json), asdict(state))

        print("=" * 100)
        print(f"[submit chunk] chunk={chunk_no} lines={state.start_line}-{state.end_line} size={state.chunk_size}")
        state = submit_batch(
            client=client,
            state=state,
            endpoint=args.endpoint,
            completion_window=args.completion_window,
            metadata_prefix=args.metadata_prefix,
        )
        print_state(state)

        state = wait_for_batch(client, state, args.poll_seconds)

        if state.status == "completed":
            state = download_batch_outputs(client, state)
            print(f"[downloaded] output={state.output_jsonl} errors={state.error_jsonl}")
            completed_states.append(state)
            remaining_start = end
            continue

        if state.status == "failed" and state.error_code in RETRYABLE_BATCH_ERROR_CODES:
            if chunk_size <= min_chunk_size:
                raise RuntimeError(
                    f"Batch failed with retryable error {state.error_code}, but chunk_size={chunk_size} "
                    f"is already <= min_chunk_size={min_chunk_size}. Wait for existing batches to clear "
                    "or reduce prompt/token size."
                )
            new_chunk_size = max(min_chunk_size, chunk_size // 2)
            print("=" * 100)
            print(
                f"[retryable failure] {state.error_code}. Reducing chunk_size {chunk_size} -> {new_chunk_size} "
                f"and retrying from line {remaining_start + 1}."
            )
            chunk_size = new_chunk_size
            continue

        raise RuntimeError(
            f"Batch ended with status={state.status}, error_code={state.error_code}, "
            f"message={state.error_message}"
        )

    stats = merge_outputs(work_dir, merged_output, merged_error)
    manifest = read_json(manifest_path)
    manifest.update({
        "completed_at": now_iso(),
        "merged_output_jsonl": str(merged_output),
        "merged_error_jsonl": str(merged_error),
        "merge_stats": stats,
    })
    write_json(manifest_path, manifest)

    print("=" * 100)
    print("[all chunks completed]")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"merged_output_jsonl: {merged_output}")
    print(f"merged_error_jsonl: {merged_error}")


def cmd_split(args: argparse.Namespace) -> None:
    lines = read_jsonl_lines(Path(args.input_jsonl))
    states = make_chunk_files(lines, Path(args.work_dir), args.chunk_size, prefix=args.prefix)
    print("=" * 100)
    print("[split complete]")
    print(f"input_jsonl: {args.input_jsonl}")
    print(f"work_dir: {args.work_dir}")
    print(f"chunk_size: {args.chunk_size}")
    print(f"chunks: {len(states)}")
    for st in states[:10]:
        print(f"chunk={st.chunk_no} lines={st.start_line}-{st.end_line} input={st.input_jsonl}")
    if len(states) > 10:
        print("...")


def cmd_merge_outputs(args: argparse.Namespace) -> None:
    stats = merge_outputs(Path(args.work_dir), Path(args.merged_output_jsonl), Path(args.merged_error_jsonl))
    print("=" * 100)
    print("[merge outputs complete]")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run OpenAI Batch requests sequentially in small chunks.")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Split, submit, wait, download, and merge sequentially.")
    run.add_argument("--input-jsonl", required=True)
    run.add_argument("--work-dir", required=True)
    run.add_argument("--merged-output-jsonl", required=True)
    run.add_argument("--merged-error-jsonl", required=True)
    run.add_argument("--initial-chunk-size", type=int, default=50)
    run.add_argument("--min-chunk-size", type=int, default=10)
    run.add_argument("--poll-seconds", type=int, default=60)
    run.add_argument("--endpoint", default="/v1/chat/completions")
    run.add_argument("--completion-window", default="24h")
    run.add_argument("--metadata-prefix", default="pr05b_gdelt_context_judge")
    run.add_argument("--env-file", default=None)
    run.set_defaults(func=run_sequential)

    sp = sub.add_parser("split", help="Only split a request JSONL into chunk files.")
    sp.add_argument("--input-jsonl", required=True)
    sp.add_argument("--work-dir", required=True)
    sp.add_argument("--chunk-size", type=int, default=50)
    sp.add_argument("--prefix", default="chunk")
    sp.set_defaults(func=cmd_split)

    mg = sub.add_parser("merge-outputs", help="Merge already downloaded chunk outputs/errors.")
    mg.add_argument("--work-dir", required=True)
    mg.add_argument("--merged-output-jsonl", required=True)
    mg.add_argument("--merged-error-jsonl", required=True)
    mg.set_defaults(func=cmd_merge_outputs)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
