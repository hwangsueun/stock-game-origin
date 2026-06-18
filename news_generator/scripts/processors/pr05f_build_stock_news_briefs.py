#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr05f_build_stock_news_briefs.py

Build source-aware stock-news briefs from pr05e evidence bundles.

V2.1 policy changes:
- stock_event_context is background only, not an article lead.
- DART disclosure briefs need real official detail facts before they become ready.
- stock_event trigger briefs are separated from background context.
- output includes source evidence counts and trigger/context fact counts.
- write_safe_facts_ko and restricted_facts_ko are separated for pr06/pr06a.
- no_market_claim briefs block stock-price, volume, investor-reaction, and market-sentiment wording.

This script does NOT call an LLM and does NOT generate final news text.
It converts pr05e bundles into editorial brief cards that pr06a/pr06 can use
for high-quality Korean stock-news generation without mixing unsupported
causality across DART, stock events, macro context, and price/volume evidence.

Default input:
  /Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.jsonl

Default output dir:
  /Users/hgs/Desktop/IISE CD/data/interim/pr05f_stock_news_briefs
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


# =============================================================================
# Defaults
# =============================================================================

DEFAULT_BUNDLES_JSONL = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.jsonl"
)

DEFAULT_OUTPUT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05f_stock_news_briefs"
)

DEFAULT_DART_ANNUAL_FINANCIAL_FACTS_CSV = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05f_dart_annual_financial_facts/dart_annual_financial_facts.csv"
)

DEFAULT_DART_DISCLOSURE_DETAIL_FACTS_CSV = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05f_dart_disclosure_detail_facts/dart_disclosure_detail_facts.csv"
)

SOURCE_NAMES = [
    "dart",
    "dart_disclosure_detail",
    "dart_annual_financial",
    "stock_event",
    "stock_event_context",
    "macro",
    "price_volume",
    "gdelt",
]

CLAIM_ORDER = {
    "insufficient_evidence": 0,
    "no_market_claim": 1,
    "reaction_only": 2,
    "plausible_market_context": 3,
    "likely_contributor": 4,
    "strongest_attributable_disclosed_factor": 5,
    # Legacy compatibility.
    "primary_market_driver_candidate": 5,
    "dominant_disclosed_factor": 5,
    "strongest_attributable_factor": 5,
}

# dart_disclosure_detail fact types that provide business context (not numeric data)
DART_DETAIL_CONTEXT_FACT_TYPES = {
    "earnings_change_reason",
    "investment_detail",
    "contract_detail",
    "contract_item",
}

# Fact types that are enough to support a second sentence in a real article.
STRONG_OFFICIAL_FACT_TYPES = {
    "amount",
    "total_amount",
    "issue_price",
    "conversion_price",
    "exercise_price",
    "share_count",
    "dividend_per_share",
    "dividend_total_amount",
    "record_date",
    "payment_date",
    "subscription_date",
    "payment_due_date",
    "maturity_date",
    "contract_amount",
    "contract_period",
    "counterparty",
    "target_company",
    "acquisition_amount",
    "disposal_amount",
    "stake_ratio",
    "funding_purpose",
    "transaction_purpose",
    "investment_amount",
    "investment_period",
    "facility",
    "asset_type",
    "court",
    "case_name",
    "claim_amount",
    "ruling_result",
    "sales",
    "operating_profit",
    "net_income",
    "yoy_change",
}

STRONG_PRICE_FACT_TYPES = {
    "return_1d",
    "return_3d",
    "return_5d",
    "event_window_return",
    "abnormal_return",
    "volume_z",
    "volume_ratio",
    "turnover_ratio",
    "price_direction",
}

CONTEXT_FACT_TYPES = {
    "stock_event_topic",
    "stock_event_main_fact",
    "stock_event_supporting_fact",
    "stock_event_interpretation",
    "stock_event_role",
    "sector_theme",
    "peer_context",
    "macro_event",
    "macro_direction",
    "macro_sector_exposure",
    "gdelt_background",
}

# Values/fields that are provenance but not enough to make an article natural.
PROVENANCE_FACT_TYPES = {
    "report_title",
    "disclosure_date",
    "receipt_no",
    "corp_code",
    "stock_code",
    "anchor_date",
}

GENERIC_ACTION_TYPES = {
    "",
    "unknown",
    "unspecified_disclosure",
    "generic_management_matter",
    "plain_disclosure",
    "other_disclosure",
}

NEWS_TYPE_ORDER = [
    "corporate_action_disclosure",
    "stock_event_trigger",
    "macro_exposure_context",
    "market_reaction_observed",
    "context_only",
    "mixed_context",
    "sparse_disclosure",
    "do_not_generate",
]

# Text-level safety filter used to produce write_safe_facts_ko.
# These patterns do not mean the underlying evidence is invalid; they mean pr06
# should not copy that wording when allowed_claim_level is no_market_claim.
MARKET_REACTION_WORDS = [
    "주가", "종가", "시초가", "상한가", "하한가",
    "급등", "급락", "상승", "하락", "강세", "약세",
    "거래량", "거래대금", "매수세", "매도세",
    "투자심리", "시장 반응", "시장 관심", "시장에서는",
    "주목", "부각", "호재", "악재",
]

AI_OR_ANALYST_STYLE_WORDS = [
    "이벤트", "맥락", "시사", "해석", "확인할 필요",
    "보인다", "알려졌다", "전망된다", "예상된다",
]

STOCK_EVENT_UNSAFE_BODY_WORDS = [
    "주가", "거래량", "급등", "급락", "상승", "하락", "강세", "약세",
    "투자심리", "시장 반응", "투자자", "호재", "악재", "수혜", "테마주",
    "돌풍", "최악", "쇼크", "충격", "정점", "폭락", "폭증", "급증",
    "폭발적", "폭발적으로", "성공했다",
    "확인됐다", "확인했다", "주목", "부각", "기대가 강화", "우려가 고조",
    "고공행진", "슈퍼사이클", "절정에 달했다",
]

STOCK_EVENT_MAIN_FACT_HINTS = [
    "영업이익", "영업적자", "매출", "순이익", "판매", "인수", "취득", "처분",
    "투자", "계약", "배당", "흑자 전환", "적자 전환", "사상 최대", "급감",
    "실적이 예상을", "실적이 기대를", "퇴진 운동에 동참",
]

STOCK_EVENT_SUPPORT_FACT_HINTS = [
    "신차", "원화", "환율", "비중", "전기차", "수요", "가격", "HBM",
    "D램", "낸드", "믹스", "비용", "채산성", "효과",
]

STOCK_EVENT_FACT_ANCHOR_RE = re.compile(
    r"(영업이익|영업적자|매출|판매|인수|퇴진|동참|흑자 전환|사상 최대|적자|"
    r"채산성|실적|순이익|계약|투자|배당)"
)

STOCK_EVENT_CAUSE_HEAD_REWRITE_TERMS = [
    "폭락", "폭증", "급증", "급락", "가격", "수요", "매출",
    "수혜", "기대", "우려", "호조",
]

STOCK_EVENT_MAIN_FACT_RE = re.compile(
    r"(영업이익|영업적자|매출|순이익|판매|실적).{0,80}"
    r"(기록했다|달성했다|경신했다|돌파했다|상회했다|개선됐다|감소했다|급감했다|"
    r"흑자 전환했다|적자 전환했다|넘어섰다)"
    r"|퇴진 운동에 동참했다"
)


# =============================================================================
# Regexes
# =============================================================================

MONEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(" 
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
    r")\s*(조\s*원|억원|백만원|천만원|만원|천원|원|USD|달러|KRW)",
    re.IGNORECASE,
)

DATE_RE = re.compile(
    r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일|\d{8})"
)

PCT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*%")
SHARE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,3}(?:,\d{3})+|\d+)\s*(주|株)")
PRICE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*원")


# =============================================================================
# Config / IO
# =============================================================================


@dataclass(frozen=True)
class Pr05fConfig:
    bundles_jsonl: Path
    output_dir: Path
    dart_annual_financial_facts_csv: Path | None = DEFAULT_DART_ANNUAL_FINANCIAL_FACTS_CSV
    dart_disclosure_detail_facts_csv: Path | None = DEFAULT_DART_DISCLOSURE_DETAIL_FACTS_CSV
    max_bundles: int | None = None
    min_ready_score: int = 4
    include_raw_bundle: bool = False
    overwrite: bool = True

    @property
    def briefs_jsonl_path(self) -> Path:
        return self.output_dir / "stock_news_briefs.jsonl"

    @property
    def briefs_csv_path(self) -> Path:
        return self.output_dir / "stock_news_briefs.csv"

    @property
    def facts_csv_path(self) -> Path:
        return self.output_dir / "stock_news_brief_facts.csv"

    @property
    def report_path(self) -> Path:
        return self.output_dir / "stock_news_brief_report.md"


class JsonlIO:
    @staticmethod
    def read_jsonl(path: Path, max_rows: int | None = None) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"JSONL not found: {path}")
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSONL at {path}:{line_no}: {e}") from e
                if not isinstance(obj, dict):
                    raise ValueError(f"Expected JSON object at {path}:{line_no}")
                rows.append(obj)
                if max_rows is not None and len(rows) >= max_rows:
                    break
        return rows

    @staticmethod
    def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1
        return n


class DartAnnualFinancialFactIndex:
    def __init__(self, csv_path: Path | None) -> None:
        self.csv_path = csv_path
        self.by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.csv_path or not self.csv_path.exists():
            return

        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stock_code = normalize_stock_code(row.get("stock_code"))
                if not stock_code:
                    continue
                try:
                    facts = json.loads(row.get("facts_json") or "[]")
                except json.JSONDecodeError:
                    facts = []
                if not isinstance(facts, list) or not facts:
                    continue
                self.by_stock[stock_code].append({
                    "stock_code": stock_code,
                    "stock_name": normalize_str(row.get("stock_name")),
                    "business_year": normalize_str(row.get("business_year")),
                    "rcept_no": normalize_str(row.get("rcept_no")),
                    "rcept_date": normalize_date(row.get("rcept_date")),
                    "report_name": normalize_str(row.get("report_name")),
                    "facts": [x for x in facts if isinstance(x, dict)],
                })

        for rows in self.by_stock.values():
            rows.sort(key=lambda x: (x.get("rcept_date", ""), x.get("business_year", "")), reverse=True)

    def facts_for_bundle(self, bundle: dict[str, Any], max_age_days: int = 180) -> list[dict[str, Any]]:
        self.load()
        stock_code = normalize_stock_code(bundle.get("stock_code"))
        anchor_date = normalize_date(bundle.get("anchor_date"))
        if not stock_code or not anchor_date:
            return []

        anchor_ord = date_ordinal(anchor_date)
        if anchor_ord is None:
            return []

        event_family = normalize_str(bundle.get("event_family"))
        if event_family and event_family not in {"earnings", "dividend"}:
            return []

        out: list[dict[str, Any]] = []
        for item in self.by_stock.get(stock_code, []):
            rcept_date = normalize_date(item.get("rcept_date"))
            rcept_ord = date_ordinal(rcept_date)
            if rcept_ord is None or rcept_ord > anchor_ord:
                continue
            if anchor_ord - rcept_ord > max_age_days:
                continue

            annual_facts = sorted(
                item.get("facts", []),
                key=lambda x: {"operating_profit": 0, "net_income": 1, "sales": 2}.get(normalize_str(x.get("fact_type")), 9),
            )
            for fact in annual_facts[:4]:
                text = normalize_str(fact.get("text_ko"))
                if not text:
                    continue
                out.append({
                    "text_ko": text,
                    "fact_type": normalize_str(fact.get("fact_type")) or "annual_financial",
                    "business_year": normalize_str(item.get("business_year")),
                    "rcept_no": normalize_str(item.get("rcept_no")),
                    "rcept_date": rcept_date,
                    "report_name": normalize_str(item.get("report_name")),
                    "relation_scope": "same_stock_report_after_filing",
                })
            break
        return out


