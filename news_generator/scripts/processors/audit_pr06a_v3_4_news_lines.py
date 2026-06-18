#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audit pr06a v3.4 news_lines outputs.

v3.4 removes headline/detail_news and accepts only news_lines to prevent
headline-body repetition when detail_source_facts_ko has low fact density.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


BAD_TERMS = [
    "최근",
    "것으로 나타났다",
    "것으로 분석된다",
    "분석된다",
    "영향을 미친 것으로",
    "영향을 미친",
    "영향을 미쳤다",
    "이는",
    "이로써",
    "이에 따라",
    "성과다",
    "이루어진",
    "이루어졌다",
    "전망된다",
    "예상된다",
    "주목된다",
    "부각됐다",
    "부각되며",
    "기여했다",
    "기여한",
    "기여하며",
    "정점이었다",
    "슈퍼사이클의 정점",
    "폭발적으로",
    "폭발적",
    "폭발했다",
    "돌풍",
    "최악",
    "쇼크",
    "수혜",
    "확인했다",
    "확인됐다",
    "성공했다",
]

MARKET_TERMS = [
    "주가",
    "거래량",
    "급등",
    "급락",
    "상승",
    "하락",
    "강세",
    "약세",
    "매수세",
    "매도세",
    "투자심리",
    "시장 반응",
    "투자자 반응",
    "호재",
    "악재",
    "수혜주",
    "테마주",
]

