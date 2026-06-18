#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr05b_run_openai_batch.py

OpenAI Batch runner for pr05b LLM context judge.

Typical flow:
  1) submit   : upload request JSONL and create a batch
  2) status   : check batch status
  3) download : download output/error JSONL after completion

Input JSONL must already be in OpenAI Batch request format, e.g. each line:
{
  "custom_id": "...",
  "method": "POST",
  "url": "/v1/chat/completions",
  "body": {...}
}

Environment:
  export OPENAI_API_KEY="..."

Optional:
  pip install openai python-dotenv
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
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None

try:
    from openai import OpenAI
except Exception as exc:  # pragma: no cover
    OpenAI = None  # type: ignore
    _OPENAI_IMPORT_ERROR = exc
else:
    _OPENAI_IMPORT_ERROR = None


TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}
SUCCESS_STATUS = "completed"


@dataclass
class BatchRunState:
    batch_id: str
    input_jsonl: str
    uploaded_file_id: str
    endpoint: str
    completion_window: str
    created_at_utc: str
    output_jsonl: Optional[str] = None
    error_jsonl: Optional[str] = None
    last_status: Optional[str] = None
    output_file_id: Optional[str] = None
    error_file_id: Optional[str] = None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_env(env_file: Optional[str]) -> None:
    if env_file:
        if load_dotenv is None:
            raise RuntimeError("python-dotenv is not installed. Run: pip install python-dotenv")
        load_dotenv(env_file)
    elif load_dotenv is not None:
        # Load .env if present in current/project directory. No error if absent.
        load_dotenv()


def get_client(env_file: Optional[str] = None) -> OpenAI:
    load_env(env_file)
    if OpenAI is None:
        raise RuntimeError(f"openai package import failed: {_OPENAI_IMPORT_ERROR}\nRun: pip install openai")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set. Run: export OPENAI_API_KEY='...' or provide --env-file .env")
    return OpenAI()


def object_to_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    try:
        return dict(obj)
    except Exception:
        return json.loads(json.dumps(obj, default=str))


def read_state(path: Path) -> BatchRunState:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return BatchRunState(**data)


def write_state(path: Path, state: BatchRunState) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)
        f.write("\n")


def validate_jsonl(path: Path, max_preview_errors: int = 5) -> int:
    if not path.exists():
        raise FileNotFoundError(path)
    n = 0
    errors = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                obj = json.loads(line)
            except Exception as exc:
                errors.append(f"line {lineno}: invalid JSON: {exc}")
                continue
            missing = [k for k in ("custom_id", "method", "url", "body") if k not in obj]
            if missing:
                errors.append(f"line {lineno}: missing keys {missing}")
            if obj.get("method") != "POST":
                errors.append(f"line {lineno}: method is not POST: {obj.get('method')}")
            if not str(obj.get("url", "")).startswith("/v1/"):
                errors.append(f"line {lineno}: url should start with /v1/: {obj.get('url')}")
            if len(errors) >= max_preview_errors:
                break
    if errors:
        raise ValueError("Batch request JSONL validation failed:\n" + "\n".join(errors))
    if n == 0:
        raise ValueError(f"No JSONL rows found: {path}")
    return n


def response_content_to_bytes(resp: Any) -> bytes:
    # openai-python returns different response wrappers across versions.
    if resp is None:
        return b""
    if isinstance(resp, bytes):
        return resp
    if isinstance(resp, str):
        return resp.encode("utf-8")
    if hasattr(resp, "read"):
        data = resp.read()
        if isinstance(data, str):
            return data.encode("utf-8")
        return data
    if hasattr(resp, "content"):
        data = resp.content
        if isinstance(data, str):
            return data.encode("utf-8")
        return data
    if hasattr(resp, "text"):
        return str(resp.text).encode("utf-8")
    return str(resp).encode("utf-8")


def print_batch_summary(batch: Any) -> None:
    b = object_to_dict(batch)
    keys = [
        "id", "status", "endpoint", "input_file_id", "output_file_id", "error_file_id",
        "completion_window", "created_at", "in_progress_at", "finalizing_at", "completed_at",
        "failed_at", "expired_at", "request_counts", "errors",
    ]
    print("=" * 100)
    print("[OpenAI Batch 상태]")
    for k in keys:
        if k in b and b[k] is not None:
            print(f"{k}: {b[k]}")
    print("=" * 100)


def cmd_validate(args: argparse.Namespace) -> None:
    path = Path(args.input_jsonl).expanduser().resolve()
    rows = validate_jsonl(path)
    print("=" * 100)
    print("[Batch request JSONL 검증 완료]")
    print(f"input_jsonl: {path}")
    print(f"rows: {rows:,}")
    print("=" * 100)


