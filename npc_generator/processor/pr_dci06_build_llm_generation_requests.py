#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pr_dci06_build_llm_generation_requests.py

Purpose
-------
Build OpenAI request JSONL for factual stock news generation only.

Pipeline position
-----------------
Actual event input
→ factual stock news generation
→ pseudonymize stock/news
→ board reaction generation from pseudonymized news

Important
---------
This script no longer generates:
- rumor_or_speculation
- community_reaction_only
- market_reaction_news

Those belong to later stages.

Input
-----
data/processed/dci_llm_event_inputs_with_event_contexts/
  event_thread_units_with_event_contexts_dart_stockcode_fixed_2013_2023.jsonl

Output
------
data/processed/dci_llm_stock_news_requests/
  stock_news_requests_all.jsonl
  stock_news_request_preview.csv
  stock_news_request_report.txt

Design rules
------------
- Only factual_news_needed units are used.
- DART raw title is kept in preview/report only.
- LLM payload receives cleaned DART title only.
- No community threads are sent.
- No macro/stock mood context is sent.
- No market price_move is sent.
- No raw board/market numeric fields are sent.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


FACTUAL_TYPE = "factual_news_needed"


@dataclass
class StockNewsRequestConfig:
    project_root: Path
    input_jsonl: Path
    output_dir: Path

    model: str = "gpt-4o"
    max_tokens: int = 320
    temperature: float = 0.35

    limit: Optional[int] = None


# ---------------------------------------------------------------------
# DART title cleaning
# ---------------------------------------------------------------------

COMMON_DART_TITLE_REPLACEMENTS = [
    ("매출액또는손익구조", "매출액 또는 손익구조"),
    ("단일판매ㆍ공급계약체결", "단일판매ㆍ공급계약 체결"),
    ("단일판매·공급계약체결", "단일판매·공급계약 체결"),
    ("현금ㆍ현물배당결정", "현금ㆍ현물배당 결정"),
    ("현금·현물배당결정", "현금·현물배당 결정"),
    ("유상증자결정", "유상증자 결정"),
    ("무상증자결정", "무상증자 결정"),
    ("타법인주식및출자증권취득결정", "타법인 주식 및 출자증권 취득 결정"),
    ("타법인주식및출자증권처분결정", "타법인 주식 및 출자증권 처분 결정"),
    ("자기주식취득결정", "자기주식 취득 결정"),
    ("자기주식처분결정", "자기주식 처분 결정"),
    ("자기주식취득신탁계약체결결정", "자기주식 취득 신탁계약 체결 결정"),
    ("자기주식취득신탁계약해지결정", "자기주식 취득 신탁계약 해지 결정"),
    ("소송등의제기ㆍ신청", "소송 등의 제기ㆍ신청"),
    ("소송등의판결ㆍ결정", "소송 등의 판결ㆍ결정"),
    ("최대주주변경", "최대주주 변경"),
    ("대표이사변경", "대표이사 변경"),
    ("주식명의개서정지", "주식 명의개서 정지"),
    ("주주총회소집결의", "주주총회 소집 결의"),
    ("주주총회소집공고", "주주총회 소집공고"),
    ("영업양수결정", "영업 양수 결정"),
    ("영업양도결정", "영업 양도 결정"),
    ("회사합병결정", "회사 합병 결정"),
    ("회사분할결정", "회사 분할 결정"),
    ("회사분할합병결정", "회사 분할합병 결정"),
    ("전환사채권발행결정", "전환사채권 발행 결정"),
    ("신주인수권부사채권발행결정", "신주인수권부사채권 발행 결정"),
    ("교환사채권발행결정", "교환사채권 발행 결정"),
    ("감자결정", "감자 결정"),
    ("증권발행결과", "증권 발행 결과"),
    ("기업설명회", "기업설명회"),
    ("잠정실적", "잠정실적"),
    ("영업실적", "영업실적"),
    ("사업보고서", "사업보고서"),
    ("반기보고서", "반기보고서"),
    ("분기보고서", "분기보고서"),
]


BOILERPLATE_PAREN_KEYWORDS = re.compile(
    r"(대규모|법인|상장|공시|규정|자본시장|거래소|연결재무제표|"
    r"별도재무제표|최근사업연도|직전사업연도|이상|이하|초과|미만|해당|기준)"
)

THRESHOLD_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*%?\s*"
    r"(?:이상|이하|초과|미만|변경|감소|증가)?"
)

