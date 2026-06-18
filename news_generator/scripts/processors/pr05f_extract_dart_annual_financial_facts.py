#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract conservative annual financial facts from locally downloaded DART XML zips.

This script does not call DART or any LLM. It reads business-report XML files
under news_generator/data/raw/dart/documents and emits short Korean fact rows
that pr05f can attach as official same-stock annual context after the report
filing date.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


DEFAULT_INDEX_CSV = Path(
    "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/dart_business_report_index.csv"
)
DEFAULT_DOCUMENT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/news_generator/data/raw/dart/documents"
)
DEFAULT_OUTPUT_CSV = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05f_dart_annual_financial_facts/dart_annual_financial_facts.csv"
)


def clean(text: Any) -> str:
    if text is None:
        return ""
    text = str(text).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" ,", ",")
    return text


def normalize_date(value: Any) -> str:
    digits = re.sub(r"[^0-9]", "", clean(value))
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def sentence_split(text: str) -> list[str]:
    text = clean(text)
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+", text) if s.strip()]


def cap(text: str, limit: int = 180) -> str:
    text = clean(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class IndexRow:
    stock_code: str
    stock_name: str
    business_year: str
    rcept_no: str
    rcept_date: str
    report_name: str


class DartAnnualFinancialExtractor:
    def __init__(self, index_csv: Path, document_dir: Path) -> None:
        self.index_csv = index_csv
        self.document_dir = document_dir

    def load_index(self) -> list[IndexRow]:
        rows: list[IndexRow] = []
        with self.index_csv.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stock_code = clean(row.get("stock_code")).zfill(6)[-6:]
                business_year = clean(row.get("business_year"))
                rcept_no = clean(row.get("rcept_no"))
                if not stock_code or not business_year or not rcept_no:
                    continue
                rows.append(
                    IndexRow(
                        stock_code=stock_code,
                        stock_name=clean(row.get("dart_name")) or clean(row.get("input_name")),
                        business_year=business_year,
                        rcept_no=rcept_no,
                        rcept_date=normalize_date(row.get("rcept_date")),
                        report_name=clean(row.get("report_name")),
                    )
                )
        return rows

    def extract_all(self, max_reports: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for idx, row in enumerate(self.load_index(), start=1):
            if max_reports is not None and idx > max_reports:
                break
            zip_path = self.document_dir / row.stock_code / f"{row.business_year}_{row.rcept_no}.zip"
            if not zip_path.exists():
                continue
            facts = self.extract_one(row, zip_path)
            if not facts:
                continue
            out.append(
                {
                    "stock_code": row.stock_code,
                    "stock_name": row.stock_name,
                    "business_year": row.business_year,
                    "rcept_no": row.rcept_no,
                    "rcept_date": row.rcept_date,
                    "report_name": row.report_name,
                    "fact_count": len(facts),
                    "facts_json": json.dumps(facts, ensure_ascii=False),
                }
            )
        return out

    def extract_one(self, row: IndexRow, zip_path: Path) -> list[dict[str, str]]:
        text = self._zip_text(zip_path)
        if not text:
            return []

        facts: list[dict[str, str]] = []
        facts.extend(self._extract_yoy_amount_sentences(row, text))
        facts.extend(self._extract_multiyear_sales_sentence(row, text))
        facts.extend(self._extract_summary_table_metrics(row, text))

        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for fact in facts:
            key = fact.get("text_ko", "")
            if not key or key in seen or self._looks_noisy_fact(key):
                continue
            seen.add(key)
            deduped.append(fact)
        return deduped[:6]

    def _zip_text(self, zip_path: Path) -> str:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
                if not names:
                    return ""
                data = zf.read(names[0]).decode("utf-8", "ignore")
        except (zipfile.BadZipFile, OSError):
            return ""

        soup = BeautifulSoup(data, "lxml-xml")
        text = soup.get_text(" ", strip=True)
        return clean(text)

    def _extract_yoy_amount_sentences(self, row: IndexRow, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        for sentence in sentence_split(text):
            if "전년동기대비" not in sentence:
                continue
            if not any(metric in sentence for metric in ["매출액", "영업이익", "당기순이익"]):
                continue
            if "기록" not in sentence:
                continue
            # Keep the source wording, but trim very long business-description prefixes.
            for metric in ["총 매출액", "매출액", "영업이익", "당기순이익"]:
                idx = sentence.find(metric)
                if idx >= 0:
                    sentence = sentence[idx:]
                    break
            sentence = sentence.replace("하였습니다", "했다").replace("였습니다", "였다")
            sentence = clean(sentence)
            fact_type = self._metric_fact_type(sentence)
            facts.append(
                {
                    "fact_type": fact_type,
                    "relation_scope": "same_stock_report_after_filing",
                    "text_ko": f"{row.stock_name}의 {row.business_year}년 {cap(sentence, 150)}",
                    "source_text_ko": cap(sentence, 220),
                }
            )
        return facts

    def _extract_multiyear_sales_sentence(self, row: IndexRow, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        pattern = re.compile(
            rf"(?:연결 기준 )?매출액은\s*{re.escape(row.business_year)}년\([^)]*\)\s*"
            r"([0-9,]+\s*조\s*[0-9,]+억원|[0-9,]+억원|[0-9,]+백만원)"
        )
        match = pattern.search(text)
        if not match:
            return facts
        amount = clean(match.group(1))
        facts.append(
            {
                "fact_type": "sales",
                "relation_scope": "same_stock_report_after_filing",
                "text_ko": f"{row.stock_name}의 연결 기준 매출액은 {row.business_year}년 {amount}을 기록했다.",
                "source_text_ko": cap(match.group(0), 220),
            }
        )
        return facts

    def _extract_summary_table_metrics(self, row: IndexRow, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        pattern = re.compile(
            r"구\s*분\s+제\d+기\s+누계\([^)]*\)\s+제\d+기\s+누계\([^)]*\)\s+전기\s*대비증감\s+"
            r"매출액\s+([0-9,]+)\s+[0-9,]+\s+(-?[0-9.]+%)\s+"
            r"영업이익\s+([0-9,]+)\s+[0-9,]+\s+(-?[0-9.]+%)\s+"
            r"당기순이익\s+([0-9,]+)\s+[0-9,]+\s+(-?[0-9.]+%)"
        )
        match = pattern.search(text)
        if not match:
            return facts

        metrics = [
            ("sales", "매출액", match.group(1), match.group(2)),
            ("operating_profit", "영업이익", match.group(3), match.group(4)),
            ("net_income", "당기순이익", match.group(5), match.group(6)),
        ]
        for fact_type, label, amount, change in metrics:
            direction = "증가" if not change.startswith("-") else "감소"
            pct = change[1:] if change.startswith("-") else change
            facts.append(
                {
                    "fact_type": fact_type,
                    "relation_scope": "same_stock_report_after_filing",
                    "text_ko": f"{row.stock_name}의 {row.business_year}년 {label}은 전년 대비 {pct} {direction}한 {amount}백만원을 기록했다.",
                    "source_text_ko": cap(match.group(0), 220),
                }
            )
        return facts

    @staticmethod
    def _metric_fact_type(sentence: str) -> str:
        if "영업이익" in sentence:
            return "operating_profit"
        if "당기순이익" in sentence:
            return "net_income"
        return "sales"

    @staticmethod
    def _looks_noisy_fact(text: str) -> bool:
        noisy_terms = ["주)", " 기준 나.", "다.주", "보험(일시납", "장기보험"]
        return any(term in text for term in noisy_terms)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "stock_code",
        "stock_name",
        "business_year",
        "rcept_no",
        "rcept_date",
        "report_name",
        "fact_count",
        "facts_json",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract annual financial facts from local DART XML zips.")
    parser.add_argument("--index-csv", type=Path, default=DEFAULT_INDEX_CSV)
    parser.add_argument("--document-dir", type=Path, default=DEFAULT_DOCUMENT_DIR)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--max-reports", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    extractor = DartAnnualFinancialExtractor(args.index_csv, args.document_dir)
    rows = extractor.extract_all(max_reports=args.max_reports)
    write_csv(args.output_csv, rows)
    print(f"[dart annual facts] reports_with_facts={len(rows)}")
    print(f"[saved] {args.output_csv}")


if __name__ == "__main__":
    main()
