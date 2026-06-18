#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_v4_0_download_and_audit.py

v4.0 배치 결과 다운로드 + 오딧 일괄 실행 스크립트.

Usage:
    python3 run_v4_0_download_and_audit.py

    # 배치 상태만 확인 (다운로드 없음):
    python3 run_v4_0_download_and_audit.py --check-only

    # 이미 다운로드된 output 파일로 오딧만 실행:
    python3 run_v4_0_download_and_audit.py --audit-only
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 경로 상수 ────────────────────────────────────────────────────────────────

BATCH_ID = "batch_6a3221e587d88190ade3076b2b3d5b67"

REQUESTS_JSONL = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/"
    "pr06a_stock_news_sample_requests_from_briefs_v4_0_dart_disclosure_detail/"
    "stock_news_sample_requests.jsonl"
)

OUTPUT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs"
)

OUTPUT_JSONL = OUTPUT_DIR / "stock_news_sample_outputs_v4_0.jsonl"
ERROR_JSONL  = OUTPUT_DIR / "stock_news_sample_errors_v4_0.jsonl"
AUDIT_DIR    = OUTPUT_DIR / "audit_v4_0"


# ── 다운로드 ─────────────────────────────────────────────────────────────────

def download(batch_id: str, output_jsonl: Path, error_jsonl: Path, wait: bool) -> bool:
    """배치 상태 확인 후 완료 시 결과 다운로드. 성공 여부 반환."""
    from openai import OpenAI
    client = OpenAI()

    while True:
        batch = client.batches.retrieve(batch_id)
        print(f"[batch] id={batch.id}  status={batch.status}  "
              f"completed={batch.request_counts.completed}  "
              f"failed={batch.request_counts.failed}  "
              f"total={batch.request_counts.total}")

        if batch.status in {"completed", "failed", "expired", "cancelled"}:
            break
        if not wait:
            print("[wait] 배치가 아직 진행 중입니다. --wait 옵션을 주거나 나중에 다시 실행하세요.")
            return False
        print("[wait] 30초 후 재확인...")
        time.sleep(30)

    if batch.status != "completed":
        print(f"[error] 배치가 completed 아님: {batch.status}")
        return False

    if not batch.output_file_id:
        print("[error] output_file_id 없음")
        return False

    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    content = client.files.content(batch.output_file_id)
    output_jsonl.write_text(content.text, encoding="utf-8")
    print(f"[saved] {output_jsonl}")

    if batch.error_file_id:
        err_content = client.files.content(batch.error_file_id)
        error_jsonl.write_text(err_content.text, encoding="utf-8")
        print(f"[saved errors] {error_jsonl}")

    return True


# ── 오딧 (audit_pr06a_v3_4_news_lines.py 로직 내장) ─────────────────────────

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