BRACKET_PREFIX_PATTERN = re.compile(r"^\s*(?:\[[^\]]+\]\s*)+")


def normalize_space(text: str) -> str:
    text = str(text or "")
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_report_names(raw: str) -> List[str]:
    """
    Conservative split.
    Avoid splitting on comma because DART titles can contain punctuation.
    """
    raw = str(raw or "").strip()
    if not raw:
        return []

    parts = re.split(r"\s*(?:\|\|\||\|\||\n)\s*", raw)
    parts = [normalize_space(p) for p in parts if normalize_space(p)]
    return parts if parts else [raw]


def extract_correction_label(raw_title: str) -> str:
    raw_title = str(raw_title or "")

    if "[기재정정]" in raw_title:
        return "기재정정"

    if "[첨부정정]" in raw_title:
        return "첨부정정"

    if "정정" in raw_title[:20]:
        return "정정"

    return ""


def clean_parenthetical(match: re.Match) -> str:
    content = normalize_space(match.group(1))

    if not content:
        return ""

    # Remove likely legal/regulatory/threshold parentheticals.
    if BOILERPLATE_PAREN_KEYWORDS.search(content):
        return ""

    if re.search(r"\d", content) and re.search(r"%|이상|이하|초과|미만|기준", content):
        return ""

    # Keep short substantive qualifiers.
    if content in {"잠정", "연결", "개별", "정정"}:
        return f" {content} "

    # Conservative default:
    # if uncertain, keep the phrase rather than inventing a simplified meaning.
    return f" {content} "



def final_clean_dart_title_text(s: str) -> str:
    """
    Final conservative cleanup before cleaned DART title is sent to the model.
    This function removes remaining DART form wrappers and fixes common compact titles.
    """
    s = normalize_space(s)

    replacements = [
        ("매출액 또는 손익구조변경", "매출액 또는 손익구조 변경"),
        ("매출액또는손익구조변경", "매출액 또는 손익구조 변경"),
        ("손익구조변경", "손익구조 변경"),

        ("신규시설투자등", "신규시설 투자 등"),
        ("횡령ㆍ배임사실확인", "횡령ㆍ배임 사실 확인"),
        ("투자판단관련주요경영사항", "투자판단 관련 주요 경영사항"),
        ("불성실공시법인지정예고", "불성실공시법인 지정예고"),

        ("전환사채권발행결정", "전환사채권 발행 결정"),
        ("신주인수권부사채권발행결정", "신주인수권부사채권 발행 결정"),
        ("교환사채권발행결정", "교환사채권 발행 결정"),

        ("타법인주식및출자증권취득결정", "타법인 주식 및 출자증권 취득 결정"),
        ("타법인주식및출자증권처분결정", "타법인 주식 및 출자증권 처분 결정"),

        ("현금ㆍ현물배당결정", "현금ㆍ현물배당 결정"),
        ("현금·현물배당결정", "현금·현물배당 결정"),
        ("유상증자결정", "유상증자 결정"),
        ("무상증자결정", "무상증자 결정"),
        ("자기주식취득결정", "자기주식 취득 결정"),
        ("자기주식처분결정", "자기주식 처분 결정"),

        ("최대주주변경", "최대주주 변경"),
        ("대표이사변경", "대표이사 변경"),
        ("주주총회소집결의", "주주총회 소집 결의"),
        ("주주총회소집공고", "주주총회 소집공고"),
    ]

    for old, new in replacements:
        s = s.replace(old, new)

    # Remove disclosure form wrapper.
    s = s.replace("주요사항보고서", "")
    s = normalize_space(s)

    # Deduplicate cases like:
    # "기재정정 신규시설 투자 등 | 신규시설 투자 등"
    if "|" in s:
        parts = [normalize_space(p) for p in s.split("|") if normalize_space(p)]

        kept = []
        seen = set()

        for part in parts:
            key = re.sub(r"^(기재정정|첨부정정|정정)\s+", "", part)
            key = re.sub(r"\s+", "", key)

            if key in seen:
                continue

            seen.add(key)
            kept.append(part)

        s = " | ".join(kept)

    s = re.sub(r"\s+", " ", s)
    return s.strip()