class DartDisclosureDetailFactIndex:
    def __init__(self, csv_path: Path | None) -> None:
        self.csv_path = csv_path
        self.by_rcept_no: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.csv_path or not self.csv_path.exists():
            return

        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rcept_no = normalize_str(row.get("rcept_no"))
                if not rcept_no:
                    continue
                try:
                    facts = json.loads(row.get("facts_json") or "[]")
                except json.JSONDecodeError:
                    facts = []
                if not isinstance(facts, list) or not facts:
                    continue
                self.by_rcept_no[rcept_no].extend([x for x in facts if isinstance(x, dict)])

    def facts_for_rcept_no(self, rcept_no: Any) -> list[dict[str, Any]]:
        self.load()
        key = normalize_str(rcept_no)
        return list(self.by_rcept_no.get(key, []))


# =============================================================================
# Generic utilities
# =============================================================================


def is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "null", "nat", "<na>"}:
        return True
    return False


def normalize_str(value: Any) -> str:
    if is_nullish(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_date(value: Any) -> str:
    text = normalize_str(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    text = text.replace("/", "-").replace(".", "-")
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return text


def normalize_stock_code(value: Any) -> str:
    text = re.sub(r"[^0-9]", "", normalize_str(value))
    if not text:
        return ""
    return text.zfill(6)[-6:]


def date_ordinal(value: Any) -> int | None:
    text = normalize_date(value)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).toordinal()
    except ValueError:
        return None


def first_present(obj: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in obj and not is_nullish(obj.get(key)):
            return obj.get(key)
    return None


def as_list(value: Any) -> list[Any]:
    if is_nullish(value):
        return []
    if isinstance(value, list):
        return value
    return [value]


def to_float(value: Any) -> float | None:
    if is_nullish(value):
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    try:
        return float(text)
    except Exception:
        return None


def flatten_text(value: Any, *, max_chars: int = 12000) -> str:
    parts: list[str] = []

    def walk(x: Any, prefix: str = "") -> None:
        if len("\n".join(parts)) >= max_chars:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if k in {"raw_event_group"}:
                    # raw_event_group is often huge and duplicated. Source evidence is handled separately.
                    continue
                if isinstance(v, (dict, list)):
                    walk(v, f"{prefix}{k}.")
                elif not is_nullish(v):
                    parts.append(f"{prefix}{k}: {normalize_str(v)}")
        elif isinstance(x, list):
            for item in x:
                walk(item, prefix)
        elif not is_nullish(x):
            parts.append(normalize_str(x))

    walk(value)
    text = "\n".join(p for p in parts if p)
    return text[:max_chars]


def dedupe_dicts(rows: list[dict[str, Any]], key_fields: list[str]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row.get(k) for k in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def claim_rank(level: str) -> int:
    return CLAIM_ORDER.get(str(level or ""), 0)


def min_claim_level(a: str, b: str) -> str:
    return a if claim_rank(a) <= claim_rank(b) else b


def cap_text(text: str, max_len: int = 120) -> str:
    text = normalize_str(text)
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if is_nullish(value):
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "y", "가능", "있음"}


# =============================================================================
# Fact extraction
# =============================================================================


class StockFactExtractor:
    """Extract source-aware usable facts from a pr05e bundle.

    The extractor is deliberately conservative. It does not infer numbers or
    purposes from disclosure titles. If it finds only report dates and titles,
    the brief will be marked insufficient/borderline instead of forcing news.
    """

    def extract_all(self, bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        facts_by_source: dict[str, list[dict[str, Any]]] = {}
        for source in SOURCE_NAMES:
            items = self.get_source_items(bundle, source)
            source_facts: list[dict[str, Any]] = []
            for item_idx, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    item = {"value": item}
                evidence_id = normalize_str(item.get("evidence_id")) or f"{bundle.get('bundle_id')}:{source}:{item_idx:03d}"
                source_facts.extend(self._extract_from_item(bundle, source, item, evidence_id))
            facts_by_source[source] = dedupe_dicts(
                source_facts,
                ["source", "fact_type", "value", "evidence_id"],
            )
        return facts_by_source

    def get_source_items(self, bundle: dict[str, Any], source: str) -> list[Any]:
        """Return source evidence items from both v1 flat keys and nested evidence_by_source.

        pr05e variants have used several names. V2 accepts all of them so that
        missing macro/price/stock_event counts are visible as a wiring issue,
        not silently treated as absent.
        """
        items: list[Any] = []
        direct_keys = [
            f"{source}_evidence",
            f"{source}_evidences",
            f"{source}_items",
        ]
        aliases = {
            "dart": ["dart", "disclosure", "dart_disclosure"],
            "stock_event": ["stock_event", "stock_events", "primary_stock_event", "primary_candidate_for_news_topic"],
            "stock_event_context": ["stock_event_context", "stock_context", "supporting_stock_event", "background_stock_event"],
            "macro": ["macro", "macro_event", "macro_context", "macro_evidence"],
            "price_volume": ["price_volume", "price", "price_context", "price_volume_context", "market_reaction"],
            "gdelt": ["gdelt", "gdelt_context", "external_news"],
        }

        for key in direct_keys:
            value = bundle.get(key)
            if isinstance(value, list):
                items.extend(value)
            elif not is_nullish(value):
                items.append(value)

        nested = bundle.get("evidence_by_source")
        if isinstance(nested, dict):
            for key in aliases.get(source, [source]):
                value = nested.get(key)
                if isinstance(value, list):
                    items.extend(value)
                elif not is_nullish(value):
                    items.append(value)

        return [x for x in items if not is_nullish(x)]

    def _extract_from_item(self, bundle: dict[str, Any], source: str, item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        if source == "dart":
            return self._extract_dart_facts(bundle, item, evidence_id)
        if source in {"stock_event", "stock_event_context"}:
            return self._extract_stock_event_facts(bundle, source, item, evidence_id)
        if source == "macro":
            return self._extract_macro_facts(bundle, item, evidence_id)
        if source == "price_volume":
            return self._extract_price_volume_facts(bundle, item, evidence_id)
        if source == "gdelt":
            return self._extract_gdelt_facts(bundle, item, evidence_id)
        return []

    def _fact(
        self,
        *,
        bundle: dict[str, Any],
        source: str,
        fact_type: str,
        value: Any,
        text_ko: str,
        evidence_id: str,
        confidence: str = "medium",
        can_use_in_news: bool | None = None,
        role: str = "supporting",
    ) -> dict[str, Any]:
        value_text = normalize_str(value)
        if can_use_in_news is None:
            can_use_in_news = fact_type in STRONG_OFFICIAL_FACT_TYPES | STRONG_PRICE_FACT_TYPES | CONTEXT_FACT_TYPES
        return {
            "fact_id": "",  # Filled later by BriefBuilder.
            "bundle_id": normalize_str(bundle.get("bundle_id")),
            "source": source,
            "fact_type": fact_type,
            "value": value_text,
            "text_ko": cap_text(text_ko, 180),
            "evidence_id": evidence_id,
            "confidence": confidence,
            "can_use_in_news": bool(can_use_in_news),
            "role": role,
        }

    def _extract_dart_facts(self, bundle: dict[str, Any], item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        source = "dart"
        stock = normalize_str(bundle.get("stock_name")) or normalize_str(item.get("corp_name"))

        report_title = first_present(item, ["report_nm", "report_name", "rpt_nm", "title", "candidate_topic"])
        if report_title:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="report_title",
                    value=report_title,
                    text_ko=f"공시 제목: {normalize_str(report_title)}",
                    evidence_id=evidence_id,
                    confidence="high",
                    can_use_in_news=False,
                    role="provenance",
                )
            )

        receipt_no = first_present(item, ["rcept_no", "receipt_no", "rceptNo"])
        if receipt_no:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="receipt_no",
                    value=receipt_no,
                    text_ko=f"DART 접수번호: {normalize_str(receipt_no)}",
                    evidence_id=evidence_id,
                    confidence="high",
                    can_use_in_news=False,
                    role="provenance",
                )
            )

        date_value = first_present(item, ["rcept_dt", "receipt_date", "disclosure_date", "date", "ref_date"])
        if date_value:
            dt = normalize_date(date_value)
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="disclosure_date",
                    value=dt,
                    text_ko=f"공시일은 {dt}",
                    evidence_id=evidence_id,
                    confidence="high",
                    can_use_in_news=False,
                    role="provenance",
                )
            )

        # Structured fields first.
        facts.extend(self._extract_structured_official_fields(bundle, source, item, evidence_id))

        # Text regex fallback. Useful if later stages attach DART table text.
        text = flatten_text(item)
        facts.extend(self._extract_text_pattern_facts(bundle, source, text, evidence_id))

        # Action title is a lead fact but not enough as a supporting concrete fact.
        action = normalize_str((bundle.get("writing_frame") or {}).get("plain_action_ko"))
        if not action:
            action = self._infer_action_from_topic(normalize_str(bundle.get("candidate_topic") or report_title))
        if stock and action:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="corporate_action",
                    value=action,
                    text_ko=f"{stock}의 {action}",
                    evidence_id=evidence_id,
                    confidence="high",
                    can_use_in_news=True,
                    role="lead",
                )
            )

        return dedupe_dicts(facts, ["source", "fact_type", "value", "evidence_id"])

    def _extract_structured_official_fields(self, bundle: dict[str, Any], source: str, item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []

        # Key rules are intentionally broad because upstream datasets use mixed Korean/English names.
        rules: list[tuple[str, list[str], str]] = [
            ("amount", ["amount", "금액", "규모", "발행총액", "총액", "금전", "예정금액"], "금액"),
            ("total_amount", ["total_amount", "총발행가액", "발행총액", "배당금총액", "총금액"], "총액"),
            ("issue_price", ["issue_price", "발행가액", "발행가격", "신주발행가액"], "발행가액"),
            ("conversion_price", ["conversion_price", "전환가액", "전환가격"], "전환가액"),
            ("exercise_price", ["exercise_price", "행사가액", "행사가격"], "행사가액"),
            ("share_count", ["share_count", "shares", "주식수", "주식 수", "발행주식", "취득주식", "처분주식", "신주수"], "주식 수"),
            ("dividend_per_share", ["dividend_per_share", "주당배당금", "1주당배당금", "주당 현금배당금"], "주당 배당금"),
            ("dividend_total_amount", ["dividend_total_amount", "배당금총액", "배당총액"], "배당금 총액"),
            ("record_date", ["record_date", "배당기준일", "기준일"], "기준일"),
            ("payment_date", ["payment_date", "지급예정일", "지급일", "배당금지급"], "지급일"),
            ("subscription_date", ["subscription_date", "청약일", "청약기간"], "청약일"),
            ("payment_due_date", ["payment_due_date", "납입일", "납입기일"], "납입일"),
            ("maturity_date", ["maturity_date", "만기일", "사채만기"], "만기일"),
            ("contract_amount", ["contract_amount", "계약금액", "계약규모"], "계약금액"),
            ("contract_period", ["contract_period", "계약기간", "시작일", "종료일"], "계약기간"),
            ("counterparty", ["counterparty", "상대방", "거래상대방", "계약상대", "인수인", "양수인", "양도인"], "거래상대방"),
            ("target_company", ["target_company", "대상회사", "타법인", "회사명", "법인명"], "대상 회사"),
            ("acquisition_amount", ["acquisition_amount", "취득금액", "취득가액"], "취득금액"),
            ("disposal_amount", ["disposal_amount", "처분금액", "처분가액"], "처분금액"),
            ("stake_ratio", ["stake_ratio", "지분율", "소유비율", "비율"], "지분율"),
            ("funding_purpose", ["funding_purpose", "자금조달", "조달목적", "자금의 사용목적", "사용목적"], "자금 사용 목적"),
            ("transaction_purpose", ["transaction_purpose", "거래목적", "취득목적", "처분목적", "목적"], "거래 목적"),
            ("investment_amount", ["investment_amount", "투자금액", "투자규모"], "투자금액"),
            ("investment_period", ["investment_period", "투자기간", "투자예정기간"], "투자기간"),
            ("facility", ["facility", "시설", "투자대상", "설비", "공장", "라인"], "시설"),
            ("asset_type", ["asset_type", "자산", "유형자산", "부동산", "토지", "건물"], "자산"),
            ("court", ["court", "법원"], "법원"),
            ("case_name", ["case_name", "사건명", "소송명"], "사건명"),
            ("claim_amount", ["claim_amount", "청구금액", "소송가액"], "청구금액"),
            ("ruling_result", ["ruling_result", "판결", "결정내용", "결과"], "판결/결정"),
            ("sales", ["sales", "매출액"], "매출액"),
            ("operating_profit", ["operating_profit", "영업이익"], "영업이익"),
            ("net_income", ["net_income", "당기순이익", "순이익"], "순이익"),
            ("yoy_change", ["yoy", "전년", "증감", "변동률", "증가율", "감소율"], "전년 대비 변동"),
        ]

        for key, value in item.items():
            if is_nullish(value) or isinstance(value, (dict, list)):
                continue
            key_l = str(key).lower()
            key_ko = str(key)
            value_text = normalize_str(value)
            if not value_text or len(value_text) > 200:
                continue
            for fact_type, needles, label_ko in rules:
                if any(n.lower() in key_l or n in key_ko for n in needles):
                    # Avoid treating generic titles as target companies just because key contains 회사명.
                    if fact_type == "target_company" and value_text in {normalize_str(item.get("corp_name")), normalize_str(bundle.get("stock_name"))}:
                        continue
                    facts.append(
                        self._fact(
                            bundle=bundle,
                            source=source,
                            fact_type=fact_type,
                            value=value_text,
                            text_ko=f"{label_ko}: {value_text}",
                            evidence_id=evidence_id,
                            confidence="high",
                            can_use_in_news=True,
                            role="supporting",
                        )
                    )
                    break
        return facts

    def _extract_text_pattern_facts(self, bundle: dict[str, Any], source: str, text: str, evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        if not text:
            return facts

        # Regex-only facts are medium confidence because they may come from generic prose.
        for amount, unit in MONEY_RE.findall(text):
            value = f"{amount}{unit.replace(' ', '')}"
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="amount",
                    value=value,
                    text_ko=f"금액 표현: {value}",
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                )
            )

        for raw_date in DATE_RE.findall(text):
            dt = normalize_date(raw_date)
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="date_mentioned",
                    value=dt,
                    text_ko=f"날짜 표현: {dt}",
                    evidence_id=evidence_id,
                    confidence="low",
                    can_use_in_news=False,
                    role="provenance",
                )
            )

        for pct in PCT_RE.findall(text):
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="percentage",
                    value=f"{pct}%",
                    text_ko=f"비율 표현: {pct}%",
                    evidence_id=evidence_id,
                    confidence="low",
                    can_use_in_news=False,
                )
            )

        for number, unit in SHARE_RE.findall(text):
            value = f"{number}{unit}"
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="share_count",
                    value=value,
                    text_ko=f"주식 수 표현: {value}",
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                )
            )

        return dedupe_dicts(facts, ["source", "fact_type", "value", "evidence_id"])

    def _extract_stock_event_facts(self, bundle: dict[str, Any], source: str, item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        is_trigger = source == "stock_event"
        role_name = "trigger" if is_trigger else "background_context"
        confidence = "high" if is_trigger else "medium"

        topic = first_present(
            item,
            [
                "candidate_topic",
                "event_topic",
                "topic",
                "stock_event_topic",
                "event_name",
                "title",
                "headline",
                "stock_event_class",
            ],
        )
        if topic:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="stock_event_topic",
                    value=topic,
                    text_ko=f"종목 이벤트 주제: {normalize_str(topic)}",
                    evidence_id=evidence_id,
                    confidence=confidence,
                    can_use_in_news=True,
                    role=role_name,
                )
            )

        interpretation = first_present(
            item,
            [
                "llm_event_interpretation",
                "event_interpretation",
                "interpretation",
                "final_event_interpretation",
            ],
        )
        if interpretation:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="stock_event_interpretation",
                    value=interpretation,
                    text_ko=f"종목 이벤트 해석 라벨: {normalize_str(interpretation)}",
                    evidence_id=evidence_id,
                    confidence=confidence,
                    can_use_in_news=is_trigger,
                    role=role_name,
                )
            )

        role = first_present(item, ["evidence_role", "role", "final_allowed_usage", "allowed_usage"])
        if role:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="stock_event_role",
                    value=role,
                    text_ko=f"종목 이벤트 근거 역할: {normalize_str(role)}",
                    evidence_id=evidence_id,
                    confidence=confidence,
                    can_use_in_news=False,
                    role="provenance",
                )
            )

        sector_theme = first_present(
            item,
            [
                "sector_theme",
                "theme",
                "sector",
                "industry",
                "stock_sector_theme_context",
                "related_theme",
            ],
        )
        if sector_theme:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="sector_theme",
                    value=sector_theme,
                    text_ko=f"업종·테마 맥락: {normalize_str(sector_theme)}",
                    evidence_id=evidence_id,
                    confidence=confidence,
                    can_use_in_news=is_trigger,
                    role=role_name,
                )
            )

        peer = first_present(item, ["peer", "peer_company", "group_company", "competitor", "related_company"])
        if peer:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="peer_context",
                    value=peer,
                    text_ko=f"관련 기업 맥락: {normalize_str(peer)}",
                    evidence_id=evidence_id,
                    confidence=confidence,
                    can_use_in_news=is_trigger,
                    role=role_name,
                )
            )

        summary = first_present(item, ["summary", "description", "context", "reason", "note", "detail"])
        if summary and len(normalize_str(summary)) >= 8:
            summary_text = normalize_str(summary)
            facts.append(
                self._fact(
                    bundle=bundle,
                    source=source,
                    fact_type="stock_event_topic",
                    value=summary_text,
                    text_ko=f"종목 이벤트 설명: {cap_text(summary_text, 140)}",
                    evidence_id=evidence_id,
                    confidence=confidence,
                    can_use_in_news=True,
                    role=role_name,
                )
            )
            for sent in self._split_korean_sentences(summary_text):
                fact_sentence = self._normalize_stock_event_sentence(sent)
                salvaged, cause_clause = self._salvage_stock_event_sentence(fact_sentence)
                fact_sentence = salvaged or fact_sentence
                fact_type, can_use, role_override = self._classify_stock_event_sentence(fact_sentence, is_trigger)
                if not fact_type:
                    continue
                facts.append(
                    self._fact(
                        bundle=bundle,
                        source=source,
                        fact_type=fact_type,
                        value=fact_sentence,
                        text_ko=fact_sentence,
                        evidence_id=evidence_id,
                        confidence=confidence,
                        can_use_in_news=can_use,
                        role=role_override or role_name,
                    )
                )
                if cause_clause:
                    facts.append(
                        self._fact(
                            bundle=bundle,
                            source=source,
                            fact_type="stock_event_supporting_fact",
                            value=cause_clause,
                            text_ko=cause_clause,
                            evidence_id=evidence_id,
                            confidence=confidence,
                            can_use_in_news=True,
                            role="supporting_context",
                        )
                    )

        return dedupe_dicts(facts, ["source", "fact_type", "value", "evidence_id"])

    @staticmethod
    def _normalize_stock_event_sentence(sentence: str) -> str:
        sentence = normalize_str(sentence)
        sentence = sentence.replace("K5·스포티지", "K5와 스포티지")
        sentence = sentence.replace("현대·기아차", "현대차와 기아")
        sentence = sentence.replace("현대차·기아", "현대차와 기아")
        sentence = sentence.replace("·기아", "와 기아")
        sentence = sentence.replace("돌풍을 일으키며 ", "")
        sentence = sentence.replace("급증했다", "늘었다")
        sentence = re.sub(r"^(.+?와 .+?)가 미국 시장에서 전기차 판매가 늘었다\.$", r"\1의 미국 전기차 판매가 늘었다.", sentence)
        sentence = re.sub(r"(.+)이 기여했다\.$", r"\1이 실적에 반영됐다.", sentence)
        sentence = re.sub(r"(.+)가 기여했다\.$", r"\1가 실적에 반영됐다.", sentence)
        sentence = re.sub(r"(.+)이 주된 요인이었다\.$", r"\1이 배경으로 제시됐다.", sentence)
        sentence = re.sub(r"(.+)가 주된 요인이었다\.$", r"\1가 배경으로 제시됐다.", sentence)
        sentence = re.sub(r"^(.+?영업이익이\s+[0-9조억원, ]+[을를]?\s+넘어서며)\s+.+?을 확인했다\.$", r"\1 넘어섰다.", sentence)
        sentence = sentence.replace("넘어서며 넘어섰다", "넘어섰다")
        sentence = sentence.replace("흑자 전환에 성공했다", "흑자 전환했다")
        sentence = sentence.replace("흑자전환에 성공했다", "흑자 전환했다")
        sentence = sentence.replace("적자 전환에 성공했다", "적자 전환했다")
        sentence = sentence.replace("적자전환에 성공했다", "적자 전환했다")
        return normalize_str(sentence)

    @staticmethod
    def _salvage_stock_event_sentence(sentence: str) -> tuple[str, str]:
        """Return (result_sentence, cause_sentence).

        When a causal sentence like "D램 가격 폭락으로 영업이익이 급감했다." is
        split, the result clause is returned as the primary fact and the cause
        clause is preserved as a separate supporting fact so neither is lost.
        """
        sentence = normalize_str(sentence)
        if not sentence:
            return "", ""

        m = re.match(
            r"^(.+?(?:영업이익|영업적자|매출|실적|순이익)이?)\s+"
            r"(.{1,45}(?:가격|수요|호조|둔화|폭락|폭증|급락|급증|반등|회복).{0,45})(?:으로|로)\s+"
            r"(.+다\.)$",
            sentence,
        )
        if m:
            prefix = normalize_str(m.group(1))
            cause = normalize_str(m.group(2))
            tail = StockFactExtractor._normalize_stock_event_sentence(m.group(3))
            if (
                any(word in cause for word in STOCK_EVENT_UNSAFE_BODY_WORDS)
                or any(word in cause for word in STOCK_EVENT_CAUSE_HEAD_REWRITE_TERMS)
            ):
                return normalize_str(f"{prefix} {tail}"), cause

        m = re.match(r"^(.+?)(?:으로|로)\s+(.+다\.)$", sentence)
        if m:
            head = normalize_str(m.group(1))
            tail = StockFactExtractor._normalize_stock_event_sentence(m.group(2))
            if (
                any(word in head for word in STOCK_EVENT_UNSAFE_BODY_WORDS)
                or any(word in head for word in STOCK_EVENT_CAUSE_HEAD_REWRITE_TERMS)
            ) and STOCK_EVENT_FACT_ANCHOR_RE.search(tail):
                return tail, head

        for pattern in [r"^(.+?)에\s+따라\s+(.+다\.)$", r"^(.+?)때문에\s+(.+다\.)$"]:
            m = re.match(pattern, sentence)
            if not m:
                continue
            head = normalize_str(m.group(1))
            tail = StockFactExtractor._normalize_stock_event_sentence(m.group(2))
            if (
                any(word in head for word in STOCK_EVENT_UNSAFE_BODY_WORDS)
                or any(word in head for word in STOCK_EVENT_CAUSE_HEAD_REWRITE_TERMS)
            ) and STOCK_EVENT_FACT_ANCHOR_RE.search(tail):
                return tail, head

        return "", ""

    @staticmethod
    def _split_korean_sentences(text: str) -> list[str]:
        text = normalize_str(text)
        if not text:
            return []
        return [p.strip() for p in re.split(r"(?<=[.!?。])\s+", text) if p.strip()]

    @staticmethod
    def _classify_stock_event_sentence(sentence: str, is_trigger: bool) -> tuple[str, bool, str]:
        sentence = normalize_str(sentence)
        if not sentence or len(sentence) < 8:
            return "", False, ""

        if any(word in sentence for word in STOCK_EVENT_UNSAFE_BODY_WORDS):
            return "stock_event_restricted_sentence", False, "restricted"

        if any(word in sentence for word in STOCK_EVENT_MAIN_FACT_HINTS) or STOCK_EVENT_MAIN_FACT_RE.search(sentence):
            return "stock_event_main_fact", bool(is_trigger), "trigger" if is_trigger else "background_context"

        if any(word in sentence for word in STOCK_EVENT_SUPPORT_FACT_HINTS):
            return "stock_event_supporting_fact", bool(is_trigger), "supporting_context"

        return "stock_event_supporting_fact", False, "background_context"

    def _extract_macro_facts(self, bundle: dict[str, Any], item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        topic = first_present(item, ["macro_event", "event_name", "event", "indicator", "title", "name", "category"])
        if topic:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source="macro",
                    fact_type="macro_event",
                    value=topic,
                    text_ko=f"거시 이벤트: {normalize_str(topic)}",
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                )
            )

        direction = first_present(item, ["direction", "signal", "surprise_direction", "macro_direction"])
        if direction:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source="macro",
                    fact_type="macro_direction",
                    value=direction,
                    text_ko=f"거시 방향성: {normalize_str(direction)}",
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                )
            )

        sector = first_present(item, ["affected_sector", "sector", "industry", "related_asset", "asset_class", "exposure"])
        if sector:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source="macro",
                    fact_type="macro_sector_exposure",
                    value=sector,
                    text_ko=f"거시 노출 대상: {normalize_str(sector)}",
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                )
            )

        summary = first_present(item, ["summary", "description", "context", "note", "detail"])
        if summary and len(normalize_str(summary)) >= 8:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source="macro",
                    fact_type="macro_event",
                    value=summary,
                    text_ko=f"거시 설명: {cap_text(normalize_str(summary), 140)}",
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                )
            )
        return dedupe_dicts(facts, ["source", "fact_type", "value", "evidence_id"])

    def _extract_price_volume_facts(self, bundle: dict[str, Any], item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        source = "price_volume"
        rules = [
            ("return_1d", ["return_1d", "ret_1d", "d1_return", "price_change_1d", "one_day_return"], "1일 수익률"),
            ("return_3d", ["return_3d", "ret_3d", "d3_return", "three_day_return"], "3일 수익률"),
            ("return_5d", ["return_5d", "ret_5d", "d5_return", "five_day_return"], "5일 수익률"),
            ("event_window_return", ["event_window_return", "window_return", "cum_return", "cumulative_return"], "이벤트 구간 수익률"),
            ("abnormal_return", ["abnormal_return", "excess_return", "ar", "residual_return"], "초과수익률"),
            ("volume_z", ["volume_z", "volume_zscore", "volume_spike_z", "turnover_z"], "거래량 z-score"),
            ("volume_ratio", ["volume_ratio", "volume_multiple", "volume_vs_avg", "volume_spike_ratio"], "거래량 배율"),
            ("turnover_ratio", ["turnover_ratio", "turnover", "trading_value_ratio"], "거래대금/회전율 지표"),
            ("price_direction", ["direction", "price_direction", "return_direction"], "가격 방향"),
        ]
        for key, value in item.items():
            if is_nullish(value) or isinstance(value, (dict, list)):
                continue
            key_l = str(key).lower()
            value_text = normalize_str(value)
            for fact_type, needles, label in rules:
                if any(n in key_l for n in needles):
                    facts.append(
                        self._fact(
                            bundle=bundle,
                            source=source,
                            fact_type=fact_type,
                            value=value_text,
                            text_ko=f"{label}: {value_text}",
                            evidence_id=evidence_id,
                            confidence="high",
                            can_use_in_news=True,
                        )
                    )
                    break

        # If upstream only gives boolean flags, keep them as weak observations.
        for flag_key in ["has_price_reaction", "price_reaction", "has_strong_price_reaction", "strong_price_reaction"]:
            if flag_key in item and boolish(item.get(flag_key)):
                facts.append(
                    self._fact(
                        bundle=bundle,
                        source=source,
                        fact_type="price_reaction_flag",
                        value=flag_key,
                        text_ko=f"가격·거래량 반응 플래그: {flag_key}",
                        evidence_id=evidence_id,
                        confidence="low",
                        can_use_in_news=False,
                    )
                )
        return dedupe_dicts(facts, ["source", "fact_type", "value", "evidence_id"])

    def _extract_gdelt_facts(self, bundle: dict[str, Any], item: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        topic = first_present(item, ["theme", "themes", "title", "domain", "source_name", "summary", "description"])
        if topic:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source="gdelt",
                    fact_type="gdelt_background",
                    value=topic,
                    text_ko=f"외부 뉴스 배경: {cap_text(normalize_str(topic), 140)}",
                    evidence_id=evidence_id,
                    confidence="low",
                    can_use_in_news=True,
                )
            )
        tone = first_present(item, ["tone_score", "tone", "sentiment"])
        if tone:
            facts.append(
                self._fact(
                    bundle=bundle,
                    source="gdelt",
                    fact_type="gdelt_background",
                    value=f"tone={normalize_str(tone)}",
                    text_ko=f"GDELT 톤 점수: {normalize_str(tone)}",
                    evidence_id=evidence_id,
                    confidence="low",
                    can_use_in_news=False,
                )
            )
        return dedupe_dicts(facts, ["source", "fact_type", "value", "evidence_id"])

    def _infer_action_from_topic(self, topic: str) -> str:
        compact = re.sub(r"\s+", "", topic)
        mapping = [
            ("전환사채", "전환사채 발행"),
            ("교환사채", "교환사채 발행"),
            ("신주인수권부사채", "신주인수권부사채 발행"),
            ("유상증자", "유상증자"),
            ("무상증자", "무상증자"),
            ("자기주식취득", "자기주식 취득"),
            ("자기주식처분", "자기주식 처분"),
            ("현금ㆍ현물배당", "현금배당"),
            ("현금·현물배당", "현금배당"),
            ("타법인주식및출자증권취득", "타법인 주식 취득"),
            ("타법인주식및출자증권처분", "타법인 주식 처분"),
            ("신규시설투자", "신규 시설투자"),
            ("유형자산취득", "유형자산 취득"),
            ("유형자산처분", "유형자산 처분"),
            ("소송", "소송 관련 사항"),
            ("매출액또는손익구조", "매출액 또는 손익구조 변동"),
            ("단일판매", "단일판매·공급계약"),
            ("공급계약", "공급계약"),
        ]
        for needle, action in mapping:
            if needle in compact:
                return action
        return ""


