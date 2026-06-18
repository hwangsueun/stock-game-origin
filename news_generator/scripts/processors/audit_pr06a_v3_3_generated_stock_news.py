#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audit pr06a v3.3 generated stock-news outputs.

This auditor is intentionally stricter than the model self-check. It verifies
the generated output against the request payload, especially detail_source_facts_ko.
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
]

NUMERIC_TOKEN_RE = re.compile(
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:조|억|만|원|억원|조원|%|퍼센트|분기|월|일|년)?"
)


@dataclass
class AuditRow:
    custom_id: str
    status: str = ""
    headline: str = ""
    detail_news: str = ""
    used_facts: list[str] = field(default_factory=list)
    detail_source_facts_ko: list[str] = field(default_factory=list)
    topic_hints_ko: list[str] = field(default_factory=list)
    raw_write_safe_facts_ko_for_audit_only: list[str] = field(default_factory=list)
    restricted_facts_ko: list[Any] = field(default_factory=list)
    detail_sentence_rule: str = ""
    claim_level: str = ""
    sentence_count: int = 0
    detail_char_len_no_space: int = 0
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
            payload = RequestPayloadReader._extract_payload(row)
            if custom_id:
                out[custom_id] = payload
        return out

    @staticmethod
    def _extract_payload(row: dict[str, Any]) -> dict[str, Any]:
        messages = row.get("body", {}).get("messages", [])
        user_msg = next((m for m in messages if m.get("role") == "user"), {})
        content = user_msg.get("content", "{}")
        try:
            instruction = json.loads(content)
        except json.JSONDecodeError:
            return {}
        payload = instruction.get("brief_payload", {})
        return payload if isinstance(payload, dict) else {}


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

        if not content and ("headline" in row or "detail_news" in row):
            return dict(row), ""

        try:
            parsed = json.loads(content)
        except Exception as e:
            return {"raw_text": content[:1000]}, str(e)

        if isinstance(parsed, dict):
            return parsed, ""
        return {"raw_text": content[:1000]}, "model_content_not_json_object"