@dataclass
class AuditRow:
    custom_id: str
    status: str = ""
    news_lines: list[str] = field(default_factory=list)
    used_facts: list[str] = field(default_factory=list)
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _load_requests(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
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


def _extract_model_json(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
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


_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _numeric_leaks(text: str, detail_source_facts: list[str]) -> list[str]:
    source_text = " ".join(detail_source_facts)
    # ISO 날짜(2022-12-31)를 한국어 형식(2022년12월31일)으로도 허용
    extra = []
    for m in _ISO_DATE_RE.finditer(source_text):
        y, mo, d = m.group(1), m.group(2).lstrip("0") or "0", m.group(3).lstrip("0") or "0"
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


def _audit_one(obj: dict[str, Any], requests: dict[str, dict[str, Any]]) -> AuditRow:
    parsed, parse_error = _extract_model_json(obj)
    cid = str(obj.get("custom_id", "")).strip()
    payload = requests.get(cid, {})

    row = AuditRow(
        custom_id=cid,
        status=str(parsed.get("status", "")).strip(),
        news_lines=_as_str_list(parsed.get("news_lines", [])),
        used_facts=_as_str_list(parsed.get("used_facts", [])),
        detail_source_facts_ko=_as_str_list(payload.get("detail_source_facts_ko", [])),
        news_line_count_rule=str(payload.get("news_line_count_rule", "")).strip(),
        claim_level=str(payload.get("claim_level", "")).strip(),
    )

    text = "\n".join(row.news_lines)
    row.line_count = len(row.news_lines)
    row.char_len_no_space = len(text.replace(" ", ""))
    row.bad_term_hits = _hits(text, BAD_TERMS)
    row.market_term_hits = _hits(text, MARKET_TERMS)
    row.source_label_hits = _hits(text, SOURCE_LABEL_TERMS)
    row.numeric_leak_hits = _numeric_leaks(text, row.detail_source_facts_ko)

    # basic failures
    if parse_error:
        row.fail_reasons.append("json_parse_failed")
    if cid and not payload:
        row.fail_reasons.append("request_payload_not_found")
    if row.status != "accepted":
        row.fail_reasons.append(f"status_not_accepted:{row.status or 'missing'}")
    if "headline" in parsed:
        row.fail_reasons.append("unexpected_headline_field")
    if "detail_news" in parsed:
        row.fail_reasons.append("unexpected_detail_news_field")
    if not row.news_lines:
        row.fail_reasons.append("missing_news_lines")
    if not row.used_facts:
        row.fail_reasons.append("missing_used_facts")

    # v3.4 rules
    if row.news_line_count_rule == "exactly_one_line" and row.line_count != 1:
        row.fail_reasons.append(f"line_count_should_be_1:{row.line_count}")
    elif row.news_line_count_rule == "one_or_two_lines" and row.line_count not in {1, 2}:
        row.fail_reasons.append(f"line_count_should_be_1_or_2:{row.line_count}")
    elif row.news_line_count_rule not in {"exactly_one_line", "one_or_two_lines"}:
        row.fail_reasons.append(f"unknown_news_line_count_rule:{row.news_line_count_rule or 'missing'}")

    detail_set = set(row.detail_source_facts_ko)
    if not all(f in detail_set for f in row.used_facts):
        row.fail_reasons.append("used_facts_not_subset_of_detail_source_facts")

    if row.line_count != len(set(row.news_lines)):
        row.fail_reasons.append("duplicate_news_lines")

    for line in row.news_lines:
        if _sentence_count(line) != 1:
            row.fail_reasons.append("news_line_not_single_sentence")
            break

    if row.claim_level == "no_market_claim" and row.market_term_hits:
        row.fail_reasons.append("market_terms_under_no_market_claim")

    if row.numeric_leak_hits:
        row.fail_reasons.append("numeric_detail_not_in_detail_source_facts")

    # style
    if row.bad_term_hits:
        row.fail_reasons.append("bad_terms")
    if row.source_label_hits:
        row.fail_reasons.append("source_label_or_prompt_artifact")

    if row.news_line_count_rule == "exactly_one_line":
        if row.char_len_no_space < 15:
            row.fail_reasons.append(f"news_too_short:{row.char_len_no_space}")
        if row.char_len_no_space > 105:
            row.fail_reasons.append(f"news_too_long:{row.char_len_no_space}")

    return row


def run_audit(requests_jsonl: Path, outputs_jsonl: Path, audit_dir: Path) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    requests = _load_requests(requests_jsonl)
    raw_outputs = _read_jsonl(outputs_jsonl)

    rows = [_audit_one(obj, requests) for obj in raw_outputs]

    # missing outputs
    output_ids = {str(obj.get("custom_id", "")).strip() for obj in raw_outputs}
    for cid in sorted(set(requests) - output_ids):
        rows.append(AuditRow(
            custom_id=cid,
            detail_source_facts_ko=_as_str_list(requests[cid].get("detail_source_facts_ko", [])),
            fail_reasons=["missing_output_for_request"],
        ))

    _write_audit_csv(rows, audit_dir)
    _write_audit_report(rows, requests_jsonl, outputs_jsonl, audit_dir)
    _print_audit_summary(rows)


def _write_audit_csv(rows: list[AuditRow], audit_dir: Path) -> None:
    path = audit_dir / "generated_news_lines_audit_v4_0.csv"
    fieldnames = [
        "pass_all", "custom_id", "status", "news_lines", "line_count",
        "char_len_no_space", "claim_level", "news_line_count_rule",
        "fail_reasons", "bad_term_hits", "market_term_hits",
        "source_label_hits", "numeric_leak_hits",
        "used_facts", "detail_source_facts_ko",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "pass_all": row.pass_all,
                "custom_id": row.custom_id,
                "status": row.status,
                "news_lines": json.dumps(row.news_lines, ensure_ascii=False),
                "line_count": row.line_count,
                "char_len_no_space": row.char_len_no_space,
                "claim_level": row.claim_level,
                "news_line_count_rule": row.news_line_count_rule,
                "fail_reasons": "|".join(row.fail_reasons),
                "bad_term_hits": "|".join(row.bad_term_hits),
                "market_term_hits": "|".join(row.market_term_hits),
                "source_label_hits": "|".join(row.source_label_hits),
                "numeric_leak_hits": "|".join(row.numeric_leak_hits),
                "used_facts": json.dumps(row.used_facts, ensure_ascii=False),
                "detail_source_facts_ko": json.dumps(row.detail_source_facts_ko, ensure_ascii=False),
            })
    print(f"[saved] {path}")


def _write_audit_report(
    rows: list[AuditRow],
    requests_jsonl: Path,
    outputs_jsonl: Path,
    audit_dir: Path,
) -> None:
    total = len(rows)
    passed = sum(1 for r in rows if r.pass_all)
    fail_counts: dict[str, int] = {}
    for r in rows:
        for reason in r.fail_reasons:
            key = reason.split(":", 1)[0]
            fail_counts[key] = fail_counts.get(key, 0) + 1

    lines = [
        "# pr06a v4.0 News Lines Audit",
        "",
        f"- requests_jsonl: `{requests_jsonl}`",
        f"- outputs_jsonl : `{outputs_jsonl}`",
        f"- total : {total}",
        f"- passed: {passed}",
        f"- failed: {total - passed}",
        f"- pass_rate: {(passed / total if total else 0):.1%}",
        "",
        "## Gate",
    ]

    if total and passed == total:
        lines.append("- **PASS**: 전체 35건 strict gate 통과.")
    else:
        lines.append("- **FAIL**: 실패 항목 수정 후 대량 생성 진행.")

    lines += ["", "## Fail Reason Counts"]
    if fail_counts:
        for reason, cnt in sorted(fail_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- {reason}: {cnt}")
    else:
        lines.append("- none")

    lines += ["", "## Accepted News Lines"]
    for r in rows:
        if r.pass_all:
            joined = " / ".join(r.news_lines)
            lines.append(f"- `{r.custom_id}`: {joined}")

    lines += ["", "## Failed Rows"]
    failed = [r for r in rows if not r.pass_all]
    if failed:
        for r in failed:
            lines.append(f"- `{r.custom_id}`: {', '.join(r.fail_reasons)}")
    else:
        lines.append("- none")

    path = audit_dir / "generated_news_lines_audit_report_v4_0.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[saved] {path}")


def _print_audit_summary(rows: list[AuditRow]) -> None:
    total = len(rows)
    passed = sum(1 for r in rows if r.pass_all)
    print("=" * 100)
    print(f"[v4.0 audit]  total={total}  pass={passed}  fail={total - passed}  "
          f"rate={passed/total:.1%}" if total else "[v4.0 audit] no rows")
    print("=" * 100)
    for row in rows:
        tag = "PASS" if row.pass_all else "FAIL"
        joined = " / ".join(row.news_lines) if row.news_lines else "(no news_lines)"
        print(f"[{tag}] {row.custom_id}")
        print(f"       {joined}")
        if row.fail_reasons:
            print(f"       reasons: {'; '.join(row.fail_reasons)}")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="v4.0 배치 다운로드 + 오딧")
    parser.add_argument("--check-only", action="store_true", help="배치 상태만 확인, 다운로드 안 함")
    parser.add_argument("--audit-only", action="store_true", help="이미 다운로드된 output으로 오딧만 실행")
    parser.add_argument("--wait", action="store_true", help="배치 완료까지 폴링 대기")
    args = parser.parse_args()

    if args.audit_only:
        if not OUTPUT_JSONL.exists():
            print(f"[error] output file 없음: {OUTPUT_JSONL}", file=sys.stderr)
            sys.exit(1)
        run_audit(REQUESTS_JSONL, OUTPUT_JSONL, AUDIT_DIR)
        return

    if args.check_only:
        from openai import OpenAI
        client = OpenAI()
        batch = client.batches.retrieve(BATCH_ID)
        print(f"status   : {batch.status}")
        print(f"completed: {batch.request_counts.completed}")
        print(f"failed   : {batch.request_counts.failed}")
        print(f"total    : {batch.request_counts.total}")
        return

    # 다운로드
    ok = download(BATCH_ID, OUTPUT_JSONL, ERROR_JSONL, wait=args.wait)
    if not ok:
        sys.exit(1)

    # 오딧
    run_audit(REQUESTS_JSONL, OUTPUT_JSONL, AUDIT_DIR)


if __name__ == "__main__":
    main()
