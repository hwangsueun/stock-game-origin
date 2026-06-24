#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.2 단위 버그 정정분 재생성 배치 러너.

DART 공시 단위(천원/원) 처리 버그 수정 후, detail_source_facts가 바뀐 271건만
정정된 요청으로 다시 생성한다. 30건 청크 순차 제출(OpenAI Batch enqueued token 한도).

사용법:
  cd "/Users/hgs/Desktop/IISE-CD/data-pipeline/news_generator"
  python scripts/processors/run_v4_2_regen_unit_fix.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parents[2]  # news_generator/
REGEN_DIR = BASE_DIR / "data/interim/pr06a_regen_v4_2_unit_fix"
REGEN_REQUESTS = REGEN_DIR / "regen_requests.jsonl"
WORK_DIR = REGEN_DIR / "chunks_30"
MERGED_OUTPUT = REGEN_DIR / "regen_outputs.jsonl"
MERGED_ERROR = REGEN_DIR / "regen_errors.jsonl"
ENV_FILE = BASE_DIR / ".env"

CHUNK_SIZE = 30
POLL_SECONDS = 60
ENDPOINT = "/v1/chat/completions"
COMPLETION_WINDOW = "24h"
METADATA_PREFIX = "pr06a_v4_2_regen_unit_fix"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}


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


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def response_to_dict(obj: Any) -> dict:
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


def submit_batch(client: Any, state: ChunkState) -> ChunkState:
    input_path = Path(state.input_jsonl)
    with input_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="batch")
    file_id = uploaded.id
    batch = client.batches.create(
        input_file_id=file_id,
        endpoint=ENDPOINT,
        completion_window=COMPLETION_WINDOW,
        metadata={
            "job": METADATA_PREFIX,
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


def wait_for_batch(client: Any, state: ChunkState) -> ChunkState:
    while True:
        state = retrieve_batch(client, state)
        print(
            f"[{now_iso()}] chunk={state.chunk_no} lines={state.start_line}-{state.end_line} "
            f"status={state.status} batch_id={state.batch_id} error_code={state.error_code or ''}"
        )
        if state.status in TERMINAL_STATUSES:
            return state
        time.sleep(max(5, POLL_SECONDS))


def download_file_content(client: Any, file_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = client.files.content(file_id)
    if hasattr(content, "write_to_file"):
        content.write_to_file(str(out_path))
        return
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


def merge_all_outputs() -> None:
    state_files = sorted(WORK_DIR.glob("*_state.json"))
    out_count = err_count = 0
    with MERGED_OUTPUT.open("w", encoding="utf-8") as out_f:
        for sf in state_files:
            d = read_json(sf)
            out_path = Path(d["output_jsonl"])
            if out_path.exists():
                for line in out_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        out_f.write(line + "\n")
                        out_count += 1
    with MERGED_ERROR.open("w", encoding="utf-8") as err_f:
        for sf in state_files:
            d = read_json(sf)
            err_path = Path(d["error_jsonl"])
            if err_path.exists():
                for line in err_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        err_f.write(line + "\n")
                        err_count += 1
    print(f"[merge] output_lines={out_count} error_lines={err_count}")
    print(f"  -> {MERGED_OUTPUT}")
    print(f"  -> {MERGED_ERROR}")


def main() -> None:
    load_env(ENV_FILE)
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai 패키지가 없습니다. pip install openai")
    client = OpenAI()

    if not REGEN_REQUESTS.exists():
        sys.exit(f"요청 JSONL 없음: {REGEN_REQUESTS}")
    all_lines = [ln for ln in REGEN_REQUESTS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    total = len(all_lines)
    print(f"[init] 재생성 요청: {total}건")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    chunks: List[ChunkState] = []
    for start in range(0, total, CHUNK_SIZE):
        end = min(start + CHUNK_SIZE, total)
        chunk_no = (start // CHUNK_SIZE) + 1
        stem = f"chunk_{chunk_no:04d}_{start+1:06d}_{end:06d}"
        cs = ChunkState(
            chunk_no=chunk_no,
            start_line=start + 1,
            end_line=end,
            chunk_size=end - start,
            input_jsonl=str(WORK_DIR / f"{stem}_input.jsonl"),
            output_jsonl=str(WORK_DIR / f"{stem}_output.jsonl"),
            error_jsonl=str(WORK_DIR / f"{stem}_errors.jsonl"),
            state_json=str(WORK_DIR / f"{stem}_state.json"),
        )
        # input 작성
        input_path = Path(cs.input_jsonl)
        if not input_path.exists():
            input_path.write_text("\n".join(all_lines[start:end]) + "\n", encoding="utf-8")
        # 기존 state 로드(재개)
        state_path = Path(cs.state_json)
        if state_path.exists():
            d = read_json(state_path)
            for k, v in d.items():
                if hasattr(cs, k):
                    setattr(cs, k, v)
        else:
            write_json(state_path, asdict(cs))
        chunks.append(cs)
    print(f"[init] 전체 청크: {len(chunks)}개")

    for cs in chunks:
        print("=" * 80)
        if cs.status == "completed":
            print(f"[skip] chunk={cs.chunk_no} (already completed)")
            continue
        if cs.batch_id and cs.status not in TERMINAL_STATUSES:
            print(f"[resume] chunk={cs.chunk_no} 기존 batch_id={cs.batch_id} 상태 확인...")
        else:
            print(f"[submit] chunk={cs.chunk_no} lines={cs.start_line}-{cs.end_line}")
            cs = submit_batch(client, cs)
        cs = wait_for_batch(client, cs)
        if cs.status == "completed":
            cs = download_batch_outputs(client, cs)
            print(f"[done] chunk={cs.chunk_no} output={cs.output_jsonl}")
        else:
            print(f"[ERROR] chunk={cs.chunk_no} status={cs.status} error_code={cs.error_code}")
            print(f"  message: {cs.error_message}")
            sys.exit(1)

    print("=" * 80)
    print("[merge] 모든 청크 완료. 병합 시작...")
    merge_all_outputs()
    print("[완료] 재생성 배치 처리 완료.")


if __name__ == "__main__":
    main()