SOURCE_LABEL_TERMS = [
    "자료에는",
    "보고서에는",
    "공시 자료",
    "이벤트",
    "맥락",
    "주제",
    "항목이 포함",
    "관련 내용",
    "세부 내용",
    "detail_source",
    "write_safe",
    "brief",
    "bundle",
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


class Jsonl:
    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        rows = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSONL at {path}:{line_no}: {e}") from e
                if not isinstance(row, dict):
                    raise ValueError(f"Expected JSON object at {path}:{line_no}")
                rows.append(row)
        return rows


class RequestPayloadReader:
    @staticmethod
    def load_by_custom_id(path: Path) -> dict[str, dict[str, Any]]:
        out = {}
        for row in Jsonl.read(path):
            custom_id = str(row.get("custom_id", "")).strip()
            messages = row.get("body", {}).get("messages", [])
            user_msg = next((m for m in messages if m.get("role") == "user"), {})
            try:
                instruction = json.loads(user_msg.get("content", "{}"))
            except json.JSONDecodeError:
                instruction = {}
            payload = instruction.get("brief_payload", {})
            if custom_id and isinstance(payload, dict):
                out[custom_id] = payload
        return out


class OutputReader:
    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        rows = []
        for row in Jsonl.read(path):
            custom_id = str(row.get("custom_id", "")).strip()
            parsed, parse_error = OutputReader._extract_model_json(row)
            parsed["_outer_custom_id"] = custom_id
            parsed["_parse_error"] = parse_error
            rows.append(parsed)
        return rows

    @staticmethod
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

        if not content and ("news_lines" in row or "detail_news" in row):
            return dict(row), ""

        try:
            parsed = json.loads(content)
        except Exception as e:
            return {"raw_text": content[:1000]}, str(e)

        if isinstance(parsed, dict):
            return parsed, ""
        return {"raw_text": content[:1000]}, "model_content_not_json_object"


class V34NewsLinesAuditor:
    def __init__(self, requests_jsonl: Path, outputs_jsonl: Path, output_dir: Path) -> None:
        self.requests_jsonl = requests_jsonl
        self.outputs_jsonl = outputs_jsonl
        self.output_dir = output_dir

    def run(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        requests = RequestPayloadReader.load_by_custom_id(self.requests_jsonl)
        outputs = OutputReader.read(self.outputs_jsonl)

        rows = [self._audit_one(obj, requests) for obj in outputs]
        self._add_missing_output_failures(rows, requests, outputs)
        self._write_csv(rows)
        self._write_report(rows)
        self._print_summary(rows)

    def _audit_one(self, obj: dict[str, Any], requests: dict[str, dict[str, Any]]) -> AuditRow:
        custom_id = str(obj.get("_outer_custom_id", "")).strip()
        payload = requests.get(custom_id, {})
        row = AuditRow(
            custom_id=custom_id,
            status=str(obj.get("status", "")).strip(),
            news_lines=self._as_str_list(obj.get("news_lines", [])),
            used_facts=self._as_str_list(obj.get("used_facts", [])),
            detail_source_facts_ko=self._as_str_list(payload.get("detail_source_facts_ko", [])),
            news_line_count_rule=str(payload.get("news_line_count_rule", "")).strip(),
            claim_level=str(payload.get("claim_level", "")).strip(),
        )

        text = "\n".join(row.news_lines)
        row.line_count = len(row.news_lines)
        row.char_len_no_space = len(text.replace(" ", ""))
        row.bad_term_hits = self._hits(text, BAD_TERMS)
        row.market_term_hits = self._hits(text, MARKET_TERMS)
        row.source_label_hits = self._hits(text, SOURCE_LABEL_TERMS)
        row.numeric_leak_hits = self._numeric_leaks(text, row.detail_source_facts_ko)

        self._add_basic_failures(row, obj, payload)
        self._add_v34_rule_failures(row)
        self._add_style_failures(row)
        return row

    def _add_basic_failures(self, row: AuditRow, obj: dict[str, Any], payload: dict[str, Any]) -> None:
        if obj.get("_parse_error"):
            row.fail_reasons.append("json_parse_failed")
        if row.custom_id and not payload:
            row.fail_reasons.append("request_payload_not_found")
        if row.status != "accepted":
            row.fail_reasons.append(f"status_not_accepted:{row.status or 'missing'}")
        if "headline" in obj:
            row.fail_reasons.append("unexpected_headline_field")
        if "detail_news" in obj:
            row.fail_reasons.append("unexpected_detail_news_field")
        if not row.news_lines:
            row.fail_reasons.append("missing_news_lines")
        if not row.used_facts:
            row.fail_reasons.append("missing_used_facts")

    def _add_v34_rule_failures(self, row: AuditRow) -> None:
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
            if self._sentence_count(line) != 1:
                row.fail_reasons.append("news_line_not_single_sentence")
                break

        if row.claim_level == "no_market_claim" and row.market_term_hits:
            row.fail_reasons.append("market_terms_under_no_market_claim")

        if row.numeric_leak_hits:
            row.fail_reasons.append("numeric_detail_not_in_detail_source_facts")

    def _add_style_failures(self, row: AuditRow) -> None:
        if row.bad_term_hits:
            row.fail_reasons.append("bad_terms")
        if row.source_label_hits:
            row.fail_reasons.append("source_label_or_prompt_artifact")

        if row.news_line_count_rule == "exactly_one_line":
            if row.char_len_no_space < 15:
                row.fail_reasons.append(f"news_too_short:{row.char_len_no_space}")
            if row.char_len_no_space > 105:
                row.fail_reasons.append(f"news_too_long:{row.char_len_no_space}")

    def _add_missing_output_failures(
        self,
        rows: list[AuditRow],
        requests: dict[str, dict[str, Any]],
        outputs: list[dict[str, Any]],
    ) -> None:
        output_ids = {str(obj.get("_outer_custom_id", "")).strip() for obj in outputs}
        for custom_id in sorted(set(requests) - output_ids):
            rows.append(
                AuditRow(
                    custom_id=custom_id,
                    detail_source_facts_ko=self._as_str_list(
                        requests[custom_id].get("detail_source_facts_ko", [])
                    ),
                    fail_reasons=["missing_output_for_request"],
                )
            )

    def _write_csv(self, rows: list[AuditRow]) -> None:
        path = self.output_dir / "generated_news_lines_audit_v3_4.csv"
        fieldnames = [
            "pass_all",
            "custom_id",
            "status",
            "news_lines",
            "line_count",
            "char_len_no_space",
            "claim_level",
            "news_line_count_rule",
            "fail_reasons",
            "bad_term_hits",
            "market_term_hits",
            "source_label_hits",
            "numeric_leak_hits",
            "used_facts",
            "detail_source_facts_ko",
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

    def _write_report(self, rows: list[AuditRow]) -> None:
        total = len(rows)
        passed = sum(1 for row in rows if row.pass_all)
        fail_counts: dict[str, int] = {}
        for row in rows:
            for reason in row.fail_reasons:
                key = reason.split(":", 1)[0]
                fail_counts[key] = fail_counts.get(key, 0) + 1

        lines = [
            "# pr06a v3.4 News Lines Audit",
            "",
            f"- requests_jsonl: `{self.requests_jsonl}`",
            f"- outputs_jsonl: `{self.outputs_jsonl}`",
            f"- total: {total}",
            f"- passed: {passed}",
            f"- failed: {total - passed}",
            f"- pass_rate: {(passed / total if total else 0):.1%}",
            "",
            "## Gate",
        ]

        if total and passed == total:
            lines.append("- PASS: all generated rows satisfy the strict v3.4 news_lines gate.")
        else:
            lines.append("- FAIL: do not proceed to wider generation until failures are fixed.")

        lines.extend(["", "## Fail Reason Counts"])
        if fail_counts:
            for reason, count in sorted(fail_counts.items(), key=lambda x: (-x[1], x[0])):
                lines.append(f"- {reason}: {count}")
        else:
            lines.append("- none")

        lines.extend(["", "## Failed Rows"])
        failed_rows = [row for row in rows if not row.pass_all]
        if failed_rows:
            for row in failed_rows:
                lines.append(f"- `{row.custom_id}`: {', '.join(row.fail_reasons)}")
        else:
            lines.append("- none")

        (self.output_dir / "generated_news_lines_audit_report_v3_4.md").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def _print_summary(self, rows: list[AuditRow]) -> None:
        total = len(rows)
        passed = sum(1 for row in rows if row.pass_all)
        print("=" * 100)
        print("[pr06a v3.4 news_lines audit]")
        print("requests:", self.requests_jsonl)
        print("outputs :", self.outputs_jsonl)
        print("total   :", total)
        print("pass    :", passed)
        print("fail    :", total - passed)
        print("output  :", self.output_dir)
        print("=" * 100)
        for row in rows:
            result = "PASS" if row.pass_all else "FAIL"
            joined = " / ".join(row.news_lines)
            print(f"[{result}] {row.custom_id} | {joined}")
            if row.fail_reasons:
                print("  reasons:", "; ".join(row.fail_reasons))

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            text = str(item).strip() if not isinstance(item, dict) else str(item.get("text_ko", "")).strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _sentence_count(text: str) -> int:
        text = text.strip()
        if not text:
            return 0
        parts = [x.strip() for x in re.split(r"(?<=[.!?。])\s+", text) if x.strip()]
        return len(parts)

    @staticmethod
    def _hits(text: str, terms: list[str]) -> list[str]:
        return [term for term in terms if term in text]

    @staticmethod
    def _numeric_leaks(text: str, detail_source_facts: list[str]) -> list[str]:
        allowed_blob = re.sub(r"\s+", "", " ".join(detail_source_facts))
        out = []
        for raw_token in NUMERIC_TOKEN_RE.findall(text):
            token = re.sub(r"\s+", "", raw_token)
            if token and token not in allowed_blob:
                out.append(token)
        return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requests-jsonl",
        type=Path,
        default=Path(
            "/Users/hgs/Desktop/IISE CD/data/interim/"
            "pr06a_stock_news_sample_requests_from_briefs_v3_4_detailfacts/"
            "stock_news_sample_requests.jsonl"
        ),
    )
    parser.add_argument(
        "--outputs-jsonl",
        type=Path,
        default=Path(
            "/Users/hgs/Desktop/IISE CD/data/interim/"
            "pr06a_stock_news_sample_outputs/stock_news_sample_outputs_v3_4_news_lines.jsonl"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "/Users/hgs/Desktop/IISE CD/data/interim/"
            "pr06a_stock_news_sample_outputs/audit_v3_4_news_lines"
        ),
    )
    args = parser.parse_args()

    V34NewsLinesAuditor(
        requests_jsonl=args.requests_jsonl,
        outputs_jsonl=args.outputs_jsonl,
        output_dir=args.output_dir,
    ).run()


if __name__ == "__main__":
    main()