def clean_dart_report_name(raw_title: str) -> str:
    """
    Clean DART disclosure title before sending it to the model.

    Goal:
    - remove threshold/legal/form boilerplate likely to cause interpretation leakage
    - preserve enough event meaning for a short factual memo
    """
    s = normalize_space(raw_title)

    if not s:
        return ""

    correction_label = extract_correction_label(s)

    # Remove leading bracket labels like [기재정정], [첨부정정].
    s = BRACKET_PREFIX_PATTERN.sub("", s)

    # Remove or keep parentheticals conservatively.
    s = re.sub(r"\(([^()]*)\)", clean_parenthetical, s)

    # Remove common threshold fragments such as 30%이상, 15%이상.
    s = re.sub(r"\d+(?:\.\d+)?\s*%\s*(?:이상|이하|초과|미만)?", "", s)

    # Remove leftover threshold words that often remain after stripping numbers.
    s = re.sub(r"\b(?:이상|이하|초과|미만)\b", "", s)

    # Apply common DART title spacing replacements.
    for old, new in COMMON_DART_TITLE_REPLACEMENTS:
        s = s.replace(old, new)

    # Specific cleanup for the known failure form.
    s = s.replace("매출액 또는 손익구조 변경변경", "매출액 또는 손익구조 변경")
    s = s.replace("매출액 또는 손익구조  변경", "매출액 또는 손익구조 변경")

    # Add spacing around common suffixes when title is still compact.
    s = re.sub(r"(공급계약)(체결)", r"\1 \2", s)
    s = re.sub(r"(주주총회)(소집)", r"\1 \2", s)
    s = re.sub(r"(최대주주)(변경)", r"\1 \2", s)
    s = re.sub(r"(대표이사)(변경)", r"\1 \2", s)
    s = re.sub(r"(유상증자|무상증자|감자|합병|분할)(결정)", r"\1 \2", s)

    s = normalize_space(s)

    # If correction information was present, keep it as an explicit, source-backed label.
    if correction_label and correction_label not in s:
        s = f"{correction_label} {s}"

    # Remove empty parentheses/brackets remnants.
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\[\s*\]", "", s)
    s = normalize_space(s)
    s = final_clean_dart_title_text(s)

    return s


def clean_dart_report_names(raw_reports: str) -> List[Dict[str, str]]:
    rows = []

    for raw in split_report_names(raw_reports):
        cleaned = clean_dart_report_name(raw)
        if not cleaned:
            continue

        rows.append({
            "cleaned_report_name": cleaned,
            "correction_label": extract_correction_label(raw),
            "raw_report_name": raw,
        })

    return rows


# ---------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------