def cmd_submit(args: argparse.Namespace) -> None:
    input_path = Path(args.input_jsonl).expanduser().resolve()
    state_path = Path(args.state_json).expanduser().resolve()
    rows = validate_jsonl(input_path)

    client = get_client(args.env_file)

    print("=" * 100)
    print("[OpenAI Batch 제출 시작]")
    print(f"input_jsonl: {input_path}")
    print(f"rows: {rows:,}")
    print(f"endpoint: {args.endpoint}")
    print(f"completion_window: {args.completion_window}")
    print(f"state_json: {state_path}")

    with input_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="batch")
    uploaded_dict = object_to_dict(uploaded)
    uploaded_file_id = uploaded_dict.get("id") or getattr(uploaded, "id")
    print(f"uploaded_file_id: {uploaded_file_id}")

    metadata = {
        "project": "news_generator",
        "stage": "pr05b_llm_context_judge",
        "input_name": input_path.name[:128],
        "rows": str(rows),
    }
    if args.metadata:
        for item in args.metadata:
            if "=" not in item:
                raise ValueError(f"--metadata must be key=value, got: {item}")
            k, v = item.split("=", 1)
            metadata[k[:64]] = v[:512]

    batch = client.batches.create(
        input_file_id=uploaded_file_id,
        endpoint=args.endpoint,
        completion_window=args.completion_window,
        metadata=metadata,
    )
    batch_dict = object_to_dict(batch)
    batch_id = batch_dict.get("id") or getattr(batch, "id")
    status = batch_dict.get("status") or getattr(batch, "status", None)

    state = BatchRunState(
        batch_id=batch_id,
        input_jsonl=str(input_path),
        uploaded_file_id=uploaded_file_id,
        endpoint=args.endpoint,
        completion_window=args.completion_window,
        created_at_utc=now_utc_iso(),
        output_jsonl=str(Path(args.output_jsonl).expanduser().resolve()) if args.output_jsonl else None,
        error_jsonl=str(Path(args.error_jsonl).expanduser().resolve()) if args.error_jsonl else None,
        last_status=status,
        output_file_id=batch_dict.get("output_file_id"),
        error_file_id=batch_dict.get("error_file_id"),
    )
    write_state(state_path, state)

    print(f"batch_id: {batch_id}")
    print(f"status: {status}")
    print(f"state_saved: {state_path}")
    print("=" * 100)

    if args.wait:
        wait_for_batch(client, state_path, poll_seconds=args.poll_seconds, timeout_seconds=args.timeout_seconds)
        if args.output_jsonl:
            download_from_state(client, state_path, output_jsonl=Path(args.output_jsonl).expanduser().resolve(), error_jsonl=Path(args.error_jsonl).expanduser().resolve() if args.error_jsonl else None)


def wait_for_batch(client: OpenAI, state_path: Path, poll_seconds: int, timeout_seconds: Optional[int]) -> Any:
    state = read_state(state_path)
    started = time.time()
    while True:
        batch = client.batches.retrieve(state.batch_id)
        b = object_to_dict(batch)
        status = b.get("status")
        state.last_status = status
        state.output_file_id = b.get("output_file_id")
        state.error_file_id = b.get("error_file_id")
        write_state(state_path, state)

        print(f"[{now_utc_iso()}] batch_id={state.batch_id} status={status} request_counts={b.get('request_counts')}")

        if status in TERMINAL_STATUSES:
            print_batch_summary(batch)
            return batch
        if timeout_seconds is not None and time.time() - started > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for batch after {timeout_seconds} seconds: {state.batch_id}")
        time.sleep(max(5, poll_seconds))


def cmd_status(args: argparse.Namespace) -> None:
    state_path = Path(args.state_json).expanduser().resolve()
    state = read_state(state_path)
    client = get_client(args.env_file)
    batch = client.batches.retrieve(state.batch_id)
    b = object_to_dict(batch)
    state.last_status = b.get("status")
    state.output_file_id = b.get("output_file_id")
    state.error_file_id = b.get("error_file_id")
    write_state(state_path, state)
    print_batch_summary(batch)



def cmd_wait(args: argparse.Namespace) -> None:
    state_path = Path(args.state_json).expanduser().resolve()
    client = get_client(args.env_file)
    wait_for_batch(client, state_path, poll_seconds=args.poll_seconds, timeout_seconds=args.timeout_seconds)


