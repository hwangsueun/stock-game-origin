#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_v4_1_full_chunk_download_and_audit.py

v4.1 전체 생성 청크 다운로드 + 오딧.

Usage:
    python run_v4_1_full_chunk_download_and_audit.py --chunk 1
    python run_v4_1_full_chunk_download_and_audit.py --chunk 1 --audit-only
    python run_v4_1_full_chunk_download_and_audit.py --chunk 1 --wait
"""
from __future__ import annotations
import argparse, csv, json, re, sys, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHUNK_BATCH_IDS: dict[int, str] = {
    1:  "batch_6a3339d556fc81909be7a4f511df617c",
    2:  "batch_6a333a72274c8190b801fa414887acf3",
    3:  "batch_6a333a76b01c81909865aa83e87068e4",
    4:  "batch_6a333a7bd8608190bbf20a58f47f5261",
    5:  "batch_6a333a808a788190b4b7efde03cb1f6b",
    6:  "batch_6a333a85731c8190af18976924b873b5",
    7:  "batch_6a333a8a0cb88190912240b6cbc05480",
    8:  "batch_6a333a8e516c819095052364f3c9c5da",
    9:  "batch_6a333a92f7148190bd1354dd25436c21",
    10: "batch_6a333a9807b08190b0c646f25b97f863",
    11: "batch_6a333a9d17148190986ad2d387e5c030",
    12: "batch_6a333aa253ec81909bc968d42f8a6d7c",
    13: "batch_6a333aa67ca081908994fa26bd2d06c5",
    14: "batch_6a333aaaf4c4819081036342c890655c",
}

BASE = Path("/Users/hgs/Desktop/IISE CD/data/interim")
REQUESTS_DIR = BASE / "pr06a_full_requests_v4_1"
OUTPUTS_DIR  = BASE / "pr06a_full_outputs_v4_1"

def requests_path(chunk: int) -> Path:
    return REQUESTS_DIR / f"requests_chunk_{chunk:02d}.jsonl"

def outputs_path(chunk: int) -> Path:
    return OUTPUTS_DIR / f"outputs_chunk_{chunk:02d}.jsonl"

def errors_path(chunk: int) -> Path:
    return OUTPUTS_DIR / f"errors_chunk_{chunk:02d}.jsonl"

def audit_dir(chunk: int) -> Path:
    return OUTPUTS_DIR / f"audit_chunk_{chunk:02d}"

# ── 오딧 로직 (run_v4_1_download_and_audit.py 와 동일) ────────────────────────

BAD_TERMS = [
    "최근", "것으로 나타났다", "것으로 분석된다", "분석된다",
    "영향을 미친 것으로", "영향을 미친", "영향을 미쳤다",
    "이는", "이로써", "이에 따라", "성과다", "이루어진", "이루어졌다",
    "전망된다", "예상된다", "주목된다", "부각됐다", "부각되며",
    "기여했다", "기여한", "기여하며",
    "정점이었다", "슈퍼사이클의 정점", "폭발적으로", "폭발적", "폭발했다",
    "돌풍", "최악", "쇼크", "수혜", "확인했다", "확인됐다", "성공했다",
]
MARKET_TERMS = [
    "주가", "거래량", "급등", "급락", "상승", "하락", "강세", "약세",
    "매수세", "매도세", "투자심리", "시장 반응", "투자자 반응",
    "호재", "악재", "수혜주", "테마주",
]
SOURCE_LABEL_TERMS = [
    "자료에는", "보고서에는", "공시 자료", "이벤트", "맥락", "주제",
    "항목이 포함", "관련 내용", "세부 내용",
    "detail_source", "write_safe", "brief", "bundle",
]
NUMERIC_TOKEN_RE = re.compile(
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:조|억|만|원|억원|조원|%|퍼센트|분기|월|일|년)?"
)
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_KR_ABBREV_DATE_RE = re.compile(r"'(\d{2})\.(\d{1,2})\.(\d{1,2})일")


@dataclass
class AuditRow:
    custom_id: str
    status: str = ""
    news_lines: list[str] = field(default_factory=list)
    detail_source_facts_ko: list[str] = field(default_factory=list)
    news_line_count_rule: str = ""
    claim_level: str = ""
    line_count: int = 0
    char_len_no_space: int = 0
    bad_term_hits: list[str] = field(default_factory=list)
    market_term_hits: list[str] = field(default_factory=list)
    source_label_hits: list[str] = field(default_factory=list)
    numeric_leak_hits: list[str] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)

    @property
    def pass_all(self) -> bool:
        return not self.fail_reasons


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: {e}") from e
    return rows


def _load_requests(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in _read_jsonl(path):
        cid = str(row.get("custom_id", "")).strip()
        messages = row.get("body", {}).get("messages", [])
        user_msg = next((m for m in messages if m.get("role") == "user"), {})
        try:
            instruction = json.loads(user_msg.get("content", "{}"))
        except json.JSONDecodeError:
            instruction = {}
        payload = instruction.get("brief_payload", {})
        if cid and isinstance(payload, dict):
            out[cid] = payload
    return out


def _extract_model_json(row: dict) -> tuple[dict, str]:
    content = ""
    try:
        content = row["response"]["body"]["choices"][0]["message"]["content"]
    except Exception:
        pass
    if not content:
        try:
            content = row["choices"][0]["message"]["content"]
        except Exception:
            pass
    if not content and ("news_lines" in row or "status" in row):
        return dict(row), ""
    try:
        parsed = json.loads(content)
    except Exception as e:
        return {"raw_text": content[:1000]}, str(e)
    return (parsed, "") if isinstance(parsed, dict) else ({"raw_text": content[:1000]}, "not_json_object")


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip() if not isinstance(item, dict) else str(item.get("text_ko", "")).strip()
        if text:
            out.append(text)
    return out


def _hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t in text]


def _numeric_leaks(text: str, detail_source_facts: list[str]) -> list[str]:
    source_text = " ".join(detail_source_facts)
    extra = []
    for m in _ISO_DATE_RE.finditer(source_text):
        y = m.group(1)
        mo = m.group(2).lstrip("0") or "0"
        d  = m.group(3).lstrip("0") or "0"
        extra.append(f"{y}년{mo}월{d}일")
    for m in _KR_ABBREV_DATE_RE.finditer(source_text):
        y = f"20{m.group(1)}"
        mo = m.group(2).lstrip("0") or "0"
        d  = m.group(3).lstrip("0") or "0"
        extra.append(f"{y}년{mo}월{d}일")
    allowed = re.sub(r"\s+", "", source_text + " ".join(extra))
    out = []
    for raw in NUMERIC_TOKEN_RE.findall(text):
        token = re.sub(r"\s+", "", raw)
        if token and token not in allowed:
            out.append(token)
    return out


def _sentence_count(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    return len([x for x in re.split(r"(?<=[.!?。])\s+", text) if x.strip()])


def _audit_one(obj: dict, requests: dict[str, dict]) -> AuditRow:
    parsed, parse_error = _extract_model_json(obj)
    cid = str(obj.get("custom_id", "")).strip()
    payload = requests.get(cid, {})
    row = AuditRow(
        custom_id=cid,
        status=str(parsed.get("status", "")).strip(),
        news_lines=_as_str_list(parsed.get("news_lines", [])),
        detail_source_facts_ko=payload.get("detail_source_facts_ko") or [],
        news_line_count_rule=str(payload.get("news_line_count_rule", "")),
        claim_level=str(payload.get("claim_level", "")),
    )
    if parse_error:
        row.fail_reasons.append("json_parse_failed")
        return row
    if row.status not in {"accepted", "rejected"}:
        row.fail_reasons.append("missing_output_for_request")
        return row
    if row.status == "rejected":
        return row
    full_text = " ".join(row.news_lines)
    row.line_count = len(row.news_lines)
    row.char_len_no_space = len(re.sub(r"\s+", "", full_text))
    row.bad_term_hits = _hits(full_text, BAD_TERMS)
    row.market_term_hits = _hits(full_text, MARKET_TERMS)
    row.source_label_hits = _hits(full_text, SOURCE_LABEL_TERMS)
    if row.claim_level == "no_market_claim" and row.market_term_hits:
        row.fail_reasons.append("market_terms_under_no_market_claim")
    if row.bad_term_hits:
        row.fail_reasons.append("bad_terms")
    if row.source_label_hits:
        row.fail_reasons.append("source_label_or_prompt_artifact")
    if row.news_line_count_rule == "exactly_one_line" and row.line_count != 1:
        row.fail_reasons.append("sentence_count_should_be_1")
    row.numeric_leak_hits = _numeric_leaks(full_text, row.detail_source_facts_ko)
    if row.numeric_leak_hits:
        row.fail_reasons.append("numeric_detail_not_in_detail_source_facts")
    return row


def run_audit(chunk: int) -> None:
    req_path = requests_path(chunk)
    out_path = outputs_path(chunk)
    adir = audit_dir(chunk)
    adir.mkdir(parents=True, exist_ok=True)
    requests = _load_requests(req_path)
    raw_outputs = _read_jsonl(out_path)
    rows = [_audit_one(obj, requests) for obj in raw_outputs]
    # 누락 체크
    for cid in requests:
        if not any(r.custom_id == cid for r in rows):
            rows.append(AuditRow(custom_id=cid, fail_reasons=["missing_output_for_request"]))

    # CSV 저장
    csv_path = adir / f"audit_chunk_{chunk:02d}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pass_all","custom_id","status","news_lines","line_count","fail_reasons",
                    "bad_term_hits","market_term_hits","numeric_leak_hits"])
        for r in rows:
            w.writerow([r.pass_all, r.custom_id, r.status,
                        " / ".join(r.news_lines), r.line_count,
                        "|".join(r.fail_reasons),
                        "|".join(r.bad_term_hits),
                        "|".join(r.market_term_hits),
                        "|".join(r.numeric_leak_hits)])
    print(f"[saved] {csv_path}")

    total  = len(rows)
    passed = sum(1 for r in rows if r.pass_all)
    print("=" * 80)
    print(f"[chunk {chunk:02d} audit]  total={total}  pass={passed}  fail={total-passed}  "
          f"rate={passed/total:.1%}" if total else f"[chunk {chunk:02d} audit] no rows")
    print("=" * 80)
    for r in rows:
        tag = "PASS" if r.pass_all else "FAIL"
        news_str = " / ".join(r.news_lines) if r.news_lines else f"[{r.status}]"
        print(f"[{tag}] {r.custom_id}")
        if r.news_lines:
            print(f"       {news_str[:120]}")
        if not r.pass_all:
            print(f"       reasons: {'|'.join(r.fail_reasons)}")


def download(chunk: int, wait: bool) -> bool:
    batch_id = CHUNK_BATCH_IDS.get(chunk)
    if not batch_id:
        print(f"[error] chunk {chunk}의 batch_id가 등록되지 않았습니다.")
        return False
    from openai import OpenAI
    client = OpenAI()
    out_path = outputs_path(chunk)
    err_path = errors_path(chunk)
    while True:
        batch = client.batches.retrieve(batch_id)
        print(f"[batch] id={batch.id}  status={batch.status}  "
              f"completed={batch.request_counts.completed}  "
              f"failed={batch.request_counts.failed}  "
              f"total={batch.request_counts.total}")
        if batch.status in {"completed","failed","expired","cancelled"}:
            break
        if not wait:
            print("[wait] 배치가 아직 진행 중입니다. --wait 옵션을 주세요.")
            return False
        print("[wait] 30초 후 재확인...")
        time.sleep(30)
    if batch.status != "completed":
        print(f"[error] 배치가 completed 아님: {batch.status}")
        return False
    if not batch.output_file_id:
        print("[error] output_file_id 없음")
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = client.files.content(batch.output_file_id)
    out_path.write_text(content.text, encoding="utf-8")
    print(f"[saved] {out_path}")
    if batch.error_file_id:
        err_content = client.files.content(batch.error_file_id)
        err_path.write_text(err_content.text, encoding="utf-8")
        print(f"[saved errors] {err_path}")
    return True


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--chunk", type=int, required=True, help="청크 번호 (1~4)")
    p.add_argument("--audit-only", action="store_true")
    p.add_argument("--wait", action="store_true")
    args = p.parse_args()
    if not args.audit_only:
        ok = download(args.chunk, args.wait)
        if not ok:
            sys.exit(1)
    run_audit(args.chunk)


if __name__ == "__main__":
    main()