class PromptBuilder:
    SYSTEM_PROMPT = (
        "투자 게임용 한국어 개별 종목 공시 뉴스 메모를 만든다.\n"
        "- 출력은 JSON 하나만 쓴다.\n"
        "- 존댓말을 쓰지 않는다. '~습니다', '~합니다', '~해요', '~네요' 금지.\n"
        "- cleaned DART 공시명과 명시된 evidence 밖의 사실을 만들지 않는다.\n"
        "- 공시명의 괄호, 비율, 기준, 법적 문구, 공시 양식 문구를 해석하거나 설명하지 않는다.\n"
        "- 수치, 상대방, 계약금액, 실적, 원인, 전망을 새로 만들지 않는다.\n"
        "- headline은 반드시 종목명이 들어간 명사형 제목으로 쓴다.\n"
        "- headline에는 cleaned_report_name에 없는 사건 단어를 새로 붙이지 않는다.\n"
        "- cleaned_report_name에 '결정'이 없으면 headline이나 body에 '결정'을 새로 붙이지 않는다.\n"
        "- 공시일을 언급할 때는 dart.disclosure_dates만 사용하고, '공시일은 YYYY년 M월 D일이다.' 형식으로만 쓴다.\n"
        "- '공시는 ~에 이루어졌다' 표현은 쓰지 않는다.\n"
        "- '발표했다'보다 '공시를 냈다'를 우선 사용한다.\n"
        "- 정정 여부, 공시 유형은 payload에 있는 값만 사용한다.\n"
        "- 시장 가격, 거래량, 커뮤니티 반응, 거시 배경은 쓰지 않는다.\n"
        "- 기사체 클리셰와 전망성 문장을 쓰지 않는다.\n"
        "- 혐오표현, 노골적 성표현, 개인정보는 쓰지 않는다."
    )

    USER_RULE = (
        "cleaned_report_name만 근거로 짧은 공시 뉴스 메모를 작성한다. "
        "raw DART title은 제공되지 않는다. "
        "공시명이 애매하면 애매한 상태로 짧게 쓴다. "
        "의미를 풀어서 설명하지 말고 공시 사실만 건조하게 쓴다. "
        "공시일을 쓰는 경우 반드시 '공시일은 YYYY년 M월 D일이다.' 형식으로 쓴다. "
        "cleaned_report_name에 없는 '결정', '확대', '개선', '우려', '기대' 같은 단어를 추가하지 않는다."
    )

    OUTPUT_SHAPE = {
        "headline": "종목명 + cleaned_report_name 기반 제목. 없는 사건 단어 추가 금지.",
        "body": "1~2문장. '~ 공시를 냈다. 공시일은 YYYY년 M월 D일이다.' 중심. 해석/전망/원인 금지.",
        "community_line": "",
    }

    def build_messages(self, unit: Dict[str, Any], cleaned_reports: List[Dict[str, str]]) -> List[Dict[str, str]]:
        payload = {
            "task": "stock_factual_news",
            "rule": self.USER_RULE,
            "output": self.OUTPUT_SHAPE,
            "context": self._build_context(unit, cleaned_reports),
        }

        return [
            {
                "role": "system",
                "content": self.SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]

    @staticmethod
    def _build_context(unit: Dict[str, Any], cleaned_reports: List[Dict[str, str]]) -> Dict[str, Any]:
        evidence = unit.get("evidence", {}) or {}
        dart = evidence.get("dart", {}) or {}

        # Important:
        # raw_report_name is intentionally NOT sent to the model.
        model_reports = [
            {
                "cleaned_report_name": r["cleaned_report_name"],
                "correction_label": r.get("correction_label", ""),
            }
            for r in cleaned_reports
        ]

        return {
            "event_id": unit.get("candidate_id", ""),
            "stock": {
                "stock_code": str((unit.get("stock", {}) or {}).get("stock_code", "")),
                "stock_name": str((unit.get("stock", {}) or {}).get("stock_name", "")),
            },
            "dart": {
                "disclosure_dates": dart.get("dart_dates", ""),
                "cleaned_reports": model_reports,
            },
        }


class StockNewsRequestBuilder:
    def __init__(self, config: StockNewsRequestConfig):
        self.config = config
        self.prompt_builder = PromptBuilder()

    def load_units(self) -> List[Dict[str, Any]]:
        units: List[Dict[str, Any]] = []

        with self.config.input_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                units.append(json.loads(line))

        print(f"[LOAD] units: {len(units):,}")
        return units

    @staticmethod
    def get_raw_dart_reports(unit: Dict[str, Any]) -> str:
        evidence = unit.get("evidence", {}) or {}
        dart = evidence.get("dart", {}) or {}
        return str(dart.get("dart_report_names", "") or "")

    @staticmethod
    def has_dart_evidence(unit: Dict[str, Any]) -> bool:
        evidence = unit.get("evidence", {}) or {}
        dart = evidence.get("dart", {}) or {}

        if str(dart.get("dart_report_names", "") or "").strip():
            return True

        if int(dart.get("has_factual_evidence", 0) or 0) == 1:
            return True

        return False

    def should_use_unit(self, unit: Dict[str, Any]) -> bool:
        event_type = unit.get("event_generation_candidate_type", "")

        if event_type != FACTUAL_TYPE:
            return False

        if not self.has_dart_evidence(unit):
            return False

        raw_reports = self.get_raw_dart_reports(unit)
        cleaned_reports = clean_dart_report_names(raw_reports)

        if not cleaned_reports:
            return False

        stock = unit.get("stock", {}) or {}
        if not str(stock.get("stock_code", "") or "").strip():
            return False

        if not str(stock.get("stock_name", "") or "").strip():
            return False

        return True

    def build_request(self, unit: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(unit.get("candidate_id", ""))
        raw_reports = self.get_raw_dart_reports(unit)
        cleaned_reports = clean_dart_report_names(raw_reports)

        return {
            "custom_id": f"stock_news__{event_id}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": self.config.model,
                "messages": self.prompt_builder.build_messages(unit, cleaned_reports),
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "response_format": {
                    "type": "json_object",
                },
            },
        }

    def build_all(self) -> List[Dict[str, Any]]:
        units = self.load_units()
        requests: List[Dict[str, Any]] = []
        seen_request_keys = set()

        skipped_by_type: Counter = Counter()
        skipped_reasons: Counter = Counter()

        for unit in units:
            event_type = unit.get("event_generation_candidate_type", "")

            if event_type != FACTUAL_TYPE:
                skipped_by_type[event_type or "missing_type"] += 1
                continue

            if not self.has_dart_evidence(unit):
                skipped_reasons["no_dart_evidence"] += 1
                continue

            raw_reports = self.get_raw_dart_reports(unit)
            cleaned_reports = clean_dart_report_names(raw_reports)

            if not cleaned_reports:
                skipped_reasons["empty_cleaned_dart_report"] += 1
                continue

            stock = unit.get("stock", {}) or {}
            if not str(stock.get("stock_code", "") or "").strip():
                skipped_reasons["missing_stock_code"] += 1
                continue

            if not str(stock.get("stock_name", "") or "").strip():
                skipped_reasons["missing_stock_name"] += 1
                continue

            evidence = unit.get("evidence", {}) or {}
            dart = evidence.get("dart", {}) or {}
            stock = unit.get("stock", {}) or {}

            dedup_key = (
                str(stock.get("stock_code", "") or "").strip(),
                str(dart.get("dart_dates", "") or "").strip(),
                "||".join(r["cleaned_report_name"] for r in cleaned_reports),
            )

            if dedup_key in seen_request_keys:
                skipped_reasons["duplicate_stock_dart_report"] += 1
                continue

            seen_request_keys.add(dedup_key)
            requests.append(self.build_request(unit))

            if self.config.limit is not None and len(requests) >= self.config.limit:
                break

        print(f"[BUILD] stock news requests: {len(requests):,}")

        if skipped_reasons:
            print("[SKIP reasons]")
            for k, v in skipped_reasons.most_common():
                print(f"- {k}: {v:,}")

        return requests


# ---------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------

class StockNewsRequestWriter:
    def __init__(self, config: StockNewsRequestConfig):
        self.config = config

    def write(self, requests: List[Dict[str, Any]]) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        all_path = self.config.output_dir / "stock_news_requests_all.jsonl"
        preview_path = self.config.output_dir / "stock_news_request_preview.csv"
        report_path = self.config.output_dir / "stock_news_request_report.txt"
        cleaner_audit_path = self.config.output_dir / "dart_title_cleaner_audit.csv"

        self._write_jsonl(all_path, requests)
        self._write_preview_csv(preview_path, requests)
        self._write_cleaner_audit(cleaner_audit_path, requests)
        self._write_report(report_path, requests)

        print("[SAVE]")
        print(f"- requests:      {all_path}")
        print(f"- preview:       {preview_path}")
        print(f"- cleaner_audit: {cleaner_audit_path}")
        print(f"- report:        {report_path}")

    @staticmethod
    def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _get_payload(req: Dict[str, Any]) -> Dict[str, Any]:
        messages = req["body"]["messages"]
        return json.loads(messages[1]["content"])

    def _write_preview_csv(self, path: Path, requests: List[Dict[str, Any]]) -> None:
        fieldnames = [
            "custom_id",
            "event_id",
            "event_date",
            "stock_code",
            "stock_name",
            "dart_dates",
            "cleaned_report_names",
            "model",
            "temperature",
            "max_tokens",
        ]

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for req in requests:
                payload = self._get_payload(req)
                ctx = payload.get("context", {})
                stock = ctx.get("stock", {})
                dart = ctx.get("dart", {})
                reports = dart.get("cleaned_reports", [])

                writer.writerow({
                    "custom_id": req.get("custom_id", ""),
                    "event_id": ctx.get("event_id", ""),
                    "event_date": "",
                    "stock_code": stock.get("stock_code", ""),
                    "stock_name": stock.get("stock_name", ""),
                    "dart_dates": dart.get("disclosure_dates", ""),
                    "cleaned_report_names": " || ".join(
                        r.get("cleaned_report_name", "") for r in reports
                    ),
                    "model": req["body"].get("model", ""),
                    "temperature": req["body"].get("temperature", ""),
                    "max_tokens": req["body"].get("max_tokens", ""),
                })

    def _write_cleaner_audit(self, path: Path, requests: List[Dict[str, Any]]) -> None:
        """
        raw DART title is not in the LLM payload.
        This audit file reconstructs raw/cleaned pairs from the original input.
        """
        units_by_id: Dict[str, Dict[str, Any]] = {}

        with self.config.input_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                unit = json.loads(line)
                event_id = str(unit.get("candidate_id", ""))
                units_by_id[event_id] = unit

        fieldnames = [
            "event_id",
            "stock_code",
            "stock_name",
            "raw_report_name",
            "cleaned_report_name",
            "correction_label",
        ]

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for req in requests:
                event_id = req["custom_id"].split("__", 1)[1]
                unit = units_by_id.get(event_id, {})
                stock = unit.get("stock", {}) or {}
                raw_reports = StockNewsRequestBuilder.get_raw_dart_reports(unit)

                for row in clean_dart_report_names(raw_reports):
                    writer.writerow({
                        "event_id": event_id,
                        "stock_code": stock.get("stock_code", ""),
                        "stock_name": stock.get("stock_name", ""),
                        "raw_report_name": row.get("raw_report_name", ""),
                        "cleaned_report_name": row.get("cleaned_report_name", ""),
                        "correction_label": row.get("correction_label", ""),
                    })

    def _write_report(self, path: Path, requests: List[Dict[str, Any]]) -> None:
        stock_counter: Counter = Counter()

        for req in requests:
            payload = self._get_payload(req)
            ctx = payload.get("context", {})
            stock = ctx.get("stock", {})
            stock_counter[stock.get("stock_name", "")] += 1

        lines: List[str] = []

        lines.append("# Stock News Request Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- input_jsonl: {self.config.input_jsonl}")
        lines.append("")
        lines.append("## Output")
        lines.append(f"- output_dir: {self.config.output_dir}")
        lines.append("")
        lines.append("## Config")
        lines.append(f"- model: {self.config.model}")
        lines.append(f"- max_tokens: {self.config.max_tokens}")
        lines.append(f"- temperature: {self.config.temperature}")
        lines.append(f"- limit: {self.config.limit}")
        lines.append("")
        lines.append("## Scope")
        lines.append("- included_event_type: factual_news_needed")
        lines.append("- excluded: market_reaction_news, rumor_or_speculation, community_reaction_only")
        lines.append("- community_threads_sent_to_model: no")
        lines.append("- macro_or_stock_mood_context_sent_to_model: no")
        lines.append("- raw_dart_title_sent_to_model: no")
        lines.append("- cleaned_dart_title_sent_to_model: yes")
        lines.append("")
        lines.append("## Counts")
        lines.append(f"- total_requests: {len(requests):,}")
        lines.append(f"- unique_stocks: {len(stock_counter):,}")
        lines.append("")
        lines.append("## Top stocks")
        for stock_name, count in stock_counter.most_common(20):
            lines.append(f"- {stock_name}: {count}")

        path.write_text("\n".join(lines), encoding="utf-8-sig")


# ---------------------------------------------------------------------
# Pipeline / CLI
# ---------------------------------------------------------------------

class StockNewsRequestPipeline:
    def __init__(self, config: StockNewsRequestConfig):
        self.config = config

    def run(self) -> None:
        requests = StockNewsRequestBuilder(self.config).build_all()
        StockNewsRequestWriter(self.config).write(requests)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


def build_config_from_args() -> StockNewsRequestConfig:
    project_root = Path(__file__).resolve().parent.parent

    default_input_jsonl = (
        project_root
        / "data"
        / "processed"
        / "dci_llm_event_inputs_with_event_contexts"
        / "event_thread_units_with_event_contexts_dart_stockcode_fixed_2013_2023.jsonl"
    )

    default_output_dir = (
        project_root
        / "data"
        / "processed"
        / "dci_llm_stock_news_requests"
    )

    parser = argparse.ArgumentParser(
        description="Build OpenAI request JSONL for factual stock news only."
    )

    parser.add_argument("--input-jsonl", type=str, default=str(default_input_jsonl))
    parser.add_argument("--output-dir", type=str, default=str(default_output_dir))

    parser.add_argument("--model", type=str, default="gpt-4o")
    parser.add_argument("--max-tokens", type=int, default=320)
    parser.add_argument("--temperature", type=float, default=0.35)

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional request limit for sampling.",
    )

    args = parser.parse_args()

    return StockNewsRequestConfig(
        project_root=project_root,
        input_jsonl=Path(args.input_jsonl).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        limit=args.limit,
    )


def main() -> None:
    config = build_config_from_args()
    StockNewsRequestPipeline(config).run()


if __name__ == "__main__":
    main()