def download_file(client: OpenAI, file_id: str, out_path: Path) -> int:
    ensure_parent(out_path)
    resp = client.files.content(file_id)
    data = response_content_to_bytes(resp)
    with out_path.open("wb") as f:
        f.write(data)
    return len(data)


def download_from_state(client: OpenAI, state_path: Path, output_jsonl: Optional[Path], error_jsonl: Optional[Path]) -> None:
    state = read_state(state_path)
    batch = client.batches.retrieve(state.batch_id)
    b = object_to_dict(batch)
    state.last_status = b.get("status")
    state.output_file_id = b.get("output_file_id")
    state.error_file_id = b.get("error_file_id")
    write_state(state_path, state)

    print_batch_summary(batch)

    if b.get("status") != SUCCESS_STATUS:
        raise RuntimeError(f"Batch is not completed. Current status={b.get('status')}. Use 'status' or 'wait' first.")

    output_file_id = b.get("output_file_id")
    error_file_id = b.get("error_file_id")

    if output_file_id:
        out_path = output_jsonl or (Path(state.output_jsonl) if state.output_jsonl else state_path.with_name(state_path.stem + "_output.jsonl"))
        nbytes = download_file(client, output_file_id, out_path)
        state.output_jsonl = str(out_path)
        print(f"downloaded_output: {out_path} ({nbytes:,} bytes)")
    else:
        print("output_file_id 없음")

    if error_file_id:
        err_path = error_jsonl or (Path(state.error_jsonl) if state.error_jsonl else state_path.with_name(state_path.stem + "_errors.jsonl"))
        nbytes = download_file(client, error_file_id, err_path)
        state.error_jsonl = str(err_path)
        print(f"downloaded_errors: {err_path} ({nbytes:,} bytes)")
    else:
        print("error_file_id 없음")

    write_state(state_path, state)


def cmd_download(args: argparse.Namespace) -> None:
    state_path = Path(args.state_json).expanduser().resolve()
    client = get_client(args.env_file)
    output_jsonl = Path(args.output_jsonl).expanduser().resolve() if args.output_jsonl else None
    error_jsonl = Path(args.error_jsonl).expanduser().resolve() if args.error_jsonl else None
    download_from_state(client, state_path, output_jsonl=output_jsonl, error_jsonl=error_jsonl)


def cmd_cancel(args: argparse.Namespace) -> None:
    state_path = Path(args.state_json).expanduser().resolve()
    state = read_state(state_path)
    client = get_client(args.env_file)
    batch = client.batches.cancel(state.batch_id)
    b = object_to_dict(batch)
    state.last_status = b.get("status")
    write_state(state_path, state)
    print_batch_summary(batch)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run OpenAI Batch for pr05b LLM context judge")
    p.add_argument("--env-file", default=None, help="Optional .env file containing OPENAI_API_KEY")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate", help="Validate OpenAI Batch request JSONL")
    sp.add_argument("--input-jsonl", required=True)
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("submit", help="Upload request JSONL and create OpenAI Batch")
    sp.add_argument("--input-jsonl", required=True)
    sp.add_argument("--state-json", required=True, help="Where to save batch id/state JSON")
    sp.add_argument("--endpoint", default="/v1/chat/completions")
    sp.add_argument("--completion-window", default="24h")
    sp.add_argument("--output-jsonl", default=None, help="Optional expected output path saved into state")
    sp.add_argument("--error-jsonl", default=None, help="Optional expected error output path saved into state")
    sp.add_argument("--metadata", action="append", default=[], help="Extra metadata key=value. Can repeat.")
    sp.add_argument("--wait", action="store_true", help="Wait until terminal status after submit")
    sp.add_argument("--poll-seconds", type=int, default=60)
    sp.add_argument("--timeout-seconds", type=int, default=None)
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser("status", help="Retrieve current batch status")
    sp.add_argument("--state-json", required=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("wait", help="Poll until batch reaches terminal status")
    sp.add_argument("--state-json", required=True)
    sp.add_argument("--poll-seconds", type=int, default=60)
    sp.add_argument("--timeout-seconds", type=int, default=None)
    sp.set_defaults(func=cmd_wait)

    sp = sub.add_parser("download", help="Download completed batch output/error files")
    sp.add_argument("--state-json", required=True)
    sp.add_argument("--output-jsonl", default=None)
    sp.add_argument("--error-jsonl", default=None)
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("cancel", help="Cancel a running batch")
    sp.add_argument("--state-json", required=True)
    sp.set_defaults(func=cmd_cancel)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        eprint("[ERROR]", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
