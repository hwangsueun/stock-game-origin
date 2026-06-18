#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4.2 배치 재개 스크립트 — chunk 33 (line 961)부터 순차 제출.

billing_hard_limit_reached로 중단된 이후 한도 해제 시 이 스크립트로 재개한다.

사용법:
  cd "/Users/hgs/Desktop/IISE CD/news_generator"
  python scripts/processors/run_v4_2_resume_from_chunk33.py
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

# ── 경로 설정 ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parents[2]  # news_generator/
FULL_REQUEST_JSONL = BASE_DIR / "data/interim/pr06a_full_requests_v4_2_all_stocks/stock_news_sample_requests.jsonl"
WORK_DIR = BASE_DIR / "data/interim/pr06a_full_outputs_v4_2_all_stocks/chunks_30"
MERGED_OUTPUT = BASE_DIR / "data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl"
MERGED_ERROR = BASE_DIR / "data/interim/pr06a_full_outputs_v4_2_all_stocks/errors_all.jsonl"
ENV_FILE = BASE_DIR / ".env"

CHUNK_SIZE = 30
POLL_SECONDS = 60
ENDPOINT = "/v1/chat/completions"
COMPLETION_WINDOW = "24h"
METADATA_PREFIX = "pr06a_v4_2_all_stocks"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}

# ── 데이터 클래스 ───────────────────────────────────────────────────────────────

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


# ── 유틸 ────────────────────────────────────────────────────────────────────────

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


# ── 배치 API 함수 ────────────────────────────────────────────────────────────────

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


# ── 병합 ────────────────────────────────────────────────────────────────────────

def merge_all_outputs() -> None:
    """완료된 모든 청크(기존 1-32 + 새로 완료된 33~)를 합쳐 outputs_all.jsonl 재생성."""
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


# ── 메인 ────────────────────────────────────────────────────────────────────────

def main() -> None:
    load_env(ENV_FILE)

    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai 패키지가 없습니다. pip install openai")

    client = OpenAI()

    # 전체 요청 줄 읽기
    if not FULL_REQUEST_JSONL.exists():
        sys.exit(f"요청 JSONL 없음: {FULL_REQUEST_JSONL}")
    all_lines = [ln for ln in FULL_REQUEST_JSONL.read_text(encoding="utf-8").splitlines() if ln.strip()]
    total = len(all_lines)
    print(f"[init] 전체 요청: {total}건")

    # 기존 state 파일 로드 (완료 여부 확인)
    existing_states: Dict[int, dict] = {}
    for sf in WORK_DIR.glob("*_state.json"):
        d = read_json(sf)
        existing_states[d["chunk_no"]] = d

    # 청크 목록 생성 (전체)
    chunks_needed: List[ChunkState] = []
    for start in range(0, total, CHUNK_SIZE):
        end = min(start + CHUNK_SIZE, total)
        chunk_no = (start // CHUNK_SIZE) + 1
        stem = f"chunk_{chunk_no:04d}_{start+1:06d}_{end:06d}"
        state = ChunkState(
            chunk_no=chunk_no,
            start_line=start + 1,
            end_line=end,
            chunk_size=end - start,
            input_jsonl=str(WORK_DIR / f"{stem}_input.jsonl"),
            output_jsonl=str(WORK_DIR / f"{stem}_output.jsonl"),
            error_jsonl=str(WORK_DIR / f"{stem}_errors.jsonl"),
            state_json=str(WORK_DIR / f"{stem}_state.json"),
        )
        chunks_needed.append(state)

    print(f"[init] 전체 청크: {len(chunks_needed)}개")

    # 미완료 청크 필터 (status != 'completed')
    pending: List[ChunkState] = []
    for cs in chunks_needed:
        existing = existing_states.get(cs.chunk_no)
        if existing and existing.get("status") == "completed":
            print(f"[skip] chunk={cs.chunk_no} (already completed)")
            continue
        # input.jsonl 없으면 생성
        input_path = Path(cs.input_jsonl)
        if not input_path.exists():
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            chunk_lines = all_lines[cs.start_line - 1 : cs.end_line]
            input_path.write_text("\n".join(chunk_lines) + "\n", encoding="utf-8")
        # state 파일 없거나 created면 초기화
        state_path = Path(cs.state_json)
        if not state_path.exists():
            write_json(state_path, asdict(cs))
        else:
            # 기존 state 로드 (batch_id 등 보존)
            d = read_json(state_path)
            for k, v in d.items():
                if hasattr(cs, k):
                    setattr(cs, k, v)
        pending.append(cs)

    print(f"[init] 제출 대상 청크: {len(pending)}개 (chunk {pending[0].chunk_no if pending else '-'}부터)")
    if not pending:
        print("모든 청크가 이미 완료됨. 병합만 실행합니다.")
        merge_all_outputs()
        return

    # 순차 제출
    for cs in pending:
        print("=" * 80)
        # 이미 제출된 배치가 있으면 상태 확인만
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
            print("배치가 실패했습니다. 원인을 확인하고 스크립트를 다시 실행하세요.")
            sys.exit(1)

    # 전체 병합
    print("=" * 80)
    print("[merge] 모든 청크 완료. 병합 시작...")
    merge_all_outputs()
    print("[완료] 전체 배치 처리 완료.")


if __name__ == "__main__":
    main()