# =============================================================================
# Brief building
# =============================================================================


class BriefBuilder:
    def __init__(self, config: Pr05fConfig):
        self.config = config
        self.extractor = StockFactExtractor()
        self.dart_annual_index = DartAnnualFinancialFactIndex(config.dart_annual_financial_facts_csv)
        self.dart_detail_index = DartDisclosureDetailFactIndex(config.dart_disclosure_detail_facts_csv)

    def build(self, bundle: dict[str, Any], index: int) -> dict[str, Any]:
        bundle_id = normalize_str(bundle.get("bundle_id")) or f"STOCK_BUNDLE_UNKNOWN_{index:06d}"
        facts_by_source = self.extractor.extract_all(bundle)
        detail_facts = self._dart_disclosure_detail_facts(bundle)
        if detail_facts:
            facts_by_source["dart_disclosure_detail"] = detail_facts
        annual_facts = self._annual_financial_facts(bundle)
        if annual_facts:
            facts_by_source["dart_annual_financial"] = annual_facts
        source_evidence_counts = self._source_evidence_counts(bundle)
        if detail_facts:
            source_evidence_counts["dart_disclosure_detail"] = len({f.get("evidence_id", "") for f in detail_facts})
        if annual_facts:
            source_evidence_counts["dart_annual_financial"] = 1
        all_facts = self._assign_fact_ids(bundle_id, facts_by_source)

        writing_frame = bundle.get("writing_frame") if isinstance(bundle.get("writing_frame"), dict) else {}
        precheck = bundle.get("bundle_precheck") if isinstance(bundle.get("bundle_precheck"), dict) else {}
        source_caps = bundle.get("source_caps") if isinstance(bundle.get("source_caps"), dict) else {}

        stock_name = normalize_str(bundle.get("stock_name"))
        candidate_topic = normalize_str(bundle.get("candidate_topic"))
        event_family = normalize_str(bundle.get("event_family"))
        action_type = normalize_str(writing_frame.get("action_type"))
        plain_action_ko = normalize_str(writing_frame.get("plain_action_ko")) or self.extractor._infer_action_from_topic(candidate_topic)
        corporate_actor = normalize_str(writing_frame.get("corporate_actor")) or stock_name

        lead_fact = self._build_lead_fact(corporate_actor, plain_action_ko, candidate_topic, event_family)
        editorial_angle = self._build_editorial_angle(stock_name, plain_action_ko, candidate_topic, event_family)

        scoring = self._score_facts(bundle, all_facts, precheck)
        allowed_claim_level = self._determine_allowed_claim_level(bundle, all_facts)
        news_type = self._determine_news_type(bundle, all_facts, scoring, allowed_claim_level)
        readiness = self._determine_readiness(bundle, all_facts, scoring, news_type, action_type)

        raw_supporting_facts = self._select_supporting_fact_texts(all_facts, allowed_claim_level)
        write_safe_facts, restricted_facts = self._split_write_safe_facts(
            facts=all_facts,
            raw_supporting_facts=raw_supporting_facts,
            allowed_claim_level=allowed_claim_level,
        )
        fact_layers = self._build_fact_layers(all_facts, allowed_claim_level)
        related_fact_groups = self._build_related_fact_groups(all_facts, allowed_claim_level)
        readiness = self._apply_write_safety_gate(readiness, news_type, write_safe_facts)
        supporting_facts = write_safe_facts
        source_usage_plan = self._build_source_usage_plan(bundle, all_facts, allowed_claim_level)
        do_not_claim = self._build_do_not_claim(allowed_claim_level)
        writing_guidance = self._build_writing_guidance(readiness, news_type, allowed_claim_level)

        concrete_facts = {
            "dart_facts": facts_by_source.get("dart", []),
            "dart_disclosure_detail_facts": facts_by_source.get("dart_disclosure_detail", []),
            "dart_annual_financial_facts": facts_by_source.get("dart_annual_financial", []),
            "stock_event_facts": facts_by_source.get("stock_event", []),
            "stock_event_context_facts": facts_by_source.get("stock_event_context", []),
            "macro_context_facts": facts_by_source.get("macro", []),
            "price_volume_facts": facts_by_source.get("price_volume", []),
            "gdelt_background_facts": facts_by_source.get("gdelt", []),
        }

        brief = {
            "brief_id": f"STOCK_BRIEF_{index:06d}",
            "bundle_id": bundle_id,
            "event_group_id": bundle.get("event_group_id", ""),
            "stock_code": bundle.get("stock_code", ""),
            "stock_name": stock_name,
            "anchor_date": bundle.get("anchor_date", ""),
            "candidate_topic": candidate_topic,
            "primary_topic_source": bundle.get("primary_topic_source", ""),
            "event_family": event_family,
            "action_type": action_type,
            "plain_action_ko": plain_action_ko,
            "news_type": news_type,
            "generation_readiness": readiness["generation_readiness"],
            "readiness_reason": readiness["readiness_reason"],
            "brief_quality_tier": readiness["brief_quality_tier"],
            "allowed_claim_level": allowed_claim_level,
            "max_allowed_market_claim_level_pre_llm": bundle.get("max_allowed_market_claim_level_pre_llm", ""),
            "corroboration_level": bundle.get("corroboration_level", ""),
            "directional_consistency": self._extract_directional_consistency(bundle),
            "editorial_angle_ko": editorial_angle,
            "lead_fact_ko": lead_fact,
            "supporting_facts_ko": supporting_facts,
            "raw_supporting_facts_ko": raw_supporting_facts,
            "write_safe_facts_ko": write_safe_facts,
            "main_source_facts_ko": fact_layers["main_source_facts_ko"],
            "supporting_context_facts_ko": fact_layers["supporting_context_facts_ko"],
            "official_detail_facts_ko": fact_layers["official_detail_facts_ko"],
            "background_facts_ko": fact_layers["background_facts_ko"],
            "annual_financial_context_facts_ko": fact_layers["annual_financial_context_facts_ko"],
            "related_fact_groups_ko": related_fact_groups,
            "restricted_facts_ko": restricted_facts,
            "write_safe_fact_count": len(write_safe_facts),
            "main_source_fact_count": len(fact_layers["main_source_facts_ko"]),
            "supporting_context_fact_count": len(fact_layers["supporting_context_facts_ko"]),
            "official_detail_write_fact_count": len(fact_layers["official_detail_facts_ko"]),
            "annual_financial_context_fact_count": len(fact_layers["annual_financial_context_facts_ko"]),
            "related_fact_group_count": len(related_fact_groups),
            "max_related_fact_group_fact_count": max((len(g.get("facts_ko", [])) for g in related_fact_groups), default=0),
            "restricted_fact_count": len(restricted_facts),
            "concrete_facts": concrete_facts,
            "source_evidence_counts": source_evidence_counts,
            "dart_evidence_count": source_evidence_counts.get("dart", 0),
            "dart_disclosure_detail_fact_count": len(facts_by_source.get("dart_disclosure_detail", [])),
            "dart_annual_financial_fact_count": len(facts_by_source.get("dart_annual_financial", [])),
            "stock_event_evidence_count": source_evidence_counts.get("stock_event", 0),
            "stock_event_context_evidence_count": source_evidence_counts.get("stock_event_context", 0),
            "macro_evidence_count": source_evidence_counts.get("macro", 0),
            "price_volume_evidence_count": source_evidence_counts.get("price_volume", 0),
            "gdelt_evidence_count": source_evidence_counts.get("gdelt", 0),
            "fact_density_score": scoring["fact_density_score"],
            "official_detail_fact_count": scoring["official_detail_fact_count"],
            "stock_event_trigger_fact_count": scoring["stock_event_trigger_fact_count"],
            "stock_event_context_fact_count": scoring["stock_event_context_fact_count"],
            "stock_context_fact_count": scoring["stock_context_fact_count"],
            "macro_context_fact_count": scoring["macro_context_fact_count"],
            "price_volume_fact_count": scoring["price_volume_fact_count"],
            "gdelt_context_fact_count": scoring["gdelt_context_fact_count"],
            "annual_financial_context_scoring_count": scoring["annual_financial_context_fact_count"],
            "provenance_fact_count": scoring["provenance_fact_count"],
            "usable_fact_count": scoring["usable_fact_count"],
            "missing_fact_types": self._missing_fact_types(bundle, action_type, all_facts),
            "source_usage_plan": source_usage_plan,
            "do_not_claim": do_not_claim,
            "writing_guidance": writing_guidance,
            "source_caps": source_caps,
            "bundle_precheck": precheck,
            "generation_policy": bundle.get("generation_policy", {}),
            "writing_frame": writing_frame,
        }

        if self.config.include_raw_bundle:
            brief["source_bundle"] = bundle

        return brief

    def _assign_fact_ids(self, bundle_id: str, facts_by_source: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        all_facts: list[dict[str, Any]] = []
        counter = 0
        for source in SOURCE_NAMES:
            for fact in facts_by_source.get(source, []):
                counter += 1
                fact["fact_id"] = f"{bundle_id}:fact:{counter:03d}"
                all_facts.append(fact)
        return all_facts

    def _source_evidence_counts(self, bundle: dict[str, Any]) -> dict[str, int]:
        return {source: len(self.extractor.get_source_items(bundle, source)) for source in SOURCE_NAMES}

    def _dart_disclosure_detail_facts(self, bundle: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self.extractor.get_source_items(bundle, "dart"):
            rcept_no = first_present(item, ["rcept_no", "receipt_no", "rceptNo"])
            if not rcept_no:
                evidence_id = normalize_str(item.get("evidence_id"))
                if evidence_id.startswith("DART_"):
                    rcept_no = evidence_id.replace("DART_", "", 1)
            rcept_no = normalize_str(rcept_no)
            if not rcept_no:
                continue

            for fact in self.dart_detail_index.facts_for_rcept_no(rcept_no):
                fact_type = normalize_str(fact.get("fact_type")) or "dart_disclosure_detail"
                text = normalize_str(fact.get("text_ko"))
                if not text:
                    continue
                out.append(
                    self.extractor._fact(
                        bundle=bundle,
                        source="dart_disclosure_detail",
                        fact_type=fact_type,
                        value=text,
                        text_ko=text,
                        evidence_id=rcept_no,
                        confidence="high",
                        can_use_in_news=True,
                        role="official_detail",
                    )
                )
                out[-1]["relation_scope"] = "same_dart_rcept_no"
        return dedupe_dicts(out, ["source", "fact_type", "value", "evidence_id"])[:12]

    def _annual_financial_facts(self, bundle: dict[str, Any]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for item in self.dart_annual_index.facts_for_bundle(bundle):
            evidence_id = normalize_str(item.get("rcept_no"))
            fact_type = normalize_str(item.get("fact_type")) or "annual_financial"
            facts.append(
                self.extractor._fact(
                    bundle=bundle,
                    source="dart_annual_financial",
                    fact_type=fact_type,
                    value=normalize_str(item.get("text_ko")),
                    text_ko=normalize_str(item.get("text_ko")),
                    evidence_id=evidence_id,
                    confidence="medium",
                    can_use_in_news=True,
                    role="annual_financial_context",
                )
            )
            facts[-1]["relation_scope"] = normalize_str(item.get("relation_scope")) or "same_stock_report_after_filing"
            facts[-1]["rcept_date"] = normalize_str(item.get("rcept_date"))
            facts[-1]["business_year"] = normalize_str(item.get("business_year"))
        return facts[:4]

    def _build_lead_fact(self, corporate_actor: str, plain_action_ko: str, candidate_topic: str, event_family: str) -> str:
        actor = corporate_actor or "해당 기업"
        if plain_action_ko:
            return f"{actor}의 {plain_action_ko}"
        if candidate_topic:
            clean = re.sub(r"\s+", " ", candidate_topic).strip()
            return f"{actor}의 {clean}"
        if event_family:
            return f"{actor}의 {event_family} 관련 사안"
        return f"{actor} 관련 사안"

    def _build_editorial_angle(self, stock_name: str, plain_action_ko: str, candidate_topic: str, event_family: str) -> str:
        subject = stock_name or "해당 종목"
        if plain_action_ko:
            return f"{subject} {plain_action_ko} 중심의 종목 단신"
        if candidate_topic:
            return f"{subject} {candidate_topic} 중심의 종목 단신"
        if event_family:
            return f"{subject} {event_family} 중심의 종목 단신"
        return f"{subject} 종목 단신"

    def _score_facts(self, bundle: dict[str, Any], facts: list[dict[str, Any]], precheck: dict[str, Any]) -> dict[str, int]:
        official = 0
        stock_event_trigger = 0
        stock_event_context = 0
        macro_context = 0
        price_volume = 0
        gdelt_context = 0
        annual_financial_context = 0
        provenance = 0
        usable = 0

        for fact in facts:
            fact_type = fact.get("fact_type", "")
            source = fact.get("source", "")
            if fact.get("can_use_in_news"):
                usable += 1
            if fact_type in STRONG_OFFICIAL_FACT_TYPES and source in {"dart", "dart_disclosure_detail"} and fact.get("can_use_in_news"):
                official += 1
            elif source == "stock_event" and fact_type in CONTEXT_FACT_TYPES and fact.get("can_use_in_news"):
                stock_event_trigger += 1
            elif source == "stock_event_context" and fact_type in CONTEXT_FACT_TYPES:
                stock_event_context += 1
            elif source == "macro" and fact_type in CONTEXT_FACT_TYPES and fact.get("can_use_in_news"):
                macro_context += 1
            elif source == "price_volume" and fact_type in STRONG_PRICE_FACT_TYPES and fact.get("can_use_in_news"):
                price_volume += 1
            elif source == "gdelt" and fact_type == "gdelt_background" and fact.get("can_use_in_news"):
                gdelt_context += 1
            elif source == "dart_annual_financial" and fact.get("can_use_in_news"):
                annual_financial_context += 1
            elif fact_type in PROVENANCE_FACT_TYPES or not fact.get("can_use_in_news"):
                provenance += 1

        has_dart_action = any(f.get("fact_type") == "corporate_action" for f in facts)
        has_price_flag = bool(precheck.get("has_price_reaction"))
        has_stock_trigger_signal = bool(precheck.get("has_stock_event_trigger")) or stock_event_trigger > 0

        # V2: background context alone does not raise readiness. It can only support an already-valid lead.
        score = 0
        score += official * 2
        score += stock_event_trigger * 2
        score += macro_context
        score += price_volume * 2
        score += gdelt_context
        score += annual_financial_context
        if has_dart_action:
            score += 1
        if has_stock_trigger_signal:
            score += 1
        if has_price_flag:
            score += 1

        return {
            "fact_density_score": int(score),
            "official_detail_fact_count": int(official),
            "stock_event_trigger_fact_count": int(stock_event_trigger),
            "stock_event_context_fact_count": int(stock_event_context),
            "stock_context_fact_count": int(stock_event_trigger + stock_event_context),
            "macro_context_fact_count": int(macro_context),
            "price_volume_fact_count": int(price_volume),
            "gdelt_context_fact_count": int(gdelt_context),
            "annual_financial_context_fact_count": int(annual_financial_context),
            "provenance_fact_count": int(provenance),
            "usable_fact_count": int(usable),
        }

    def _determine_allowed_claim_level(self, bundle: dict[str, Any], facts: list[dict[str, Any]]) -> str:
        ceiling = normalize_str(bundle.get("max_allowed_market_claim_level_pre_llm")) or "no_market_claim"
        # pr05f is not a judge. It cannot raise the pr05e ceiling.
        allowed = ceiling

        has_price_fact = any(f.get("source") == "price_volume" and f.get("fact_type") in STRONG_PRICE_FACT_TYPES for f in facts)
        if has_price_fact:
            # Observed reaction can only be used if pr05e ceiling permits reaction_only or above.
            if claim_rank(allowed) >= claim_rank("reaction_only"):
                return min_claim_level(allowed, "reaction_only")
            return "no_market_claim"

        if claim_rank(allowed) <= 0:
            return "insufficient_evidence"
        return min_claim_level(allowed, "no_market_claim")

    def _determine_news_type(
        self,
        bundle: dict[str, Any],
        facts: list[dict[str, Any]],
        scoring: dict[str, int],
        allowed_claim_level: str,
    ) -> str:
        has_dart = len(self.extractor.get_source_items(bundle, "dart")) > 0
        has_stock_trigger = scoring["stock_event_trigger_fact_count"] > 0
        has_stock_context = scoring["stock_event_context_fact_count"] > 0
        has_macro_context = scoring["macro_context_fact_count"] > 0
        has_price = scoring["price_volume_fact_count"] > 0 and claim_rank(allowed_claim_level) >= claim_rank("reaction_only")
        has_official_detail = scoring["official_detail_fact_count"] > 0

        if has_price:
            return "market_reaction_observed"
        if has_dart and has_official_detail:
            return "corporate_action_disclosure"
        if has_stock_trigger and has_macro_context:
            return "macro_exposure_context"
        if has_stock_trigger:
            return "stock_event_trigger"
        if has_stock_context:
            return "context_only"
        if has_dart:
            return "sparse_disclosure"
        if has_macro_context or scoring["gdelt_context_fact_count"] > 0:
            return "mixed_context"
        return "do_not_generate"

    def _determine_readiness(
        self,
        bundle: dict[str, Any],
        facts: list[dict[str, Any]],
        scoring: dict[str, int],
        news_type: str,
        action_type: str,
    ) -> dict[str, str]:
        official = scoring["official_detail_fact_count"]
        trigger = scoring["stock_event_trigger_fact_count"]
        macro_ctx = scoring["macro_context_fact_count"]
        price = scoring["price_volume_fact_count"]
        gdelt = scoring["gdelt_context_fact_count"]
        generic_action = action_type in GENERIC_ACTION_TYPES
        has_lead = any(f.get("fact_type") == "corporate_action" for f in facts)

        if news_type == "corporate_action_disclosure" and official >= 2 and has_lead and not generic_action:
            return {
                "generation_readiness": "ready",
                "brief_quality_tier": "A",
                "readiness_reason": "official_action_with_multiple_concrete_details",
            }
        if news_type == "corporate_action_disclosure" and official == 1 and has_lead and not generic_action:
            return {
                "generation_readiness": "borderline",
                "brief_quality_tier": "C",
                "readiness_reason": "official_action_has_only_one_specific_detail",
            }
        if news_type == "stock_event_trigger" and trigger >= 2:
            return {
                "generation_readiness": "ready",
                "brief_quality_tier": "B",
                "readiness_reason": "stock_event_trigger_has_multiple_usable_facts",
            }
        if news_type == "stock_event_trigger" and trigger == 1:
            return {
                "generation_readiness": "borderline",
                "brief_quality_tier": "C",
                "readiness_reason": "stock_event_trigger_has_only_one_usable_fact",
            }
        if news_type == "macro_exposure_context" and trigger >= 1 and macro_ctx >= 1:
            return {
                "generation_readiness": "borderline",
                "brief_quality_tier": "C",
                "readiness_reason": "macro_context_requires_cautious_background_framing",
            }
        if news_type == "market_reaction_observed" and price >= 2:
            return {
                "generation_readiness": "ready",
                "brief_quality_tier": "B",
                "readiness_reason": "price_volume_observation_has_multiple_metrics",
            }
        if news_type == "market_reaction_observed" and price == 1:
            return {
                "generation_readiness": "borderline",
                "brief_quality_tier": "C",
                "readiness_reason": "price_volume_observation_has_only_one_metric",
            }
        if news_type == "context_only":
            return {
                "generation_readiness": "insufficient_specific_facts",
                "brief_quality_tier": "D",
                "readiness_reason": "stock_event_context_is_background_only_not_article_lead",
            }
        if news_type in {"sparse_disclosure", "do_not_generate", "mixed_context"}:
            return {
                "generation_readiness": "insufficient_specific_facts",
                "brief_quality_tier": "D",
                "readiness_reason": "no_valid_article_lead_with_specific_supporting_facts",
            }
        return {
            "generation_readiness": "insufficient_specific_facts",
            "brief_quality_tier": "D",
            "readiness_reason": "only_title_or_sparse_context_available",
        }

    def _apply_write_safety_gate(
        self,
        readiness: dict[str, str],
        news_type: str,
        write_safe_facts: list[str],
    ) -> dict[str, str]:
        """Downgrade ready/borderline briefs if no safe writing material remains.

        This is intentionally applied after fact scoring. A fact may be valid
        evidence but still unsafe for direct article wording under no_market_claim.
        """
        current = readiness.get("generation_readiness", "insufficient_specific_facts")
        if current not in {"ready", "borderline"}:
            return readiness

        safe_count = len([x for x in write_safe_facts if normalize_str(x)])
        if safe_count >= 2:
            return readiness
        if safe_count == 1:
            if current == "ready":
                return {
                    "generation_readiness": "borderline",
                    "brief_quality_tier": "C",
                    "readiness_reason": f"{readiness.get('readiness_reason', '')}; only_one_write_safe_fact_after_filter",
                }
            return readiness
        return {
            "generation_readiness": "insufficient_specific_facts",
            "brief_quality_tier": "D",
            "readiness_reason": f"{readiness.get('readiness_reason', '')}; no_write_safe_fact_after_filter",
        }

    def _split_write_safe_facts(
        self,
        *,
        facts: list[dict[str, Any]],
        raw_supporting_facts: list[str],
        allowed_claim_level: str,
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Split raw supporting facts into direct-write and restricted facts.

        - write_safe_facts_ko: safe text fragments pr06 may copy or paraphrase.
        - restricted_facts_ko: evidence/background that should not be copied as-is.
        """
        source_by_text: dict[str, dict[str, str]] = {}
        for f in facts:
            t = normalize_str(f.get("text_ko"))
            if not t:
                continue
            source_by_text[t] = {
                "source": normalize_str(f.get("source")),
                "fact_type": normalize_str(f.get("fact_type")),
                "fact_id": normalize_str(f.get("fact_id")),
            }

        write_safe: list[str] = []
        restricted: list[dict[str, str]] = []

        for text in raw_supporting_facts:
            text = normalize_str(text)
            if not text:
                continue
            meta = source_by_text.get(text, {"source": "", "fact_type": "", "fact_id": ""})
            safe_text, reason = self._sanitize_fact_text_for_claim_level(text, allowed_claim_level)
            if safe_text:
                if safe_text not in write_safe:
                    write_safe.append(safe_text)
                if reason:
                    restricted.append({
                        "text_ko": text,
                        "reason": reason,
                        "safe_rewrite_ko": safe_text,
                        **meta,
                    })
            else:
                restricted.append({
                    "text_ko": text,
                    "reason": reason or "blocked_for_article_writing",
                    "safe_rewrite_ko": "",
                    **meta,
                })

        return write_safe[:8], restricted[:20]

    def _build_fact_layers(self, facts: list[dict[str, Any]], allowed_claim_level: str) -> dict[str, list[str]]:
        layers = {
            "main_source_facts_ko": [],
            "supporting_context_facts_ko": [],
            "official_detail_facts_ko": [],
            "background_facts_ko": [],
            "annual_financial_context_facts_ko": [],
        }

        for fact in facts:
            if not fact.get("can_use_in_news"):
                continue

            text = normalize_str(fact.get("text_ko"))
            if not text:
                continue

            safe_text, _reason = self._sanitize_fact_text_for_claim_level(text, allowed_claim_level)
            if not safe_text:
                continue

            source = normalize_str(fact.get("source"))
            fact_type = normalize_str(fact.get("fact_type"))
            role = normalize_str(fact.get("role"))

            if source in {"dart", "dart_disclosure_detail"} and fact_type in STRONG_OFFICIAL_FACT_TYPES:
                self._append_unique(layers["official_detail_facts_ko"], safe_text)
            elif source == "dart_disclosure_detail" and fact_type in DART_DETAIL_CONTEXT_FACT_TYPES:
                self._append_unique(layers["supporting_context_facts_ko"], safe_text)
            elif source == "stock_event" and fact_type == "stock_event_main_fact":
                self._append_unique(layers["main_source_facts_ko"], safe_text)
            elif source == "stock_event" and fact_type == "stock_event_supporting_fact":
                self._append_unique(layers["supporting_context_facts_ko"], safe_text)
            elif source in {"stock_event_context", "macro", "gdelt"}:
                self._append_unique(layers["background_facts_ko"], safe_text)
            elif source == "dart_annual_financial":
                self._append_unique(layers["annual_financial_context_facts_ko"], safe_text)
            elif role == "lead" and source == "dart":
                self._append_unique(layers["main_source_facts_ko"], safe_text)

        return {key: value[:6] for key, value in layers.items()}

    def _build_related_fact_groups(self, facts: list[dict[str, Any]], allowed_claim_level: str) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}

        for fact in facts:
            if not fact.get("can_use_in_news"):
                continue

            text = normalize_str(fact.get("text_ko"))
            if not text:
                continue

            safe_text, _reason = self._sanitize_fact_text_for_claim_level(text, allowed_claim_level)
            if not safe_text:
                continue

            source = normalize_str(fact.get("source"))
            evidence_id = normalize_str(fact.get("evidence_id")) or normalize_str(fact.get("fact_id"))
            fact_type = normalize_str(fact.get("fact_type"))
            role = normalize_str(fact.get("role"))
            key = (source, evidence_id)

            group = grouped.setdefault(
                key,
                {
                    "source": source,
                    "evidence_id": evidence_id,
                    "relation_scope": "same_evidence_id",
                    "main_facts_ko": [],
                    "supporting_facts_ko": [],
                    "official_detail_facts_ko": [],
                    "background_facts_ko": [],
                },
            )

            if source in {"dart", "dart_disclosure_detail"} and fact_type in STRONG_OFFICIAL_FACT_TYPES:
                self._append_unique(group["official_detail_facts_ko"], safe_text)
            elif source == "dart_disclosure_detail" and fact_type in DART_DETAIL_CONTEXT_FACT_TYPES:
                self._append_unique(group["supporting_facts_ko"], safe_text)
            elif source == "stock_event" and fact_type == "stock_event_main_fact":
                self._append_unique(group["main_facts_ko"], safe_text)
            elif source == "stock_event" and fact_type == "stock_event_supporting_fact":
                self._append_unique(group["supporting_facts_ko"], safe_text)
            elif role == "lead" and source == "dart":
                self._append_unique(group["main_facts_ko"], safe_text)
            elif source in {"stock_event_context", "macro", "gdelt"}:
                self._append_unique(group["background_facts_ko"], safe_text)

        related_groups: list[dict[str, Any]] = []
        for group in grouped.values():
            main = group["official_detail_facts_ko"] + group["main_facts_ko"]
            support = group["supporting_facts_ko"]
            if not main:
                continue

            facts_ko = []
            for text in main + support:
                self._append_unique(facts_ko, text)
            if not facts_ko:
                continue

            group["facts_ko"] = facts_ko[:4]
            group["fact_count"] = len(group["facts_ko"])
            group["has_supporting_fact"] = bool(support)
            related_groups.append(group)

        related_groups.sort(
            key=lambda g: (
                int(g.get("fact_count", 0)),
                int(bool(g.get("has_supporting_fact"))),
                1 if g.get("source") == "dart" else 0,
            ),
            reverse=True,
        )
        return related_groups[:6]

    @staticmethod
    def _append_unique(items: list[str], text: str) -> None:
        text = normalize_str(text)
        if text and text not in items:
            items.append(text)

    def _sanitize_fact_text_for_claim_level(self, text: str, allowed_claim_level: str) -> tuple[str, str]:
        """Return safe text and restriction reason.

        For no_market_claim, remove sentences/clauses that mention observed or
        implied market reaction. This preserves concrete event sentences such as
        an acquisition amount while blocking '주가가 상승했다'.
        """
        text = normalize_str(text)
        if not text:
            return "", "empty_text"

        # Provenance-style labels are safe but may contain internal wording.
        text = text.replace("종목 이벤트 설명:", "종목 이벤트:").replace("종목 이벤트 주제:", "종목 이벤트 주제:")

        if claim_rank(allowed_claim_level) >= claim_rank("reaction_only"):
            cleaned = self._remove_ai_style_markers(text)
            return cleaned, "" if cleaned == text else "ai_or_editorial_marker_removed"

        sentences = self._split_korean_sentences(text)
        kept: list[str] = []
        removed: list[str] = []
        for sent in sentences:
            sent = normalize_str(sent)
            if not sent:
                continue
            if self._has_market_reaction_word(sent):
                removed.append(sent)
                continue
            cleaned = self._remove_ai_style_markers(sent)
            if cleaned:
                kept.append(cleaned)

        if kept:
            safe = " ".join(kept)
            reason = "market_reaction_sentence_removed" if removed else ""
            return safe, reason
        if removed:
            return "", "blocked_market_reaction_or_sentiment"

        cleaned = self._remove_ai_style_markers(text)
        if not cleaned:
            return "", "blocked_ai_or_editorial_marker"
        return cleaned, "" if cleaned == text else "ai_or_editorial_marker_removed"

    @staticmethod
    def _split_korean_sentences(text: str) -> list[str]:
        text = normalize_str(text)
        if not text:
            return []
        # Keep punctuation with the sentence when possible.
        parts = re.split(r"(?<=[.!?。])\s+", text)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _has_market_reaction_word(text: str) -> bool:
        compact = normalize_str(text)
        return any(word in compact for word in MARKET_REACTION_WORDS)

    @staticmethod
    def _remove_ai_style_markers(text: str) -> str:
        cleaned = normalize_str(text)
        # Remove only isolated internal labels. Do not over-clean factual event text.
        cleaned = cleaned.replace("중심의 종목 단신", "")
        for word in AI_OR_ANALYST_STYLE_WORDS:
            if word in {"보인다", "알려졌다", "전망된다", "예상된다"}:
                continue
            cleaned = cleaned.replace(word, "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,·-/")
        return cleaned

    def _select_supporting_fact_texts(self, facts: list[dict[str, Any]], allowed_claim_level: str) -> list[str]:
        def priority(f: dict[str, Any]) -> tuple[int, int, str]:
            fact_type = f.get("fact_type", "")
            source = f.get("source", "")
            if source in {"dart", "dart_disclosure_detail"} and fact_type in STRONG_OFFICIAL_FACT_TYPES:
                p = 0
            elif source == "dart_disclosure_detail" and fact_type in DART_DETAIL_CONTEXT_FACT_TYPES:
                p = 1
            elif source == "price_volume" and fact_type in STRONG_PRICE_FACT_TYPES:
                p = 2 if claim_rank(allowed_claim_level) >= claim_rank("reaction_only") else 9
            elif source == "stock_event" and fact_type == "stock_event_main_fact":
                p = 2
            elif source == "stock_event" and fact_type == "stock_event_supporting_fact":
                p = 3
            elif source in {"stock_event", "stock_event_context"}:
                p = 4
            elif source == "macro":
                p = 5
            elif source == "gdelt":
                p = 6
            else:
                p = 8
            return (p, 0 if f.get("confidence") == "high" else 1, normalize_str(f.get("text_ko")))

        selected: list[str] = []
        for fact in sorted(facts, key=priority):
            if not fact.get("can_use_in_news"):
                continue
            if fact.get("source") == "dart_annual_financial":
                continue
            if fact.get("source") == "price_volume" and claim_rank(allowed_claim_level) < claim_rank("reaction_only"):
                continue
            text = normalize_str(fact.get("text_ko"))
            if not text:
                continue
            if text not in selected:
                selected.append(text)
            if len(selected) >= 8:
                break
        return selected

    def _build_source_usage_plan(self, bundle: dict[str, Any], facts: list[dict[str, Any]], allowed_claim_level: str) -> dict[str, Any]:
        has = {s: any(f.get("source") == s for f in facts) for s in SOURCE_NAMES}
        return {
            "dart": {
                "available": has["dart"],
                "allowed_usage": "official corporate-action fact only; do not infer market impact or business effect",
            },
            "dart_disclosure_detail": {
                "available": has["dart_disclosure_detail"],
                "allowed_usage": "official detail facts from the exact DART rcept_no; may support article detail but not market impact",
            },
            "stock_event": {
                "available": has["stock_event"],
                "allowed_usage": "article lead only when this is a primary stock_event trigger; do not convert context into causality",
            },
            "stock_event_context": {
                "available": has["stock_event_context"],
                "allowed_usage": "background context only; cannot by itself make a brief ready or borderline",
            },
            "macro": {
                "available": has["macro"],
                "allowed_usage": "background context only; no direct cause claim for the stock",
            },
            "price_volume": {
                "available": has["price_volume"],
                "allowed_usage": "observed price/volume movement only" if claim_rank(allowed_claim_level) >= claim_rank("reaction_only") else "blocked unless later claim level permits reaction_only",
            },
            "gdelt": {
                "available": has["gdelt"],
                "allowed_usage": "background corroboration only; not a direct factual article source unless summarized fact is explicit",
            },
        }

    def _build_do_not_claim(self, allowed_claim_level: str) -> list[str]:
        claims = [
            "호재/악재 단정",
            "투자자 심리 추정",
            "시장 관심/주목 표현",
            "공시 목적 또는 효과 추정",
            "거시 이벤트가 종목 움직임을 유발했다는 직접 인과",
            "DART 제목만 보고 금액·목적·상대방 생성",
        ]
        if claim_rank(allowed_claim_level) < claim_rank("reaction_only"):
            claims.extend([
                "주가 상승·하락 언급",
                "거래량 증가 언급",
                "공시 이후 시장 반응 언급",
            ])
        else:
            claims.extend([
                "주가/거래량 변화의 원인 단정",
                "공시와 가격 반응을 인과적으로 붙이는 문장 배치",
            ])
        return claims

    def _build_writing_guidance(self, readiness: dict[str, str], news_type: str, allowed_claim_level: str) -> dict[str, Any]:
        if readiness["generation_readiness"] == "ready":
            sentence_count = "2_to_4"
        elif readiness["generation_readiness"] == "borderline":
            sentence_count = "2"
        else:
            sentence_count = "reject_or_hold"
        return {
            "recommended_sentence_count": sentence_count,
            "style": "concise Korean financial wire article; factual, not analytical",
            "allowed_news_type": news_type,
            "allowed_claim_level": allowed_claim_level,
            "must_use": [
                "lead_fact_ko",
                "write_safe_facts_ko only; do not copy raw_supporting_facts_ko",
                "at least one write_safe_facts_ko item for accepted news",
                "do not use stock_event_context as the lead fact",
            ],
            "avoid_phrases": [
                "이번 결정",
                "이번 공시",
                "보인다",
                "알려졌다",
                "추후 공지될 예정이다",
                "공개되지 않았다",
                "결정 자료에는 금액과 일정 항목이 포함됐다",
                "이벤트",
                "맥락",
                "의미",
                "해석",
                "주목",
            ],
        }

    def _extract_directional_consistency(self, bundle: dict[str, Any]) -> str:
        pre = bundle.get("bundle_precheck") if isinstance(bundle.get("bundle_precheck"), dict) else {}
        value = pre.get("directional_consistency")
        return normalize_str(value) or "unknown"

    def _missing_fact_types(self, bundle: dict[str, Any], action_type: str, facts: list[dict[str, Any]]) -> list[str]:
        present = {normalize_str(f.get("fact_type")) for f in facts if f.get("can_use_in_news")}
        action = normalize_str(action_type)
        required_by_action = {
            "convertible_bond": ["amount", "conversion_price", "maturity_date", "funding_purpose"],
            "exchangeable_bond": ["amount", "conversion_price", "maturity_date", "funding_purpose"],
            "bond_with_warrant": ["amount", "exercise_price", "maturity_date", "funding_purpose"],
            "rights_issue": ["share_count", "issue_price", "payment_due_date", "funding_purpose"],
            "bonus_issue": ["share_count", "record_date"],
            "treasury_acquire": ["share_count", "amount", "acquisition_amount", "period", "transaction_purpose"],
            "treasury_dispose": ["share_count", "amount", "disposal_amount", "counterparty", "period", "transaction_purpose"],
            "treasury_dispose_result": ["share_count", "disposal_amount", "period"],
            "cash_dividend": ["dividend_per_share", "dividend_total_amount", "record_date", "payment_date"],
            "equity_acquire": ["target_company", "acquisition_amount", "stake_ratio", "transaction_purpose"],
            "equity_dispose": ["target_company", "disposal_amount", "stake_ratio", "counterparty"],
            "new_facility_investment": ["investment_amount", "investment_period", "facility", "transaction_purpose"],
            "asset_acquire": ["asset_type", "acquisition_amount", "counterparty", "transaction_purpose"],
            "asset_dispose": ["asset_type", "disposal_amount", "counterparty", "transaction_purpose"],
            "legal_regulatory": ["case_name", "court", "claim_amount", "ruling_result"],
            "financial_result_change": ["sales", "operating_profit", "net_income", "yoy_change"],
            "contract": ["contract_amount", "counterparty", "contract_period"],
        }
        required = required_by_action.get(action, ["amount", "period", "counterparty", "purpose"])
        # Alias handling.
        aliases = {
            "period": {"contract_period", "investment_period", "payment_date", "maturity_date", "record_date", "subscription_date", "payment_due_date"},
            "purpose": {"funding_purpose", "transaction_purpose"},
            "amount": {"amount", "total_amount", "contract_amount", "investment_amount", "acquisition_amount", "disposal_amount", "dividend_total_amount"},
        }
        missing: list[str] = []
        for req in required:
            candidates = aliases.get(req, {req})
            if not (present & candidates):
                missing.append(req)
        return missing[:8]


# =============================================================================
# Writers / report
# =============================================================================


class BriefWriter:
    def __init__(self, config: Pr05fConfig):
        self.config = config

    def write_all(self, briefs: list[dict[str, Any]]) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        JsonlIO.write_jsonl(self.config.briefs_jsonl_path, briefs)
        self._write_briefs_csv(briefs)
        self._write_facts_csv(briefs)
        self._write_report(briefs)

    def _write_briefs_csv(self, briefs: list[dict[str, Any]]) -> None:
        fields = [
            "brief_id",
            "bundle_id",
            "stock_code",
            "stock_name",
            "anchor_date",
            "event_family",
            "action_type",
            "candidate_topic",
            "news_type",
            "generation_readiness",
            "brief_quality_tier",
            "readiness_reason",
            "allowed_claim_level",
            "fact_density_score",
            "dart_evidence_count",
            "dart_disclosure_detail_fact_count",
            "stock_event_evidence_count",
            "stock_event_context_evidence_count",
            "macro_evidence_count",
            "price_volume_evidence_count",
            "gdelt_evidence_count",
            "dart_annual_financial_fact_count",
            "official_detail_fact_count",
            "stock_event_trigger_fact_count",
            "stock_event_context_fact_count",
            "stock_context_fact_count",
            "macro_context_fact_count",
            "price_volume_fact_count",
            "gdelt_context_fact_count",
            "annual_financial_context_scoring_count",
            "usable_fact_count",
            "write_safe_fact_count",
            "main_source_fact_count",
            "supporting_context_fact_count",
            "official_detail_write_fact_count",
            "annual_financial_context_fact_count",
            "related_fact_group_count",
            "max_related_fact_group_fact_count",
            "restricted_fact_count",
            "editorial_angle_ko",
            "lead_fact_ko",
            "supporting_facts_ko",
            "write_safe_facts_ko",
            "main_source_facts_ko",
            "supporting_context_facts_ko",
            "official_detail_facts_ko",
            "background_facts_ko",
            "annual_financial_context_facts_ko",
            "related_fact_groups_ko",
            "raw_supporting_facts_ko",
            "restricted_facts_ko",
            "missing_fact_types",
        ]
        with self.config.briefs_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for b in briefs:
                row = {k: b.get(k, "") for k in fields}
                row["supporting_facts_ko"] = json.dumps(b.get("supporting_facts_ko", []), ensure_ascii=False)
                row["write_safe_facts_ko"] = json.dumps(b.get("write_safe_facts_ko", []), ensure_ascii=False)
                row["main_source_facts_ko"] = json.dumps(b.get("main_source_facts_ko", []), ensure_ascii=False)
                row["supporting_context_facts_ko"] = json.dumps(b.get("supporting_context_facts_ko", []), ensure_ascii=False)
                row["official_detail_facts_ko"] = json.dumps(b.get("official_detail_facts_ko", []), ensure_ascii=False)
                row["background_facts_ko"] = json.dumps(b.get("background_facts_ko", []), ensure_ascii=False)
                row["annual_financial_context_facts_ko"] = json.dumps(b.get("annual_financial_context_facts_ko", []), ensure_ascii=False)
                row["related_fact_groups_ko"] = json.dumps(b.get("related_fact_groups_ko", []), ensure_ascii=False)
                row["raw_supporting_facts_ko"] = json.dumps(b.get("raw_supporting_facts_ko", []), ensure_ascii=False)
                row["restricted_facts_ko"] = json.dumps(b.get("restricted_facts_ko", []), ensure_ascii=False)
                row["missing_fact_types"] = json.dumps(b.get("missing_fact_types", []), ensure_ascii=False)
                writer.writerow(row)

    def _write_facts_csv(self, briefs: list[dict[str, Any]]) -> None:
        fields = [
            "brief_id",
            "bundle_id",
            "stock_code",
            "stock_name",
            "anchor_date",
            "source",
            "fact_id",
            "fact_type",
            "value",
            "text_ko",
            "evidence_id",
            "confidence",
            "can_use_in_news",
            "role",
        ]
        with self.config.facts_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for b in briefs:
                concrete = b.get("concrete_facts", {}) if isinstance(b.get("concrete_facts"), dict) else {}
                for group in concrete.values():
                    for fact in as_list(group):
                        if not isinstance(fact, dict):
                            continue
                        row = {k: "" for k in fields}
                        row.update({
                            "brief_id": b.get("brief_id", ""),
                            "bundle_id": b.get("bundle_id", ""),
                            "stock_code": b.get("stock_code", ""),
                            "stock_name": b.get("stock_name", ""),
                            "anchor_date": b.get("anchor_date", ""),
                        })
                        for k in ["source", "fact_id", "fact_type", "value", "text_ko", "evidence_id", "confidence", "can_use_in_news", "role"]:
                            row[k] = fact.get(k, "")
                        writer.writerow(row)

    def _write_report(self, briefs: list[dict[str, Any]]) -> None:
        lines: list[str] = []
        lines.append("# pr05f stock news brief report")
        lines.append("")
        lines.append("## Input / output")
        lines.append(f"- input: {self.config.bundles_jsonl}")
        lines.append(f"- output_jsonl: {self.config.briefs_jsonl_path}")
        lines.append(f"- output_csv: {self.config.briefs_csv_path}")
        lines.append(f"- facts_csv: {self.config.facts_csv_path}")
        lines.append("")

        self._counter_section(lines, "generation_readiness", Counter(b.get("generation_readiness", "") for b in briefs))
        self._counter_section(lines, "brief_quality_tier", Counter(b.get("brief_quality_tier", "") for b in briefs))
        self._counter_section(lines, "news_type", Counter(b.get("news_type", "") for b in briefs))
        self._counter_section(lines, "allowed_claim_level", Counter(b.get("allowed_claim_level", "") for b in briefs))
        self._counter_section(lines, "event_family", Counter(b.get("event_family", "") for b in briefs))

        lines.append("## Readiness principle")
        lines.append("- pr05f does not write final news.")
        lines.append("- pr05f may mark DART-title-only bundles as insufficient even when pr05e preserved them as valid evidence bundles.")
        lines.append("- price/volume facts are observation-only and cannot be turned into event causality.")
        lines.append("- macro and GDELT are background/context unless a later stage explicitly permits stronger use.")
        lines.append("- pr06 should prefer ready A/B briefs and reject D-tier briefs.")
        lines.append("")

        self.config.report_path.write_text("\n".join(lines), encoding="utf-8")

    def _counter_section(self, lines: list[str], title: str, counter: Counter) -> None:
        lines.append(f"## {title}")
        if not counter:
            lines.append("- none: 0")
        else:
            for k, v in counter.most_common():
                lines.append(f"- {k or '<blank>'}: {v}")
        lines.append("")


# =============================================================================
# Pipeline
# =============================================================================


class Pr05fPipeline:
    def __init__(self, config: Pr05fConfig):
        self.config = config
        self.builder = BriefBuilder(config)
        self.writer = BriefWriter(config)

    def run(self) -> None:
        print("=" * 100)
        print("[pr05f] Build stock news briefs")
        print(f"bundles_jsonl: {self.config.bundles_jsonl}")
        print(f"output_dir: {self.config.output_dir}")
        print(f"max_bundles: {self.config.max_bundles}")
        print("=" * 100)

        bundles = JsonlIO.read_jsonl(self.config.bundles_jsonl, max_rows=self.config.max_bundles)
        print(f"[load] bundles: {len(bundles):,}")

        briefs = [self.builder.build(bundle, idx) for idx, bundle in enumerate(bundles, start=1)]
        self.writer.write_all(briefs)
        self._print_summary(briefs)

    def _print_summary(self, briefs: list[dict[str, Any]]) -> None:
        print("\n[summary]")
        print(f"total_briefs: {len(briefs):,}")
        for title, counter in [
            ("generation_readiness", Counter(b.get("generation_readiness", "") for b in briefs)),
            ("brief_quality_tier", Counter(b.get("brief_quality_tier", "") for b in briefs)),
            ("news_type", Counter(b.get("news_type", "") for b in briefs)),
            ("allowed_claim_level", Counter(b.get("allowed_claim_level", "") for b in briefs)),
        ]:
            print(f"\n[{title}]")
            for k, v in counter.most_common():
                print(f"{k or '<blank>'}: {v:,}")

        print("\n[outputs]")
        print(f"jsonl: {self.config.briefs_jsonl_path}")
        print(f"csv:   {self.config.briefs_csv_path}")
        print(f"facts: {self.config.facts_csv_path}")
        print(f"report:{self.config.report_path}")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build source-aware stock-news brief cards from pr05e evidence bundles."
    )
    parser.add_argument("--bundles-jsonl", type=Path, default=DEFAULT_BUNDLES_JSONL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dart-annual-financial-facts-csv", type=Path, default=DEFAULT_DART_ANNUAL_FINANCIAL_FACTS_CSV)
    parser.add_argument("--dart-disclosure-detail-facts-csv", type=Path, default=DEFAULT_DART_DISCLOSURE_DETAIL_FACTS_CSV)
    parser.add_argument("--max-bundles", type=int, default=None)
    parser.add_argument("--min-ready-score", type=int, default=4)
    parser.add_argument("--include-raw-bundle", action="store_true")
    parser.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    parser.set_defaults(overwrite=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = Pr05fConfig(
        bundles_jsonl=args.bundles_jsonl,
        output_dir=args.output_dir,
        dart_annual_financial_facts_csv=args.dart_annual_financial_facts_csv,
        dart_disclosure_detail_facts_csv=args.dart_disclosure_detail_facts_csv,
        max_bundles=args.max_bundles,
        min_ready_score=args.min_ready_score,
        include_raw_bundle=args.include_raw_bundle,
        overwrite=args.overwrite,
    )
    pipeline = Pr05fPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