class V33Auditor:
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
            headline=str(obj.get("headline", "")).strip(),
            detail_news=str(obj.get("detail_news", "")).strip(),
            used_facts=self._as_str_list(obj.get("used_facts", [])),
            detail_source_facts_ko=self._as_str_list(payload.get("detail_source_facts_ko", [])),
            topic_hints_ko=self._as_str_list(payload.get("topic_hints_ko", [])),
            raw_write_safe_facts_ko_for_audit_only=self._as_str_list(
                payload.get("raw_write_safe_facts_ko_for_audit_only", [])
            ),
            restricted_facts_ko=payload.get("restricted_facts_ko", []),
            detail_sentence_rule=str(payload.get("detail_sentence_rule", "")).strip(),
            claim_level=str(payload.get("claim_level", "")).strip(),
        )

        text = row.headline + "\n" + row.detail_news
        row.sentence_count = self._sentence_count(row.detail_news)
        row.detail_char_len_no_space = len(row.detail_news.replace(" ", ""))
        row.bad_term_hits = self._hits(text, BAD_TERMS)
        row.market_term_hits = self._hits(text, MARKET_TERMS)
        row.source_label_hits = self._hits(text, SOURCE_LABEL_TERMS)
        row.numeric_leak_hits = self._numeric_leaks(row.detail_news, row.detail_source_facts_ko)

        self._add_basic_failures(row, obj, payload)
        self._add_v33_rule_failures(row)
        self._add_style_failures(row)
        return row

    def _add_basic_failures(self, row: AuditRow, obj: dict[str, Any], payload: dict[str, Any]) -> None:
        if obj.get("_parse_error"):
            row.fail_reasons.append("json_parse_failed")
        if row.custom_id and not payload:
            row.fail_reasons.append("request_payload_not_found")
        if row.status != "accepted":
            row.fail_reasons.append(f"status_not_accepted:{row.status or 'missing'}")
        if not row.headline:
            row.fail_reasons.append("missing_headline")
        if not row.detail_news:
            row.fail_reasons.append("missing_detail_news")
        if not row.used_facts:
            row.fail_reasons.append("missing_used_facts")

    def _add_v33_rule_failures(self, row: AuditRow) -> None:
        if row.detail_sentence_rule == "exactly_one_sentence" and row.sentence_count != 1:
            row.fail_reasons.append(f"sentence_count_should_be_1:{row.sentence_count}")
        elif row.detail_sentence_rule != "exactly_one_sentence" and row.sentence_count not in {1, 2}:
            row.fail_reasons.append(f"sentence_count_should_be_1_or_2:{row.sentence_count}")

        detail_set = set(row.detail_source_facts_ko)
        if not all(f in detail_set for f in row.used_facts):
            row.fail_reasons.append("used_facts_not_subset_of_detail_source_facts")

        if row.claim_level == "no_market_claim" and row.market_term_hits:
            row.fail_reasons.append("market_terms_under_no_market_claim")

        if row.numeric_leak_hits:
            row.fail_reasons.append("numeric_detail_not_in_detail_source_facts")

    def _add_style_failures(self, row: AuditRow) -> None:
        if row.bad_term_hits:
            row.fail_reasons.append("bad_terms")
        if row.source_label_hits:
            row.fail_reasons.append("source_label_or_prompt_artifact")

        if row.detail_sentence_rule == "exactly_one_sentence":
            if row.detail_char_len_no_space < 18:
                row.fail_reasons.append(f"detail_too_short:{row.detail_char_len_no_space}")
            if row.detail_char_len_no_space > 95:
                row.fail_reasons.append(f"detail_too_long:{row.detail_char_len_no_space}")

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
                    fail_reasons=["missing_output_for_request"],
                    detail_source_facts_ko=self._as_str_list(
                        requests[custom_id].get("detail_source_facts_ko", [])
                    ),
                )
            )

    def _write_csv(self, rows: list[AuditRow]) -> None:
        path = self.output_dir / "generated_news_audit_v3_3.csv"
        fieldnames = [
            "pass_all",
            "custom_id",
            "status",
            "headline",
            "detail_news",
            "sentence_count",
            "detail_char_len_no_space",
            "claim_level",
            "detail_sentence_rule",
            "fail_reasons",
            "bad_term_hits",
            "market_term_hits",
            "source_label_hits",
            "numeric_leak_hits",
            "used_facts",
            "detail_source_facts_ko",
            "topic_hints_ko",
            "raw_write_safe_facts_ko_for_audit_only",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "pass_all": row.pass_all,
                    "custom_id": row.custom_id,
                    "status": row.status,
                    "headline": row.headline,
                    "detail_news": row.detail_news,
                    "sentence_count": row.sentence_count,
                    "detail_char_len_no_space": row.detail_char_len_no_space,
                    "claim_level": row.claim_level,
                    "detail_sentence_rule": row.detail_sentence_rule,
                    "fail_reasons": "|".join(row.fail_reasons),
                    "bad_term_hits": "|".join(row.bad_term_hits),
                    "market_term_hits": "|".join(row.market_term_hits),
                    "source_label_hits": "|".join(row.source_label_hits),
                    "numeric_leak_hits": "|".join(row.numeric_leak_hits),
                    "used_facts": json.dumps(row.used_facts, ensure_ascii=False),
                    "detail_source_facts_ko": json.dumps(row.detail_source_facts_ko, ensure_ascii=False),
                    "topic_hints_ko": json.dumps(row.topic_hints_ko, ensure_ascii=False),
                    "raw_write_safe_facts_ko_for_audit_only": json.dumps(
                        row.raw_write_safe_facts_ko_for_audit_only,
                        ensure_ascii=False,
                    ),
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
            "# pr06a v3.3 Generated Stock News Audit",
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
            lines.append("- PASS: all generated rows satisfy the strict v3.3 gate.")
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

        (self.output_dir / "generated_news_audit_report_v3_3.md").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def _print_summary(self, rows: list[AuditRow]) -> None:
        total = len(rows)
        passed = sum(1 for row in rows if row.pass_all)
        print("=" * 100)
        print("[pr06a v3.3 generated news audit]")
        print("requests:", self.requests_jsonl)
        print("outputs :", self.outputs_jsonl)
        print("total   :", total)
        print("pass    :", passed)
        print("fail    :", total - passed)
        print("output  :", self.output_dir)
        print("=" * 100)
        for row in rows:
            result = "PASS" if row.pass_all else "FAIL"
            print(f"[{result}] {row.custom_id} | {row.headline} | {row.detail_news}")
            if row.fail_reasons:
                print("  reasons:", "; ".join(row.fail_reasons))

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("text_ko", "")).strip()
            else:
                text = str(item).strip()
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
    def _numeric_leaks(detail: str, detail_source_facts: list[str]) -> list[str]:
        allowed_blob = " ".join(detail_source_facts)
        out = []
        for raw_token in NUMERIC_TOKEN_RE.findall(detail):
            token = re.sub(r"\s+", "", raw_token)
            if not token:
                continue
            compact_allowed = re.sub(r"\s+", "", allowed_blob)
            if token not in compact_allowed:
                out.append(token)
        return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requests-jsonl",
        type=Path,
        default=Path(
            "/Users/hgs/Desktop/IISE CD/data/interim/"
            "pr06a_stock_news_sample_requests_from_briefs_v3_3/"
            "stock_news_sample_requests_5.jsonl"
        ),
    )
    parser.add_argument(
        "--outputs-jsonl",
        type=Path,
        default=Path(
            "/Users/hgs/Desktop/IISE CD/data/interim/"
            "pr06a_stock_news_sample_outputs/stock_news_sample_outputs.jsonl"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "/Users/hgs/Desktop/IISE CD/data/interim/"
            "pr06a_stock_news_sample_outputs/audit_v3_3"
        ),
    )
    args = parser.parse_args()

    V33Auditor(
        requests_jsonl=args.requests_jsonl,
        outputs_jsonl=args.outputs_jsonl,
        output_dir=args.output_dir,
    ).run()


if __name__ == "__main__":
    main()
