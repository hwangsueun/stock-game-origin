#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr05e_build_stock_evidence_bundles.py

Build stock evidence bundles from pr05d event groups.

This script does NOT call an LLM and does NOT generate news text.
It preserves all event groups as bundles, computes deterministic market-claim ceilings,
creates generation safety policies, writing frames, and compact batch-style judge inputs.

REVISION (anti-AI-tone writing layer)
--------------------------------------
The writing_frame no longer hands the generator a complete lead sentence
(recommended_lead_template_ko). A full sentence invites the model to paraphrase
a template, producing disclosure-restatement tone ("...를 공시했다.").

Instead, each bundle now carries structured action primitives:

    action_type, plain_action_ko, corporate_actor, object_ko,
    allowed_verbs_ko, avoid_verbs_ko, usable_fact_slots,
    sentence_style, do_not_reuse_template

The downstream generator (pr06) constructs a natural short Korean wire-news
sentence from these primitives. A legacy recommended_lead_template_ko is still
emitted but flagged do_not_reuse_template=True and marked as reference only,
so existing consumers do not break.

Evidence-safety logic (claim ceilings, source caps, combination rules,
generation policy, judge selection) is unchanged.

Default input:
  /Users/hgs/Desktop/IISE CD/data/interim/pr05d_stock_event_groups/stock_event_groups.jsonl

Default output dir:
  /Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None


# =============================================================================
# Constants / Claim ladder
# =============================================================================


class ClaimLevel(str, Enum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NO_MARKET_CLAIM = "no_market_claim"
    REACTION_ONLY = "reaction_only"
    PLAUSIBLE_MARKET_CONTEXT = "plausible_market_context"
    LIKELY_CONTRIBUTOR = "likely_contributor"
    STRONGEST_ATTRIBUTABLE_DISCLOSED_FACTOR = "strongest_attributable_disclosed_factor"


CLAIM_LEVEL_ORDER: Dict[str, int] = {
    ClaimLevel.INSUFFICIENT_EVIDENCE.value: 0,
    ClaimLevel.NO_MARKET_CLAIM.value: 1,
    ClaimLevel.REACTION_ONLY.value: 2,
    ClaimLevel.PLAUSIBLE_MARKET_CONTEXT.value: 3,
    ClaimLevel.LIKELY_CONTRIBUTOR.value: 4,
    ClaimLevel.STRONGEST_ATTRIBUTABLE_DISCLOSED_FACTOR.value: 5,
}

LEGACY_CLAIM_LEVEL_MAP: Dict[str, str] = {
    "primary_market_driver_candidate": ClaimLevel.STRONGEST_ATTRIBUTABLE_DISCLOSED_FACTOR.value,
    "dominant_disclosed_factor": ClaimLevel.STRONGEST_ATTRIBUTABLE_DISCLOSED_FACTOR.value,
    "strongest_attributable_factor": ClaimLevel.STRONGEST_ATTRIBUTABLE_DISCLOSED_FACTOR.value,
}

SOURCE_NAMES = ["dart", "stock_event", "stock_event_context", "price_volume", "gdelt", "macro"]

EVIDENCE_FIELD_CANDIDATES: Dict[str, List[str]] = {
    "dart": ["dart_evidence", "dart_items", "dart_item", "dart", "disclosure_items"],
    "stock_event": ["stock_event_evidence", "stock_event_items", "stock_events", "stock_event"],
    "stock_event_context": [
        "stock_event_context_evidence",
        "stock_event_context_items",
        "stock_context_items",
        "context_stock_event_items",
    ],
    "price_volume": [
        "price_volume_evidence",
        "price_volume_items",
        "price_items",
        "reaction_items",
        "price_volume",
    ],
    "gdelt": ["gdelt_evidence", "gdelt_items", "gdelt"],
    "macro": ["macro_evidence", "macro_items", "macro"],
}

STRONG_EVENT_FAMILIES = {
    "earnings",
    "guidance",
    "contract",
    "investment",
    "asset_transaction",
    "equity_investment",
    "capital_financing",
    "business_transfer",
    "legal_regulatory",
    "trading_status",
    "listing_risk",
    "major_management_matter",
    "management_governance",
}

ROUTINE_EVENT_FAMILIES = {
    "dividend",
    "treasury_stock",
}

USABLE_EVENT_FAMILIES = STRONG_EVENT_FAMILIES | ROUTINE_EVENT_FAMILIES

WEAK_EVENT_FAMILIES = {
    "other_company_event",
    "unclear",
    "unknown",
    "other",
    "misc",
}

# Ranking must not feed ceiling computation.
# It is used only for judge cost / priority selection.


# =============================================================================
# Config
# =============================================================================


@dataclass(frozen=True)
class Pr05eConfig:
    event_groups_jsonl: Path
    output_dir: Path
    model: str = "gpt-4o-mini"
    max_judge_inputs_per_stock_year: int = 8
    max_dividend_per_stock_year: int = 0
    max_treasury_per_stock_year: int = 1
    max_other_per_stock_year: int = 1
    max_official_no_price_per_stock_year: int = 3
    overwrite: bool = True

    @property
    def bundles_jsonl_path(self) -> Path:
        return self.output_dir / "stock_evidence_bundles.jsonl"

    @property
    def bundles_csv_path(self) -> Path:
        return self.output_dir / "stock_evidence_bundles.csv"

    @property
    def judge_inputs_jsonl_path(self) -> Path:
        return self.output_dir / "bundle_judge_inputs.jsonl"

    @property
    def report_path(self) -> Path:
        return self.output_dir / "stock_evidence_bundle_report.md"


# =============================================================================
# Utility functions
# =============================================================================


def is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "null", "nat", "<na>"}:
        return True
    return False


def normalize_stock_code(value: Any) -> str:
    if is_nullish(value):
        return ""
    text = str(value).strip()
    text = text.replace(".0", "")
    text = re.sub(r"[^0-9]", "", text)
    if not text:
        return ""
    return text.zfill(6)[-6:]


def normalize_date(value: Any) -> str:
    if is_nullish(value):
        return ""

    text = str(value).strip()

    # YYYYMMDD
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return text

    # YYYY-MM-DD... / YYYY/MM/DD...
    text = text.replace("/", "-")
    match = re.search(r"(\d{4}-\d{1,2}-\d{1,2})", text)
    if match:
        candidate = match.group(1)
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return candidate

    # pandas fallback if available
    if pd is not None:
        try:
            dt = pd.to_datetime(value, errors="coerce")
            if pd.notna(dt):
                return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    return text


def extract_year(date_text: str) -> str:
    if not date_text:
        return "unknown"
    match = re.match(r"(\d{4})", date_text)
    return match.group(1) if match else "unknown"


def parse_bool(value: Any, default: bool = False) -> bool:
    if is_nullish(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return default


def first_present(obj: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in obj and not is_nullish(obj[key]):
            return obj[key]
    return default


def as_list(value: Any) -> List[Any]:
    if is_nullish(value):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                return as_list(parsed)
            except Exception:
                return [value]
        return [value]
    return [value]


def json_dumps_safe(value: Any, *, indent: Optional[int] = None) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, Path):
            return str(o)
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)

    return json.dumps(value, ensure_ascii=False, default=default, indent=indent)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
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
                raise ValueError(f"JSONL row must be object at {path}:{line_no}")
            rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json_dumps_safe(row) + "\n")


def compact_text(value: Any, limit: int = 220) -> str:
    if is_nullish(value):
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_claim_level(value: Any) -> str:
    if is_nullish(value):
        return ClaimLevel.INSUFFICIENT_EVIDENCE.value
    text = str(value).strip()
    text = LEGACY_CLAIM_LEVEL_MAP.get(text, text)
    if text not in CLAIM_LEVEL_ORDER:
        return ClaimLevel.INSUFFICIENT_EVIDENCE.value
    return text


def claim_rank(level: str) -> int:
    return CLAIM_LEVEL_ORDER[normalize_claim_level(level)]


def min_claim(*levels: str) -> str:
    levels = [normalize_claim_level(x) for x in levels]
    return min(levels, key=claim_rank)


def max_claim(*levels: str) -> str:
    levels = [normalize_claim_level(x) for x in levels]
    return max(levels, key=claim_rank)


def cap_claim(level: str, ceiling: str) -> str:
    return min_claim(level, ceiling)


def list_count(value: Any) -> int:
    return len(as_list(value))


def normalize_directional_consistency(value: Any, *, has_price_reaction: bool) -> str:
    if not has_price_reaction:
        return "not_applicable"
    if is_nullish(value):
        return "not_evaluated"
    text = str(value).strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    if text in {"consistent", "directionally_consistent", "aligned", "match", "matched"}:
        return "consistent"
    if text in {"inconsistent", "directionally_inconsistent", "conflict", "conflicted", "divergent"}:
        return "inconsistent"
    if text in {"unknown", "not_evaluated", "na", "n_a", "not_applicable"}:
        return "not_evaluated"
    return text




class TopicFamilyRefiner:
    """Refine event_family using Korean disclosure/report title keywords.

    pr05d groups preserve a broad family, but some Korean report names contain
    generic words such as "계약" that can mislead the frame builder. This class
    corrects the writing/permission family using the representative topic while
    preserving the original family separately.
    """

    ORDERED_RULES = [
        ("treasury_stock", [
            "자기주식취득신탁계약",
            "자기주식 취득 신탁계약",
            "자기주식취득결과보고서",
            "자기주식처분결과보고서",
            "자기주식취득결정",
            "자기주식처분결정",
            "자기주식",
        ]),
        ("dividend", [
            "현금ㆍ현물배당결정",
            "현금·현물배당결정",
            "현금배당결정",
            "현물배당결정",
            "배당결정",
        ]),
        ("earnings", [
            "매출액또는손익구조",
            "영업실적",
            "잠정실적",
            "실적",
            "영업이익",
            "당기순이익",
        ]),
        ("capital_financing", [
            "유상증자",
            "무상증자",
            "전환사채권발행",
            "전환사채 발행",
            "교환사채권발행",
            "교환사채 발행",
            "신주인수권부사채",
            "감자결정",
            "증자결정",
            "사채권발행",
        ]),
        ("equity_investment", [
            "타법인주식및출자증권취득",
            "타법인주식및출자증권처분",
            "타법인 주식",
            "출자증권",
        ]),
        ("investment", [
            "신규시설투자",
            "시설투자",
            "투자등",
        ]),
        ("asset_transaction", [
            "유형자산취득",
            "유형자산처분",
            "유형자산 양수",
            "유형자산 양도",
        ]),
        ("business_transfer", [
            "영업양수",
            "영업양도",
            "회사합병",
            "합병결정",
            "분할결정",
            "분할합병",
            "주식의포괄적교환",
            "주식의포괄적이전",
        ]),
        ("legal_regulatory", [
            "소송등의제기",
            "소송등의판결",
            "소송",
            "제재",
            "벌금",
            "과징금",
        ]),
        ("trading_status", [
            "매매거래정지",
            "거래정지",
            "정지해제",
        ]),
        ("listing_risk", [
            "관리종목",
            "상장폐지",
            "투자유의안내",
            "상장적격성",
        ]),
        ("management_governance", [
            "대표이사",
            "임원",
            "최대주주",
            "경영권",
        ]),
        ("contract", [
            "단일판매",
            "공급계약",
            "판매계약",
            "수주",
        ]),
        ("major_management_matter", [
            "투자판단관련주요경영사항",
            "주요경영사항",
        ]),
    ]

    @classmethod
    def refine(cls, family: str, topic: str, evidence_by_source: Dict[str, List[Any]] | None = None) -> str:
        """Refine family with candidate_topic taking priority over noisy grouped evidence.

        Important:
        pr05d groups can contain multiple nearby DART items. If a dividend disclosure
        and a financial-result-change disclosure are grouped together, evidence-wide
        keyword matching can incorrectly turn "매출액또는손익구조..." into dividend.
        Therefore clear candidate_topic patterns must win first.
        """
        topic_text = str(topic or "").replace(" ", "")

        # ---------------------------------------------------------------------
        # Candidate-topic hard overrides.
        # These must win over noisy neighboring evidence inside the same group.
        # ---------------------------------------------------------------------
        if "매출액또는손익구조" in topic_text or "손익구조" in topic_text:
            return "earnings"

        if "영업실적등에대한전망" in topic_text or "실적전망" in topic_text or "전망(공정공시)" in topic_text:
            return "earnings"

        if (
            "현금ㆍ현물배당" in topic_text
            or "현금·현물배당" in topic_text
            or "현금배당" in topic_text
            or "현물배당" in topic_text
            or "배당결정" in topic_text
            or "배당금" in topic_text
        ):
            return "dividend"

        if "자기주식취득신탁계약" in topic_text or "자기주식취득신탁" in topic_text:
            return "treasury_stock"

        if "자기주식" in topic_text:
            return "treasury_stock"

        if "단일판매" in topic_text or "공급계약" in topic_text or "판매계약" in topic_text or "수주" in topic_text:
            return "contract"

        if (
            "유상증자" in topic_text
            or "무상증자" in topic_text
            or "전환사채" in topic_text
            or "교환사채" in topic_text
            or "신주인수권부사채" in topic_text
            or "감자" in topic_text
            or "사채권발행" in topic_text
        ):
            return "capital_financing"

        if "타법인주식및출자증권" in topic_text or "타법인주식" in topic_text or "출자증권" in topic_text:
            return "equity_investment"

        if "신규시설투자" in topic_text or "시설투자" in topic_text or "설비투자" in topic_text:
            return "investment"

        if "유형자산취득" in topic_text or "유형자산처분" in topic_text:
            return "asset_transaction"

        if "소송등의제기" in topic_text or "소송등의신청" in topic_text or "소송등의판결" in topic_text or "소송등의결정" in topic_text or "경영권분쟁소송" in topic_text:
            return "legal_regulatory"

        # ---------------------------------------------------------------------
        # Existing evidence-wide fallback logic.
        # ---------------------------------------------------------------------
        text_parts = [str(topic or "")]
        if evidence_by_source:
            for source in ["dart", "stock_event", "stock_event_context"]:
                for item in evidence_by_source.get(source, []):
                    if isinstance(item, dict):
                        for key in ["report_nm", "source_report_name", "candidate_topic", "topic", "title", "headline", "event_name"]:
                            value = item.get(key)
                            if not is_nullish(value):
                                text_parts.append(str(value))

        text = " ".join(text_parts).replace(" ", "")
        for refined_family, keywords in cls.ORDERED_RULES:
            for keyword in keywords:
                if keyword.replace(" ", "") in text:
                    return refined_family

        return family or "other_company_event"

class KoreanTextHelper:
    """Small Korean surface-form helpers for lead templates."""

    SUBJECT_EXCEPTIONS = {
        "LG": "LG는",
        "HMM": "HMM은",
        "S-Oil": "S-Oil은",
        "S-OIL": "S-OIL은",
        "DB하이텍": "DB하이텍은",
        "SK하이닉스": "SK하이닉스는",
    }

    @staticmethod
    def has_final_consonant(syllable: str) -> bool:
        if not syllable:
            return False
        code = ord(syllable)
        if not (0xAC00 <= code <= 0xD7A3):
            return False
        return ((code - 0xAC00) % 28) != 0

    @classmethod
    def topic_particle(cls, company: Any) -> str:
        """Return the 은/는 topic particle appropriate for the company name.

        For Latin-only names not in the exception table, fall back to "은/는"
        guidance so the generator can choose, rather than guessing wrong.
        """
        name = str(company or "회사").strip() or "회사"
        if name in cls.SUBJECT_EXCEPTIONS:
            return cls.SUBJECT_EXCEPTIONS[name][len(name):]
        for ch in reversed(name):
            if 0xAC00 <= ord(ch) <= 0xD7A3:
                return "은" if cls.has_final_consonant(ch) else "는"
        return "은/는"

    @classmethod
    def subject(cls, company: Any) -> str:
        name = str(company or "회사").strip() or "회사"
        if name in cls.SUBJECT_EXCEPTIONS:
            return cls.SUBJECT_EXCEPTIONS[name]
        # Prefer the last Hangul syllable if the company name mixes Latin and Korean.
        for ch in reversed(name):
            if 0xAC00 <= ord(ch) <= 0xD7A3:
                return name + ("은" if cls.has_final_consonant(ch) else "는")
        # For non-Hangul names, avoid a wrong heuristic by using a comma-style lead.
        return name + ","

    @staticmethod
    def clean_topic(topic: Any) -> str:
        text = compact_text(topic, 160)
        replacements = {
            "[첨부추가]": "",
            "[기재정정]": "",
            "주요사항보고서": "",
            "(자회사의 주요경영사항)": "",
            "(종속회사의주요경영사항)": "",
            "(종속회사의 주요경영사항)": "",
            "(자율공시)": "",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        text = text.replace("()", "")
        text = re.sub(r"\s+", " ", text).strip()
        return text

# =============================================================================
# Data normalization
# =============================================================================


class EventGroupLoader:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(f"event_groups_jsonl not found: {self.path}")
        return read_jsonl(self.path)


class EventGroupNormalizer:
    """Normalize the pr05d event-group object into a defensive intermediate dict."""

    def normalize(self, raw: Dict[str, Any], index: int) -> Dict[str, Any]:
        group_id = first_present(
            raw,
            ["event_group_id", "stock_event_group_id", "group_id", "id"],
            default=f"STOCK_EVT_GROUP_{index:06d}",
        )
        stock_code = normalize_stock_code(first_present(raw, ["stock_code", "ticker", "code"]))
        stock_name = str(first_present(raw, ["stock_name", "corp_name", "company_name", "name"], default="")).strip()
        anchor_date = normalize_date(first_present(raw, ["anchor_date", "event_date", "ref_date", "date", "rcept_dt"]))
        event_family = self._normalize_event_family(
            first_present(raw, ["event_family", "family", "group_family", "representative_family"], default="")
        )

        evidence_by_source = self._extract_evidence_by_source(raw)
        event_family = event_family or self._infer_event_family(raw, evidence_by_source) or "other_company_event"

        candidate_topic = str(
            first_present(
                raw,
                [
                    "candidate_topic",
                    "topic",
                    "event_topic",
                    "representative_topic",
                    "event_name",
                    "report_nm",
                    "disclosure_title",
                    "title",
                ],
                default="",
            )
        ).strip()
        if not candidate_topic:
            candidate_topic = self._infer_candidate_topic(evidence_by_source) or event_family

        original_event_family = event_family
        event_family = TopicFamilyRefiner.refine(event_family, candidate_topic, evidence_by_source)

        primary_topic_source = str(
            first_present(raw, ["primary_topic_source", "primary_source", "topic_source"], default="")
        ).strip().lower()
        if not primary_topic_source:
            primary_topic_source = self._infer_primary_topic_source(evidence_by_source)

        return {
            "event_group_id": str(group_id),
            "stock_code": stock_code,
            "stock_name": stock_name,
            "anchor_date": anchor_date,
            "candidate_topic": candidate_topic,
            "primary_topic_source": primary_topic_source,
            "event_family": event_family,
            "event_family_original": original_event_family,
            "event_family_refined": event_family != original_event_family,
            "raw_event_group": raw,
            "evidence_by_source": evidence_by_source,
        }

    def _extract_evidence_by_source(self, raw: Dict[str, Any]) -> Dict[str, List[Any]]:
        result: Dict[str, List[Any]] = {source: [] for source in SOURCE_NAMES}

        # Direct top-level evidence fields.
        for source, candidates in EVIDENCE_FIELD_CANDIDATES.items():
            for key in candidates:
                if key in raw and not is_nullish(raw[key]):
                    result[source].extend(as_list(raw[key]))

        # Nested containers sometimes used by previous processors.
        for container_key in ["evidence", "source_evidence", "evidence_by_source", "items_by_source"]:
            nested = raw.get(container_key)
            if isinstance(nested, dict):
                for source in SOURCE_NAMES:
                    if source in nested:
                        result[source].extend(as_list(nested[source]))

        # Defensive dedupe by JSON string.
        deduped: Dict[str, List[Any]] = {}
        for source, items in result.items():
            seen = set()
            clean_items: List[Any] = []
            for item in items:
                marker = json_dumps_safe(item)
                if marker in seen:
                    continue
                seen.add(marker)
                clean_items.append(item)
            deduped[source] = clean_items
        return deduped

    def _normalize_event_family(self, value: Any) -> str:
        if is_nullish(value):
            return ""
        text = str(value).strip().lower()
        text = text.replace(" ", "_").replace("-", "_")
        return text

    def _infer_event_family(self, raw: Dict[str, Any], evidence_by_source: Dict[str, List[Any]]) -> str:
        for key in ["event_family", "family", "group_family", "representative_family"]:
            value = first_present(raw, [key])
            if not is_nullish(value):
                return self._normalize_event_family(value)

        for source in ["dart", "stock_event", "stock_event_context"]:
            for item in evidence_by_source.get(source, []):
                if isinstance(item, dict):
                    value = first_present(item, ["event_family", "family", "stock_event_class", "event_type"])
                    if not is_nullish(value):
                        return self._normalize_event_family(value)
        return ""

    def _infer_candidate_topic(self, evidence_by_source: Dict[str, List[Any]]) -> str:
        key_candidates = [
            "candidate_topic",
            "topic",
            "event_topic",
            "report_nm",
            "source_report_name",
            "title",
            "headline",
            "event_name",
            "summary",
        ]
        for source in ["dart", "stock_event", "stock_event_context", "gdelt", "macro", "price_volume"]:
            for item in evidence_by_source.get(source, []):
                if isinstance(item, dict):
                    value = first_present(item, key_candidates)
                    if not is_nullish(value):
                        return compact_text(value, 160)
                elif not is_nullish(item):
                    return compact_text(item, 160)
        return ""

    def _infer_primary_topic_source(self, evidence_by_source: Dict[str, List[Any]]) -> str:
        if evidence_by_source.get("dart"):
            return "dart"
        if evidence_by_source.get("stock_event"):
            return "stock_event"
        if evidence_by_source.get("stock_event_context"):
            return "stock_event_context"
        if evidence_by_source.get("price_volume"):
            return "price_volume"
        if evidence_by_source.get("gdelt"):
            return "gdelt"
        if evidence_by_source.get("macro"):
            return "macro"
        return "unknown"


class EvidenceItemNormalizer:
    def normalize_items(self, bundle_id: str, source: str, items: List[Any]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for idx, item in enumerate(items, start=1):
            if isinstance(item, dict):
                obj = dict(item)
            else:
                obj = {"value": item}

            evidence_id = first_present(obj, ["evidence_id", "source_evidence_id", "id", "uid"])
            if is_nullish(evidence_id):
                evidence_id = f"{bundle_id}:{source}:{idx:03d}"

            obj["evidence_id"] = str(evidence_id)
            obj.setdefault("evidence_source", source)
            normalized.append(obj)
        return normalized


# =============================================================================
# Bundle construction
# =============================================================================


class EvidenceBundleBuilder:
    def __init__(self):
        self.item_normalizer = EvidenceItemNormalizer()

    def build(self, normalized_group: Dict[str, Any], index: int) -> Dict[str, Any]:
        bundle_id = f"STOCK_BUNDLE_{index:06d}"
        evidence_by_source = normalized_group["evidence_by_source"]

        evidence = {
            source: self.item_normalizer.normalize_items(bundle_id, source, evidence_by_source.get(source, []))
            for source in SOURCE_NAMES
        }

        bundle: Dict[str, Any] = {
            "bundle_id": bundle_id,
            "event_group_id": normalized_group["event_group_id"],
            "stock_code": normalized_group["stock_code"],
            "stock_name": normalized_group["stock_name"],
            "anchor_date": normalized_group["anchor_date"],
            "candidate_topic": normalized_group["candidate_topic"],
            "primary_topic_source": normalized_group["primary_topic_source"],
            "event_family": normalized_group["event_family"],
            "event_family_original": normalized_group.get("event_family_original", normalized_group["event_family"]),
            "event_family_refined": normalized_group.get("event_family_refined", False),
            "dart_evidence": evidence["dart"],
            "stock_event_evidence": evidence["stock_event"],
            "stock_event_context_evidence": evidence["stock_event_context"],
            "price_volume_evidence": evidence["price_volume"],
            "gdelt_evidence": evidence["gdelt"],
            "macro_evidence": evidence["macro"],
            "raw_event_group": normalized_group["raw_event_group"],
        }
        return bundle


# =============================================================================
# Precheck / ranking / corroboration
# =============================================================================


class BundlePrecheckEvaluator:
    def evaluate(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        raw = bundle.get("raw_event_group", {}) if isinstance(bundle.get("raw_event_group"), dict) else {}

        has_dart = bool(bundle.get("dart_evidence"))
        has_stock_event_trigger = self._has_stock_event_trigger(bundle)
        has_stock_event_context = bool(bundle.get("stock_event_context_evidence"))
        has_price_reaction = self._has_price_reaction(bundle, raw)
        has_strong_price_reaction = self._has_strong_price_reaction(bundle, raw, has_price_reaction)
        has_gdelt_support = bool(bundle.get("gdelt_evidence"))
        has_macro_background = bool(bundle.get("macro_evidence"))
        directional_consistency = normalize_directional_consistency(
            self._find_directional_consistency(bundle, raw),
            has_price_reaction=has_price_reaction,
        )

        return {
            "has_official_evidence": has_dart,
            "has_dart": has_dart,
            "has_stock_event_trigger": has_stock_event_trigger,
            "has_stock_event_context": has_stock_event_context,
            "has_price_reaction": has_price_reaction,
            "has_strong_price_reaction": has_strong_price_reaction,
            "has_gdelt_support": has_gdelt_support,
            "has_macro_background": has_macro_background,
            "directional_consistency": directional_consistency,
        }

    def _has_stock_event_trigger(self, bundle: Dict[str, Any]) -> bool:
        items = bundle.get("stock_event_evidence", [])
        if not items:
            return False

        # If prior stage explicitly marks trigger, respect it.
        trigger_fields = [
            "final_can_be_news_trigger",
            "can_be_news_trigger",
            "is_news_trigger",
            "is_trigger",
            "topic_trigger",
        ]
        saw_explicit = False
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in trigger_fields:
                if key in item:
                    saw_explicit = True
                    if parse_bool(item.get(key), default=False):
                        return True

        # In pr05d, stock_event_items usually already represent accepted trigger items.
        # If no explicit marker exists, treat presence as trigger but keep ceiling logic conservative.
        return not saw_explicit

    def _has_price_reaction(self, bundle: Dict[str, Any], raw: Dict[str, Any]) -> bool:
        explicit = first_present(raw, ["has_price_reaction", "price_reaction", "has_reaction"])
        if not is_nullish(explicit):
            return parse_bool(explicit, default=False)
        return bool(bundle.get("price_volume_evidence"))

    def _has_strong_price_reaction(self, bundle: Dict[str, Any], raw: Dict[str, Any], has_price_reaction: bool) -> bool:
        if not has_price_reaction:
            return False

        explicit = first_present(
            raw,
            [
                "has_strong_price_reaction",
                "strong_price_reaction",
                "is_strong_price_reaction",
                "strong_reaction",
            ],
        )
        if not is_nullish(explicit):
            return parse_bool(explicit, default=False)

        for item in bundle.get("price_volume_evidence", []):
            if not isinstance(item, dict):
                continue
            explicit_item = first_present(
                item,
                [
                    "has_strong_price_reaction",
                    "strong_price_reaction",
                    "is_strong_price_reaction",
                    "strong_reaction",
                ],
            )
            if not is_nullish(explicit_item) and parse_bool(explicit_item, default=False):
                return True

            # Defensive numeric heuristics, used only if prior stage did not set a flag.
            # These thresholds are intentionally conservative and should be adjusted after distribution checks.
            for key in ["abs_return", "abs_ret", "return_abs", "event_window_abs_return", "price_abs_change_pct"]:
                value = item.get(key)
                if not is_nullish(value):
                    try:
                        number = abs(float(str(value).replace("%", "")))
                        if number >= 5.0:
                            return True
                    except Exception:
                        pass

            for key in ["volume_z", "volume_zscore", "volume_spike_z", "turnover_z"]:
                value = item.get(key)
                if not is_nullish(value):
                    try:
                        if abs(float(value)) >= 2.0:
                            return True
                    except Exception:
                        pass

        return False

    def _find_directional_consistency(self, bundle: Dict[str, Any], raw: Dict[str, Any]) -> Any:
        value = first_present(raw, ["directional_consistency", "direction_consistency", "consistency"])
        if not is_nullish(value):
            return value
        for source in SOURCE_NAMES:
            for item in bundle.get(f"{source}_evidence", []):
                if isinstance(item, dict):
                    value = first_present(item, ["directional_consistency", "direction_consistency", "consistency"])
                    if not is_nullish(value):
                        return value
        return None


class BundleRanker:
    def rank(self, bundle: Dict[str, Any], precheck: Dict[str, Any]) -> Tuple[int, str]:
        family = str(bundle.get("event_family") or "").lower()

        has_dart = precheck["has_dart"]
        has_stock_signal = precheck["has_stock_event_trigger"] or precheck["has_stock_event_context"]

        if has_dart and has_stock_signal:
            return 1, "multi_source_official_and_stock_event"

        if has_dart and family in STRONG_EVENT_FAMILIES:
            return 2, "dart_strong_event_family"

        if precheck["has_stock_event_trigger"]:
            return 3, "stock_event_trigger_only"

        if family == "dividend":
            return 4, "dividend_routine_but_usable"

        if family == "treasury_stock":
            return 5, "treasury_stock_repetitive_event"

        return 6, "weak_or_unclear_event_family"


class CorroborationEvaluator:
    def evaluate(self, bundle: Dict[str, Any], precheck: Dict[str, Any]) -> str:
        active_sources = []
        if precheck["has_dart"]:
            active_sources.append("dart")
        if precheck["has_stock_event_trigger"]:
            active_sources.append("stock_event")
        if precheck["has_stock_event_context"]:
            active_sources.append("stock_event_context")
        if precheck["has_price_reaction"]:
            active_sources.append("price_volume")
        if precheck["has_gdelt_support"]:
            active_sources.append("gdelt")
        if precheck["has_macro_background"]:
            active_sources.append("macro")

        if not active_sources:
            return "none"
        if len(active_sources) == 1:
            return "single_source"
        if precheck["has_dart"] and (precheck["has_stock_event_trigger"] or precheck["has_stock_event_context"]):
            return "multi_source_strong_topic"
        return "multi_source_weak"


# =============================================================================
# Source caps + bounded combination rule
# =============================================================================


class SourceCapEvaluator:
    """Compute solo source caps.

    Rank is intentionally not used here.
    """

    def evaluate(self, bundle: Dict[str, Any], precheck: Dict[str, Any]) -> Dict[str, str]:
        family = str(bundle.get("event_family") or "").lower()
        is_strong_family = family in STRONG_EVENT_FAMILIES
        is_routine_family = family in ROUTINE_EVENT_FAMILIES
        has_price = precheck["has_price_reaction"]
        has_strong_price = precheck["has_strong_price_reaction"]
        consistency = precheck["directional_consistency"]

        caps = {
            "dart": ClaimLevel.INSUFFICIENT_EVIDENCE.value,
            "stock_event": ClaimLevel.INSUFFICIENT_EVIDENCE.value,
            "stock_event_context": ClaimLevel.INSUFFICIENT_EVIDENCE.value,
            "price_volume": ClaimLevel.INSUFFICIENT_EVIDENCE.value,
            "gdelt": ClaimLevel.INSUFFICIENT_EVIDENCE.value,
            "macro": ClaimLevel.INSUFFICIENT_EVIDENCE.value,
        }

        if precheck["has_dart"]:
            caps["dart"] = self._dart_cap(is_strong_family, is_routine_family, has_price, has_strong_price, consistency)

        if precheck["has_stock_event_trigger"]:
            caps["stock_event"] = self._stock_event_cap(is_strong_family, has_price)

        if precheck["has_stock_event_context"]:
            caps["stock_event_context"] = ClaimLevel.NO_MARKET_CLAIM.value

        if has_price:
            caps["price_volume"] = ClaimLevel.REACTION_ONLY.value

        if precheck["has_gdelt_support"]:
            # GDELT supports background/topic corroboration here, not stock-specific causality.
            caps["gdelt"] = ClaimLevel.NO_MARKET_CLAIM.value

        if precheck["has_macro_background"]:
            # Macro is exposure/background context, not stock-specific causality.
            caps["macro"] = ClaimLevel.NO_MARKET_CLAIM.value

        return caps

    def _dart_cap(
        self,
        is_strong_family: bool,
        is_routine_family: bool,
        has_price: bool,
        has_strong_price: bool,
        consistency: str,
    ) -> str:
        if not has_price:
            # Strict v2 policy: without price-volume evidence, no source may create
            # a market-context or reaction-level claim. Strong DART families remain
            # usable as official topic evidence, but the market-claim ceiling stays
            # at no_market_claim.
            return ClaimLevel.NO_MARKET_CLAIM.value

        if consistency == "inconsistent":
            return ClaimLevel.REACTION_ONLY.value

        if is_strong_family and has_strong_price and consistency == "consistent":
            return ClaimLevel.LIKELY_CONTRIBUTOR.value

        # Price exists but source alone should not assert cause.
        if is_strong_family or is_routine_family:
            return ClaimLevel.REACTION_ONLY.value

        return ClaimLevel.REACTION_ONLY.value

    def _stock_event_cap(self, is_strong_family: bool, has_price: bool) -> str:
        if has_price:
            return ClaimLevel.REACTION_ONLY.value
        return ClaimLevel.NO_MARKET_CLAIM.value


class ClaimCeilingEvaluator:
    """Apply bounded combination rules to source caps.

    The combination rule is deliberately bounded:
    - Background-only sources can never combine into a causal claim.
    - Directional inconsistency is a downward gate.
    - Top-level attribution requires official evidence, strong price reaction, consistency,
      and additional topic/news support.
    """

    def evaluate(
        self,
        bundle: Dict[str, Any],
        precheck: Dict[str, Any],
        source_caps: Dict[str, str],
    ) -> Tuple[str, Dict[str, Any]]:
        family = str(bundle.get("event_family") or "").lower()
        is_strong_family = family in STRONG_EVENT_FAMILIES
        consistency = precheck["directional_consistency"]

        strongest_source, strongest_cap = self._strongest_source_cap(source_caps)
        result = {
            "strongest_causal_eligible_source": strongest_source,
            "strongest_source_cap": strongest_cap,
            "can_lift_by_corroboration": False,
            "lift_reason": None,
            "downward_gate_applied": False,
            "downward_gate_reason": None,
        }

        if strongest_cap == ClaimLevel.INSUFFICIENT_EVIDENCE.value:
            return ClaimLevel.INSUFFICIENT_EVIDENCE.value, result

        # Directional inconsistency must actively lower the ceiling.
        if precheck["has_price_reaction"] and consistency == "inconsistent":
            result["downward_gate_applied"] = True
            result["downward_gate_reason"] = "directional_inconsistency_caps_at_reaction_only"
            return cap_claim(strongest_cap, ClaimLevel.REACTION_ONLY.value), result

        ceiling = strongest_cap

        # Unknown / not evaluated direction with price reaction should not exceed reaction-only,
        # except where there is no price reaction and the claim is only contextual.
        if precheck["has_price_reaction"] and consistency in {"unknown", "not_evaluated", "not_applicable"}:
            ceiling = cap_claim(ceiling, ClaimLevel.REACTION_ONLY.value)
            result["downward_gate_applied"] = True
            result["downward_gate_reason"] = "directional_consistency_not_verified_caps_at_reaction_only"

        # Bounded lift: only official evidence + strong price + consistent direction can reach likely_contributor.
        if (
            precheck["has_official_evidence"]
            and precheck["has_strong_price_reaction"]
            and consistency == "consistent"
            and is_strong_family
        ):
            ceiling = max_claim(ceiling, ClaimLevel.LIKELY_CONTRIBUTOR.value)
            result["can_lift_by_corroboration"] = True
            result["lift_reason"] = "official_strong_family_with_strong_consistent_price_reaction"

            # Rare top level: strongest attributable disclosed factor.
            # This is not a single-cause claim. It is a disclosed-factor attribution ceiling.
            if (
                (precheck["has_stock_event_trigger"] or precheck["has_stock_event_context"])
                and precheck["has_gdelt_support"]
            ):
                ceiling = max_claim(ceiling, ClaimLevel.STRONGEST_ATTRIBUTABLE_DISCLOSED_FACTOR.value)
                result["lift_reason"] = "official_stock_event_gdelt_strong_consistent_reaction"

        # Strict v2 policy: if there is no price reaction evidence, no claim should
        # exceed no_market_claim. This avoids causal implicature from official events
        # being narrated as market-relevant context without reaction evidence.
        if not precheck["has_price_reaction"]:
            ceiling = cap_claim(ceiling, ClaimLevel.NO_MARKET_CLAIM.value)

        # Routine/weak families should not exceed reaction-only even with price reaction.
        if family in ROUTINE_EVENT_FAMILIES or family in WEAK_EVENT_FAMILIES:
            if precheck["has_price_reaction"]:
                ceiling = cap_claim(ceiling, ClaimLevel.REACTION_ONLY.value)
            else:
                ceiling = cap_claim(ceiling, ClaimLevel.NO_MARKET_CLAIM.value)

        return ceiling, result

    def _strongest_source_cap(self, source_caps: Dict[str, str]) -> Tuple[str, str]:
        # Background-only sources are included but cannot be the basis of causal lift.
        best_source = "none"
        best_level = ClaimLevel.INSUFFICIENT_EVIDENCE.value
        for source, level in source_caps.items():
            normalized = normalize_claim_level(level)
            if claim_rank(normalized) > claim_rank(best_level):
                best_source = source
                best_level = normalized
        return best_source, best_level


# =============================================================================
# Claim policy / generation policy
# =============================================================================


class ClaimPolicyBuilder:
    """Build claim permissions and wording constraints.

    These fields are passed downstream to pr06, so the Korean guidance avoids
    report-like meta phrases such as "이벤트", "맥락", "의미", "해석",
    and "주목". The internal enum still controls permission; the output-facing
    guidance should read like plain financial news copy.
    """

    BLOCKED_OUTPUT_PHRASES = [
        "이벤트",
        "맥락",
        "관점",
        "의미",
        "해석",
        "시사",
        "주목",
        "관심이 필요",
        "확인할 필요",
        "시장 참여자",
        "투자자들은",
        "향후 흐름",
        "영향을 미칠 수",
        "가능성이 있다",
        "분류된다",
        "관련된 사안",
    ]

    CAUSAL_BLOCKED_PHRASES = [
        "때문에",
        "영향으로",
        "이에 따라",
        "그 결과",
        "호재로 받아들",
        "악재로 받아들",
        "투자심리",
        "매수세",
        "매도세",
        "주가 상승의 원인",
        "주가 하락의 원인",
    ]

    def build_allowed_forbidden(self, claim_level: str) -> Tuple[List[str], List[str]]:
        level = normalize_claim_level(claim_level)

        if level == ClaimLevel.INSUFFICIENT_EVIDENCE.value:
            return (
                [],
                [
                    "뉴스 본문 생성에 사용하지 말 것",
                    "공시 사실, 가격 반응, 주가 영향을 단정하지 말 것",
                    "다른 자료와 엮어 원인 서사를 만들지 말 것",
                ],
            )

        if level == ClaimLevel.NO_MARKET_CLAIM.value:
            return (
                [
                    "회사가 결정·체결·취득·처분·지급한 구체적 행위를 설명할 수 있음",
                    "자금 조달, 계약, 배당, 취득·처분 등 회사 행위의 대상·금액·기간을 설명할 수 있음",
                    "가격·거래량 문장 없이 회사 행위 중심으로 작성할 수 있음",
                ],
                [
                    "주가 상승 또는 하락의 원인이라고 말할 수 없음",
                    "시장 반응이 확대됐다고 말할 수 없음",
                    "호재 또는 악재로 받아들였다고 단정할 수 없음",
                    "가격·거래량 변화와 공시 문장을 붙여 원인처럼 읽히게 만들 수 없음",
                    "본문에 '이벤트', '맥락', '의미', '해석', '주목' 같은 분석 보고서식 표현을 쓰지 말 것",
                    "'공시했다', '밝혔다'를 반복적으로 쓰지 말 것 (구체적 행위 동사 사용)",
                ],
            )

        if level == ClaimLevel.REACTION_ONLY.value:
            return (
                [
                    "해당 시점 전후 가격 또는 거래량 변화가 있었다고 말할 수 있음",
                    "회사 공시와 가격·거래량 변화를 별도 관찰 사실로 분리해 제시할 수 있음",
                ],
                [
                    "공시 때문에 주가가 움직였다고 단정할 수 없음",
                    "원인-결과 문장 사용 금지",
                    "같은 날, 직후, 이에 따라 등 시간적 연결어로 원인 암시 금지",
                    "가격 반응과 공시 문장을 바로 붙여 causal implicature를 만들 수 없음",
                    "본문에 '이벤트', '맥락', '의미', '해석', '주목' 같은 분석 보고서식 표현을 쓰지 말 것",
                ],
            )

        if level == ClaimLevel.PLAUSIBLE_MARKET_CONTEXT.value:
            return (
                [
                    "회사의 결정 내용과 사업·재무상 변화를 구체적 사실 위주로 설명할 수 있음",
                    "가격 원인 단정 없이 공시 항목의 금액·대상·기간·조건을 설명할 수 있음",
                    "불확실한 부분은 단정하지 않고 공시된 범위만 쓸 수 있음",
                ],
                [
                    "직접적인 주가 원인으로 단정할 수 없음",
                    "주요 상승 또는 하락 요인이라고 말할 수 없음",
                    "시장 전체가 특정 방향으로 받아들였다고 단정할 수 없음",
                    "본문에 '이벤트', '맥락', '의미', '해석', '주목' 같은 분석 보고서식 표현을 쓰지 말 것",
                ],
            )

        if level == ClaimLevel.LIKELY_CONTRIBUTOR.value:
            return (
                [
                    "공식 근거와 가격 반응의 방향이 맞는 경우, 여러 요인 중 하나로 제한해 표현할 수 있음",
                    "다른 요인을 배제하지 않는 조건부 표현을 사용할 수 있음",
                    "공시된 사실과 가격·거래량 변화를 근거 ID에 맞춰 설명할 수 있음",
                ],
                [
                    "유일한 원인이라고 단정할 수 없음",
                    "주요 원인이라고 확정할 수 없음",
                    "근거 없이 투자자 심리를 구체적으로 단정할 수 없음",
                    "본문에 '이벤트', '맥락', '의미', '해석', '주목' 같은 분석 보고서식 표현을 쓰지 말 것",
                ],
            )

        return (
            [
                "공개된 근거 중 가장 강하게 연결되는 공시 항목으로 제한해 표현할 수 있음",
                "공식 근거, 보조 근거, 강한 가격 반응, 방향성 일치가 모두 확인된 범위에서 설명할 수 있음",
                "다른 요인 가능성을 배제하지 않는 조건부 표현을 사용할 수 있음",
            ],
            [
                "실제 시장의 유일한 원인이라고 단정할 수 없음",
                "모든 투자자의 판단을 대표한다고 말할 수 없음",
                "비공개 정보나 확인되지 않은 수급 원인을 추정할 수 없음",
                "본문에 '이벤트', '맥락', '의미', '해석', '주목' 같은 분석 보고서식 표현을 쓰지 말 것",
            ],
        )

    def build_generation_policy(self, claim_level: str) -> Dict[str, Any]:
        level = normalize_claim_level(claim_level)
        rank = claim_rank(level)

        policy = {
            "causal_implicature_allowed": False,
            "event_reaction_adjacency_allowed": False,
            "temporal_linking_allowed": False,
            "direct_causal_language_allowed": False,
            "requires_separate_sentence_block": True,
            "requires_uncertainty_language": True,
            "blocked_output_phrases": self.BLOCKED_OUTPUT_PHRASES + self.CAUSAL_BLOCKED_PHRASES,
            "preferred_output_style": [
                "회사명 + 구체적 동사 + 대상/금액/기간 중심으로 작성",
                "분류·해석·의미 부여보다 공시된 행위를 짧게 설명",
                "보고서식 표현보다 일반 경제 기사 문장 사용",
            ],
            "notes": [],
        }

        if rank <= claim_rank(ClaimLevel.REACTION_ONLY.value):
            policy["notes"].append(
                "At reaction_only or below, do not place disclosure facts and price/volume reaction in adjacent causally suggestive sentences."
            )
            policy["notes"].append(
                "Avoid Korean meta-analytical phrasing such as 이벤트, 맥락, 의미, 해석, 주목, 확인할 필요."
            )
            return policy

        if level == ClaimLevel.PLAUSIBLE_MARKET_CONTEXT.value:
            policy["event_reaction_adjacency_allowed"] = False
            policy["temporal_linking_allowed"] = False
            policy["requires_separate_sentence_block"] = True
            policy["notes"].append(
                "Disclosed corporate action may be described, but direct or implied causality remains forbidden."
            )
            return policy

        if level == ClaimLevel.LIKELY_CONTRIBUTOR.value:
            policy["event_reaction_adjacency_allowed"] = True
            policy["temporal_linking_allowed"] = True
            policy["direct_causal_language_allowed"] = False
            policy["requires_separate_sentence_block"] = False
            policy["notes"].append(
                "May describe the disclosure as one contributing factor, not as the sole or confirmed driver."
            )
            return policy

        policy["causal_implicature_allowed"] = True
        policy["event_reaction_adjacency_allowed"] = True
        policy["temporal_linking_allowed"] = True
        policy["direct_causal_language_allowed"] = False
        policy["requires_separate_sentence_block"] = False
        policy["notes"].append(
            "May describe this as the strongest attributable disclosed factor, while avoiding sole-cause language."
        )
        return policy


# =============================================================================
# Action primitive resolution (anti-AI-tone core)
# =============================================================================


class ActionPrimitiveResolver:
    """Resolve a concrete corporate-action primitive set from topic/evidence.

    Returns a dict with:
        action_type           machine label for the concrete action
        plain_action_ko       short Korean noun for the action (no full sentence)
        object_ko             optional Korean object noun
        allowed_verbs_ko      verbs the writer may use
        avoid_verbs_ko        verbs the writer should avoid (공시했다 등)
        usable_fact_slots     fact fields the writer should fill if available
        writing_quality_risk  set when the action is too generic to render well

    Crucially this never returns a full sentence. The generator builds the
    sentence; pr05e only supplies primitives. This is what removes the
    disclosure-restatement / template-paraphrase tone.
    """

    # Verbs that read like "reading the disclosure aloud". Globally discouraged.
    GLOBAL_AVOID_VERBS = ["공시했다", "밝혔다", "전했다"]

    # Ordered (most specific first). Each rule maps keyword substrings (space-removed)
    # to a concrete primitive spec.
    RULES: List[Tuple[List[str], Dict[str, Any]]] = [
        # ----- treasury_stock subtypes (신탁계약 must NOT be a supply contract) -----
        (["자기주식취득신탁계약체결결정", "자기주식취득신탁계약"], {
            "action_type": "treasury_trust_contract",
            "plain_action_ko": "자기주식 취득 신탁계약",
            "object_ko": "신탁계약",
            "allowed_verbs_ko": ["체결하기로 했다", "체결했다", "결정했다"],
            "usable_fact_slots": ["trust_amount", "contract_party", "contract_period", "purpose"],
        }),
        (["자기주식취득결과보고서", "자기주식취득결과"], {
            "action_type": "treasury_acquire_result",
            "plain_action_ko": "자기주식 취득 결과",
            "object_ko": "취득 결과",
            "allowed_verbs_ko": ["보고했다", "제출했다"],
            "usable_fact_slots": ["share_count", "amount", "method", "period"],
        }),
        (["자기주식처분결과보고서", "자기주식처분결과"], {
            "action_type": "treasury_dispose_result",
            "plain_action_ko": "자기주식 처분 결과",
            "object_ko": "처분 결과",
            "allowed_verbs_ko": ["보고했다", "제출했다"],
            "usable_fact_slots": ["share_count", "amount", "method", "period"],
        }),
        (["자기주식취득결정", "자기주식취득"], {
            "action_type": "treasury_acquire",
            "plain_action_ko": "자기주식 취득",
            "object_ko": "자기주식",
            "allowed_verbs_ko": ["취득하기로 했다", "취득한다", "매수한다", "결정했다"],
            "usable_fact_slots": ["share_count", "amount", "method", "period", "purpose"],
        }),
        (["자기주식처분결정", "자기주식처분"], {
            "action_type": "treasury_dispose",
            "plain_action_ko": "자기주식 처분",
            "object_ko": "자기주식",
            "allowed_verbs_ko": ["처분하기로 했다", "처분한다", "매각한다", "결정했다"],
            "usable_fact_slots": ["share_count", "amount", "method", "period", "purpose"],
        }),
        (["자기주식"], {
            "action_type": "treasury_generic",
            "plain_action_ko": "자기주식 관련 결정",
            "object_ko": "자기주식",
            "allowed_verbs_ko": ["결정했다", "진행한다"],
            "usable_fact_slots": ["share_count", "amount", "method", "period", "purpose"],
        }),
        # ----- dividend -----
        (["현금ㆍ현물배당결정", "현금·현물배당결정", "현금배당결정", "현금배당"], {
            "action_type": "cash_dividend",
            "plain_action_ko": "현금배당",
            "object_ko": "현금배당",
            "allowed_verbs_ko": ["결정했다", "확정했다", "지급하기로 했다"],
            "usable_fact_slots": ["dividend_per_share", "record_date", "payment_date", "total_amount"],
        }),
        (["현물배당결정", "현물배당"], {
            "action_type": "stock_dividend",
            "plain_action_ko": "현물배당",
            "object_ko": "현물배당",
            "allowed_verbs_ko": ["결정했다", "확정했다"],
            "usable_fact_slots": ["record_date", "payment_date", "total_amount"],
        }),
        (["결산배당", "배당결정", "배당"], {
            "action_type": "dividend",
            "plain_action_ko": "배당",
            "object_ko": "배당금",
            "allowed_verbs_ko": ["결정했다", "확정했다", "지급하기로 했다"],
            "usable_fact_slots": ["dividend_per_share", "record_date", "payment_date", "total_amount"],
        }),
        # ----- capital_financing -----
        (["전환사채권발행", "전환사채"], {
            "action_type": "convertible_bond",
            "plain_action_ko": "전환사채 발행",
            "object_ko": "전환사채",
            "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
            "usable_fact_slots": ["amount", "funding_purpose", "interest_rate", "maturity_date", "conversion_price"],
        }),
        (["교환사채권발행", "교환사채"], {
            "action_type": "exchangeable_bond",
            "plain_action_ko": "교환사채 발행",
            "object_ko": "교환사채",
            "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
            "usable_fact_slots": ["amount", "funding_purpose", "maturity_date", "exchange_price"],
        }),
        (["신주인수권부사채"], {
            "action_type": "bond_with_warrant",
            "plain_action_ko": "신주인수권부사채 발행",
            "object_ko": "신주인수권부사채",
            "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
            "usable_fact_slots": ["amount", "funding_purpose", "maturity_date", "exercise_price"],
        }),
        (["유상증자"], {
            "action_type": "rights_issue",
            "plain_action_ko": "유상증자",
            "object_ko": "신주",
            "allowed_verbs_ko": ["결정했다", "진행한다", "추진한다"],
            "usable_fact_slots": ["amount", "funding_purpose", "share_count", "issue_price", "payment_date"],
        }),
        (["무상증자"], {
            "action_type": "bonus_issue",
            "plain_action_ko": "무상증자",
            "object_ko": "신주",
            "allowed_verbs_ko": ["결정했다", "진행한다"],
            "usable_fact_slots": ["share_count", "ratio", "record_date", "listing_date"],
        }),
        (["감자결정", "감자"], {
            "action_type": "capital_reduction",
            "plain_action_ko": "감자",
            "object_ko": "자본금",
            "allowed_verbs_ko": ["결정했다", "진행한다"],
            "usable_fact_slots": ["reduction_ratio", "method", "record_date", "purpose"],
        }),
        (["사채권발행", "회사채"], {
            "action_type": "bond_issue",
            "plain_action_ko": "회사채 발행",
            "object_ko": "회사채",
            "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
            "usable_fact_slots": ["amount", "funding_purpose", "interest_rate", "maturity_date"],
        }),
        # ----- equity_investment -----
        (["타법인주식및출자증권취득"], {
            "action_type": "equity_acquire",
            "plain_action_ko": "타법인 주식 취득",
            "object_ko": "타법인 주식",
            "allowed_verbs_ko": ["취득하기로 했다", "취득한다", "결정했다"],
            "usable_fact_slots": ["target_company", "acquisition_amount", "stake_ratio", "purpose", "transaction_date"],
        }),
        (["타법인주식및출자증권처분"], {
            "action_type": "equity_dispose",
            "plain_action_ko": "타법인 주식 처분",
            "object_ko": "타법인 주식",
            "allowed_verbs_ko": ["처분하기로 했다", "처분한다", "결정했다"],
            "usable_fact_slots": ["target_company", "disposal_amount", "stake_ratio", "purpose", "transaction_date"],
        }),
        (["출자증권취득", "출자증권"], {
            "action_type": "equity_acquire",
            "plain_action_ko": "출자증권 취득",
            "object_ko": "출자증권",
            "allowed_verbs_ko": ["취득하기로 했다", "취득한다", "결정했다"],
            "usable_fact_slots": ["target_company", "acquisition_amount", "stake_ratio", "purpose"],
        }),
        (["타법인주식"], {
            "action_type": "equity_transaction",
            "plain_action_ko": "타법인 주식 거래",
            "object_ko": "타법인 주식",
            "allowed_verbs_ko": ["취득하기로 했다", "처분하기로 했다", "결정했다"],
            "usable_fact_slots": ["target_company", "acquisition_amount", "stake_ratio", "purpose"],
        }),
        # ----- investment (시설/설비) -----
        (["신규시설투자"], {
            "action_type": "new_facility_investment",
            "plain_action_ko": "신규 시설투자",
            "object_ko": "생산설비",
            "allowed_verbs_ko": ["결정했다", "진행한다", "투자한다"],
            "usable_fact_slots": ["amount", "facility", "region", "period", "purpose"],
        }),
        (["시설투자", "설비투자", "투자등"], {
            "action_type": "facility_investment",
            "plain_action_ko": "설비 투자",
            "object_ko": "생산설비",
            "allowed_verbs_ko": ["결정했다", "진행한다", "투자한다"],
            "usable_fact_slots": ["amount", "facility", "region", "period", "purpose"],
        }),
        # ----- contract -----
        (["단일판매", "공급계약", "판매계약", "수주"], {
            "action_type": "supply_contract",
            "plain_action_ko": "공급계약",
            "object_ko": "공급계약",
            "allowed_verbs_ko": ["체결했다", "맺었다", "수주했다"],
            "usable_fact_slots": ["counterparty", "contract_amount", "contract_period", "product_or_service", "sales_ratio"],
        }),
        # ----- asset_transaction -----
        (["유형자산취득"], {
            "action_type": "asset_acquire",
            "plain_action_ko": "유형자산 취득",
            "object_ko": "유형자산",
            "allowed_verbs_ko": ["취득하기로 했다", "매입한다", "결정했다"],
            "usable_fact_slots": ["asset_type", "amount", "location", "purpose", "transaction_date"],
        }),
        (["유형자산처분", "유형자산양도"], {
            "action_type": "asset_dispose",
            "plain_action_ko": "유형자산 처분",
            "object_ko": "유형자산",
            "allowed_verbs_ko": ["처분하기로 했다", "매각한다", "결정했다"],
            "usable_fact_slots": ["asset_type", "amount", "location", "purpose", "transaction_date"],
        }),
        # ----- business_transfer -----
        (["영업양수"], {
            "action_type": "business_acquire",
            "plain_action_ko": "영업 양수",
            "object_ko": "영업",
            "allowed_verbs_ko": ["양수한다", "양수하기로 했다", "결정했다"],
            "usable_fact_slots": ["business_unit", "counterparty", "amount", "effective_date"],
        }),
        (["영업양도"], {
            "action_type": "business_transfer",
            "plain_action_ko": "영업 양도",
            "object_ko": "영업",
            "allowed_verbs_ko": ["양도한다", "양도하기로 했다", "결정했다"],
            "usable_fact_slots": ["business_unit", "counterparty", "amount", "effective_date"],
        }),
        (["회사합병", "합병결정", "분할합병"], {
            "action_type": "merger",
            "plain_action_ko": "합병",
            "object_ko": "합병",
            "allowed_verbs_ko": ["결정했다", "진행한다", "추진한다"],
            "usable_fact_slots": ["counterparty", "method", "merger_ratio", "effective_date"],
        }),
        (["분할결정", "분할"], {
            "action_type": "spin_off",
            "plain_action_ko": "회사 분할",
            "object_ko": "분할",
            "allowed_verbs_ko": ["결정했다", "진행한다"],
            "usable_fact_slots": ["business_unit", "method", "split_ratio", "effective_date"],
        }),
        (["주식의포괄적교환", "주식의포괄적이전"], {
            "action_type": "share_swap",
            "plain_action_ko": "주식의 포괄적 교환·이전",
            "object_ko": "주식 교환",
            "allowed_verbs_ko": ["결정했다", "진행한다"],
            "usable_fact_slots": ["counterparty", "swap_ratio", "effective_date"],
        }),
        # ----- legal_regulatory -----
        (["경영권분쟁소송"], {
            "action_type": "control_dispute_litigation",
            "plain_action_ko": "경영권 분쟁 관련 소송",
            "object_ko": "소송",
            "allowed_verbs_ko": ["제기됐다", "접수됐다", "진행 중이다"],
            "usable_fact_slots": ["case_name", "plaintiff", "defendant", "court", "claim_amount"],
        }),
        (["소송등의제기", "소송등의신청"], {
            "action_type": "litigation_filed",
            "plain_action_ko": "소송 제기",
            "object_ko": "소송",
            "allowed_verbs_ko": ["제기했다", "제기됐다", "신청했다"],
            "usable_fact_slots": ["case_name", "plaintiff", "defendant", "court", "claim_amount"],
        }),
        (["소송등의판결", "소송등의결정"], {
            "action_type": "litigation_ruling",
            "plain_action_ko": "소송 판결·결정",
            "object_ko": "판결",
            "allowed_verbs_ko": ["받았다", "통보받았다", "확정됐다"],
            "usable_fact_slots": ["case_name", "court", "ruling_result", "claim_amount", "ruling_date"],
        }),
        (["과징금", "벌금", "제재"], {
            "action_type": "sanction",
            "plain_action_ko": "제재 또는 과징금",
            "object_ko": "제재",
            "allowed_verbs_ko": ["통보받았다", "부과받았다", "받았다"],
            "usable_fact_slots": ["authority", "action", "amount", "reason", "deadline"],
        }),
        (["소송"], {
            "action_type": "litigation",
            "plain_action_ko": "소송",
            "object_ko": "소송",
            "allowed_verbs_ko": ["제기됐다", "진행 중이다", "받았다"],
            "usable_fact_slots": ["case_name", "court", "claim_amount"],
        }),
        # ----- trading_status -----
        (["매매거래정지", "거래정지"], {
            "action_type": "trading_suspension",
            "plain_action_ko": "매매거래 정지",
            "object_ko": "매매거래",
            "allowed_verbs_ko": ["정지됐다", "정지된다"],
            "usable_fact_slots": ["exchange", "suspension_date", "reason"],
            "actor_override": "exchange",
        }),
        (["정지해제", "거래재개"], {
            "action_type": "trading_resumption",
            "plain_action_ko": "매매거래 재개",
            "object_ko": "매매거래",
            "allowed_verbs_ko": ["재개된다", "재개됐다"],
            "usable_fact_slots": ["exchange", "resumption_date"],
            "actor_override": "exchange",
        }),
        # ----- listing_risk -----
        (["상장폐지"], {
            "action_type": "delisting_review",
            "plain_action_ko": "상장폐지 관련 절차",
            "object_ko": "상장폐지",
            "allowed_verbs_ko": ["안내됐다", "심사 중이다", "통보됐다"],
            "usable_fact_slots": ["exchange", "reason", "review_date", "deadline"],
            "actor_override": "exchange",
        }),
        (["관리종목"], {
            "action_type": "management_issue_designation",
            "plain_action_ko": "관리종목 지정 관련 안내",
            "object_ko": "관리종목 지정",
            "allowed_verbs_ko": ["지정됐다", "안내됐다", "통보됐다"],
            "usable_fact_slots": ["exchange", "reason", "deadline"],
            "actor_override": "exchange",
        }),
        (["상장적격성", "투자유의안내"], {
            "action_type": "listing_eligibility_review",
            "plain_action_ko": "상장 적격성 관련 안내",
            "object_ko": "상장 적격성",
            "allowed_verbs_ko": ["안내됐다", "심사 중이다", "통보됐다"],
            "usable_fact_slots": ["exchange", "reason", "review_date"],
            "actor_override": "exchange",
        }),
        # ----- management_governance -----
        (["대표이사변경", "대표이사"], {
            "action_type": "ceo_change",
            "plain_action_ko": "대표이사 변경",
            "object_ko": "대표이사",
            "allowed_verbs_ko": ["변경했다", "선임했다", "교체했다"],
            "usable_fact_slots": ["person", "role", "change_type", "effective_date"],
        }),
        (["최대주주"], {
            "action_type": "largest_shareholder_change",
            "plain_action_ko": "최대주주 변경",
            "object_ko": "최대주주",
            "allowed_verbs_ko": ["변경됐다", "바뀌었다"],
            "usable_fact_slots": ["new_shareholder", "previous_shareholder", "stake_ratio", "effective_date"],
        }),
        (["임원"], {
            "action_type": "executive_change",
            "plain_action_ko": "임원 변동",
            "object_ko": "임원",
            "allowed_verbs_ko": ["선임했다", "사임했다", "변경했다"],
            "usable_fact_slots": ["person", "role", "change_type", "effective_date"],
        }),
        # ----- earnings -----
        (["매출액또는손익구조"], {
            "action_type": "earnings_structure_change",
            "plain_action_ko": "매출액·손익구조 변동",
            "object_ko": "손익구조",
            "allowed_verbs_ko": ["변동했다", "집계됐다", "기록했다"],
            "usable_fact_slots": ["sales", "operating_profit", "net_income", "period", "yoy_change"],
        }),
        (["영업실적등에대한전망", "실적전망"], {
            "action_type": "earnings_forecast",
            "plain_action_ko": "실적 전망",
            "object_ko": "실적 전망",
            "allowed_verbs_ko": ["제시했다", "전망했다", "정정했다"],
            "usable_fact_slots": ["forecast_metric", "forecast_value", "period", "revision"],
        }),
        (["잠정실적", "영업실적", "영업이익", "당기순이익", "실적"], {
            "action_type": "earnings_result",
            "plain_action_ko": "분기 실적",
            "object_ko": "실적",
            "allowed_verbs_ko": ["기록했다", "집계됐다", "발표했다"],
            "usable_fact_slots": ["sales", "operating_profit", "net_income", "period", "yoy_change"],
        }),
    ]

    def resolve(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        family = str(bundle.get("event_family") or "").lower()
        topic = bundle.get("candidate_topic") or ""

        topic_text = KoreanTextHelper.clean_topic(topic).replace(" ", "")

        # Candidate-topic hard action override.
        # This must win over noisy evidence text in grouped bundles.
        spec = self._resolve_from_candidate_topic(topic_text)

        if spec is None:
            text = self._search_text(bundle, topic)
            for keywords, candidate in self.RULES:
                if any(k.replace(" ", "") in text for k in keywords):
                    spec = candidate
                    break

        if spec is None:
            spec = self._family_fallback(family)

        return self._finalize(bundle, family, spec)

    def _resolve_from_candidate_topic(self, topic_text: str) -> Optional[Dict[str, Any]]:
        """Resolve action primitives from candidate_topic only.

        This prevents neighboring grouped evidence from overriding the representative topic.
        Example:
        candidate_topic = 매출액또는손익구조...변경
        nearby evidence contains 현금ㆍ현물배당결정
        -> must resolve as financial_result_change, not cash_dividend.
        """
        if "매출액또는손익구조" in topic_text or "손익구조" in topic_text:
            return {
                "action_type": "financial_result_change",
                "plain_action_ko": "매출액·손익구조 변동",
                "object_ko": "손익구조",
                "allowed_verbs_ko": ["변동했다", "집계됐다", "보고했다"],
                "usable_fact_slots": ["sales", "operating_profit", "net_income", "period", "yoy_change"],
            }

        if "영업실적등에대한전망" in topic_text or "실적전망" in topic_text or "전망(공정공시)" in topic_text:
            return {
                "action_type": "earnings_forecast",
                "plain_action_ko": "실적 전망",
                "object_ko": "실적 전망",
                "allowed_verbs_ko": ["제시했다", "전망했다", "정정했다"],
                "usable_fact_slots": ["forecast_metric", "forecast_value", "period", "revision"],
            }

        if (
            "현금ㆍ현물배당" in topic_text
            or "현금·현물배당" in topic_text
            or "현금배당" in topic_text
            or "현물배당" in topic_text
            or "배당결정" in topic_text
            or "배당금" in topic_text
        ):
            return {
                "action_type": "cash_dividend",
                "plain_action_ko": "현금배당",
                "object_ko": "현금배당",
                "allowed_verbs_ko": ["결정했다", "확정했다", "지급하기로 했다"],
                "usable_fact_slots": ["dividend_per_share", "record_date", "payment_date", "total_amount"],
            }

        if "자기주식취득신탁계약" in topic_text or "자기주식취득신탁" in topic_text:
            return {
                "action_type": "treasury_trust_contract",
                "plain_action_ko": "자기주식 취득 신탁계약",
                "object_ko": "신탁계약",
                "allowed_verbs_ko": ["체결하기로 했다", "체결했다", "결정했다"],
                "usable_fact_slots": ["trust_amount", "contract_party", "contract_period", "purpose"],
            }

        if "자기주식취득결과보고서" in topic_text or "자기주식취득결과" in topic_text:
            return {
                "action_type": "treasury_acquire_result",
                "plain_action_ko": "자기주식 취득 결과",
                "object_ko": "취득 결과",
                "allowed_verbs_ko": ["보고했다", "제출했다"],
                "usable_fact_slots": ["share_count", "amount", "method", "period"],
            }

        if "자기주식처분결과보고서" in topic_text or "자기주식처분결과" in topic_text:
            return {
                "action_type": "treasury_dispose_result",
                "plain_action_ko": "자기주식 처분 결과",
                "object_ko": "처분 결과",
                "allowed_verbs_ko": ["보고했다", "제출했다"],
                "usable_fact_slots": ["share_count", "amount", "method", "period"],
            }

        if "자기주식취득" in topic_text:
            return {
                "action_type": "treasury_acquire",
                "plain_action_ko": "자기주식 취득",
                "object_ko": "자기주식",
                "allowed_verbs_ko": ["취득하기로 했다", "취득한다", "매수한다", "결정했다"],
                "usable_fact_slots": ["share_count", "amount", "method", "period", "purpose"],
            }

        if "자기주식처분" in topic_text:
            return {
                "action_type": "treasury_dispose",
                "plain_action_ko": "자기주식 처분",
                "object_ko": "자기주식",
                "allowed_verbs_ko": ["처분하기로 했다", "처분한다", "매각한다", "결정했다"],
                "usable_fact_slots": ["share_count", "amount", "method", "period", "purpose"],
            }

        if "전환사채" in topic_text:
            return {
                "action_type": "convertible_bond",
                "plain_action_ko": "전환사채 발행",
                "object_ko": "전환사채",
                "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
                "usable_fact_slots": ["amount", "funding_purpose", "interest_rate", "maturity_date", "conversion_price"],
            }

        if "교환사채" in topic_text:
            return {
                "action_type": "exchangeable_bond",
                "plain_action_ko": "교환사채 발행",
                "object_ko": "교환사채",
                "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
                "usable_fact_slots": ["amount", "funding_purpose", "maturity_date", "exchange_price"],
            }

        if "신주인수권부사채" in topic_text:
            return {
                "action_type": "bond_with_warrant",
                "plain_action_ko": "신주인수권부사채 발행",
                "object_ko": "신주인수권부사채",
                "allowed_verbs_ko": ["발행하기로 했다", "발행한다", "결정했다"],
                "usable_fact_slots": ["amount", "funding_purpose", "maturity_date", "exercise_price"],
            }

        if "유상증자" in topic_text:
            return {
                "action_type": "rights_issue",
                "plain_action_ko": "유상증자",
                "object_ko": "신주",
                "allowed_verbs_ko": ["결정했다", "진행한다", "추진한다"],
                "usable_fact_slots": ["amount", "funding_purpose", "share_count", "issue_price", "payment_date"],
            }

        if "무상증자" in topic_text:
            return {
                "action_type": "bonus_issue",
                "plain_action_ko": "무상증자",
                "object_ko": "신주",
                "allowed_verbs_ko": ["결정했다", "진행한다"],
                "usable_fact_slots": ["share_count", "ratio", "record_date", "listing_date"],
            }

        if "단일판매" in topic_text or "공급계약" in topic_text or "판매계약" in topic_text or "수주" in topic_text:
            return {
                "action_type": "supply_contract",
                "plain_action_ko": "공급계약",
                "object_ko": "공급계약",
                "allowed_verbs_ko": ["체결했다", "맺었다", "수주했다"],
                "usable_fact_slots": ["counterparty", "contract_amount", "contract_period", "product_or_service", "sales_ratio"],
            }

        return None
    def _search_text(self, bundle: Dict[str, Any], topic: str) -> str:
        parts = [str(topic or "")]
        for source in ["dart", "stock_event", "stock_event_context"]:
            for item in bundle.get(f"{source}_evidence", []):
                if isinstance(item, dict):
                    for key in ["report_nm", "source_report_name", "report_name",
                                "candidate_topic", "topic", "title", "headline",
                                "event_name", "event_title", "disclosure_title"]:
                        v = item.get(key)
                        if not is_nullish(v):
                            parts.append(str(v))
        return KoreanTextHelper.clean_topic(" ".join(parts)).replace(" ", "")

    def _family_fallback(self, family: str) -> Dict[str, Any]:
        fallbacks: Dict[str, Dict[str, Any]] = {
            "dividend": {
                "action_type": "dividend",
                "plain_action_ko": "배당",
                "object_ko": "배당금",
                "allowed_verbs_ko": ["결정했다", "확정했다", "지급하기로 했다"],
                "usable_fact_slots": ["dividend_per_share", "record_date", "payment_date", "total_amount"],
            },
            "treasury_stock": {
                "action_type": "treasury_generic",
                "plain_action_ko": "자기주식 관련 결정",
                "object_ko": "자기주식",
                "allowed_verbs_ko": ["결정했다", "진행한다"],
                "usable_fact_slots": ["share_count", "amount", "method", "period", "purpose"],
            },
            "capital_financing": {
                "action_type": "financing",
                "plain_action_ko": "자금 조달",
                "object_ko": "자금",
                "allowed_verbs_ko": ["결정했다", "추진한다", "마련하기로 했다"],
                "usable_fact_slots": ["amount", "funding_purpose", "method", "payment_date"],
            },
            "equity_investment": {
                "action_type": "equity_transaction",
                "plain_action_ko": "타법인 주식 거래",
                "object_ko": "타법인 주식",
                "allowed_verbs_ko": ["취득하기로 했다", "처분하기로 했다", "결정했다"],
                "usable_fact_slots": ["target_company", "acquisition_amount", "stake_ratio", "purpose"],
            },
            "investment": {
                "action_type": "facility_investment",
                "plain_action_ko": "설비 투자",
                "object_ko": "생산설비",
                "allowed_verbs_ko": ["결정했다", "진행한다", "투자한다"],
                "usable_fact_slots": ["amount", "facility", "region", "period", "purpose"],
            },
            "contract": {
                "action_type": "supply_contract",
                "plain_action_ko": "공급계약",
                "object_ko": "공급계약",
                "allowed_verbs_ko": ["체결했다", "맺었다", "수주했다"],
                "usable_fact_slots": ["counterparty", "contract_amount", "contract_period", "product_or_service"],
            },
            "asset_transaction": {
                "action_type": "asset_transaction",
                "plain_action_ko": "자산 취득·처분",
                "object_ko": "자산",
                "allowed_verbs_ko": ["취득하기로 했다", "처분하기로 했다", "결정했다"],
                "usable_fact_slots": ["asset_type", "amount", "location", "purpose"],
            },
            "business_transfer": {
                "action_type": "business_restructuring",
                "plain_action_ko": "사업 구조 변경",
                "object_ko": "사업",
                "allowed_verbs_ko": ["결정했다", "진행한다", "추진한다"],
                "usable_fact_slots": ["business_unit", "counterparty", "amount", "effective_date"],
            },
            "legal_regulatory": {
                "action_type": "legal_regulatory",
                "plain_action_ko": "소송·제재 관련 사항",
                "object_ko": "소송",
                "allowed_verbs_ko": ["받았다", "제기됐다", "통보받았다"],
                "usable_fact_slots": ["authority", "case_name", "action", "amount", "deadline"],
            },
            "trading_status": {
                "action_type": "trading_status",
                "plain_action_ko": "매매거래 관련 조치",
                "object_ko": "매매거래",
                "allowed_verbs_ko": ["정지됐다", "재개된다", "안내됐다"],
                "usable_fact_slots": ["exchange", "suspension_date", "resumption_date", "reason"],
                "actor_override": "exchange",
            },
            "listing_risk": {
                "action_type": "listing_risk",
                "plain_action_ko": "상장 관련 유의 사항",
                "object_ko": "상장 적격성",
                "allowed_verbs_ko": ["안내됐다", "지정됐다", "통보됐다"],
                "usable_fact_slots": ["exchange", "risk_type", "reason", "deadline"],
                "actor_override": "exchange",
            },
            "management_governance": {
                "action_type": "governance_change",
                "plain_action_ko": "경영진·지배구조 변경",
                "object_ko": "경영진",
                "allowed_verbs_ko": ["변경했다", "선임했다", "사임했다"],
                "usable_fact_slots": ["person", "role", "change_type", "effective_date"],
            },
            "earnings": {
                "action_type": "earnings_result",
                "plain_action_ko": "분기 실적",
                "object_ko": "실적",
                "allowed_verbs_ko": ["기록했다", "집계됐다", "발표했다"],
                "usable_fact_slots": ["sales", "operating_profit", "net_income", "period", "yoy_change"],
            },
            "guidance": {
                "action_type": "earnings_forecast",
                "plain_action_ko": "실적 전망",
                "object_ko": "실적 전망",
                "allowed_verbs_ko": ["제시했다", "전망했다", "정정했다"],
                "usable_fact_slots": ["forecast_metric", "forecast_value", "period", "revision"],
            },
        }
        if family in fallbacks:
            return dict(fallbacks[family])

        # major_management_matter and other generic families: flag as low quality
        # because no concrete action can be derived from a generic title.
        if family == "major_management_matter":
            return {
                "action_type": "generic_management_matter",
                "plain_action_ko": "",
                "object_ko": "",
                "allowed_verbs_ko": ["결정했다", "진행한다"],
                "usable_fact_slots": ["decision_subject", "amount", "counterparty", "date", "purpose"],
                "writing_quality_risk": "generic_disclosure_title",
            }

        return {
            "action_type": "unspecified_disclosure",
            "plain_action_ko": "",
            "object_ko": "",
            "allowed_verbs_ko": ["결정했다", "진행한다"],
            "usable_fact_slots": ["decision_subject", "amount", "counterparty", "date", "purpose"],
            "writing_quality_risk": "generic_disclosure_title",
        }

    def _finalize(self, bundle: Dict[str, Any], family: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        spec = dict(spec)
        company = bundle.get("stock_name") or bundle.get("stock_code") or "회사"

        actor_override = spec.pop("actor_override", None)
        if actor_override == "exchange":
            corporate_actor = "거래소"
            actor_topic_particle = "는"
            subject_of_action = company  # the stock the exchange action concerns
        else:
            corporate_actor = company
            actor_topic_particle = KoreanTextHelper.topic_particle(company)
            subject_of_action = ""

        avoid_verbs = list(self.GLOBAL_AVOID_VERBS)
        # If the action legitimately is a report submission, allow 보고했다 but still
        # discourage 공시했다 as the lead verb.
        result = {
            "action_type": spec.get("action_type", "unspecified_disclosure"),
            "plain_action_ko": spec.get("plain_action_ko", ""),
            "corporate_actor": corporate_actor,
            "actor_topic_particle_ko": actor_topic_particle,
            "object_ko": spec.get("object_ko", ""),
            "allowed_verbs_ko": spec.get("allowed_verbs_ko", ["결정했다", "진행한다"]),
            "avoid_verbs_ko": avoid_verbs + ["의미가 있다", "주목된다"],
            "usable_fact_slots": spec.get("usable_fact_slots", []),
            "sentence_style": "short_korean_financial_wire",
            "do_not_reuse_template": True,
        }
        if actor_override == "exchange" and subject_of_action:
            result["subject_of_action_ko"] = subject_of_action
        if "writing_quality_risk" in spec:
            result["writing_quality_risk"] = spec["writing_quality_risk"]
        return result


class WritingFrameBuilder:
    """Create output-facing writing guidance built around action primitives.

    The frame no longer instructs pr06 to reuse a complete lead sentence.
    It supplies structured primitives (corporate_actor, plain_action_ko,
    allowed_verbs_ko, usable_fact_slots, ...) so the generator constructs a
    natural short wire-news sentence. A legacy reference template is still
    emitted but marked do_not_reuse_template=True.
    """

    GLOBAL_BLOCKED_PHRASES = ClaimPolicyBuilder.BLOCKED_OUTPUT_PHRASES

    # High-level frame metadata kept per family (lead focus / avoid frames).
    FRAME_META_BY_FAMILY: Dict[str, Dict[str, Any]] = {
        "capital_financing": {"frame_type": "financing", "lead_focus": "funding_method_and_use",
                              "avoid_frames": ["주가 하락 원인", "투자심리 악화", "시장 반응 단정"]},
        "equity_investment": {"frame_type": "equity_investment", "lead_focus": "target_company_and_stake",
                              "avoid_frames": ["사업 확장 기대", "호재 해석", "주가 상승 기대"]},
        "investment": {"frame_type": "investment", "lead_focus": "investment_amount_and_target",
                       "avoid_frames": ["성장성 부각", "실적 개선 기대", "주가 재평가"]},
        "contract": {"frame_type": "contract", "lead_focus": "counterparty_amount_period",
                     "avoid_frames": ["실적 개선 확정", "수혜 기대", "주가 상승 원인"]},
        "asset_transaction": {"frame_type": "asset_transaction", "lead_focus": "asset_type_amount_purpose",
                              "avoid_frames": ["수익성 개선 확정", "유동성 우려 단정", "시장 반응 단정"]},
        "business_transfer": {"frame_type": "business_restructuring", "lead_focus": "business_scope_and_counterparty",
                              "avoid_frames": ["턴어라운드 확정", "주가 재평가", "체질 개선 단정"]},
        "legal_regulatory": {"frame_type": "legal_regulatory", "lead_focus": "authority_case_action",
                             "avoid_frames": ["불확실성 확대 단정", "악재 확정", "주가 하락 원인"]},
        "trading_status": {"frame_type": "trading_status", "lead_focus": "exchange_action_and_date",
                          "avoid_frames": ["투자심리 위축", "급락 원인", "불안 확대"]},
        "listing_risk": {"frame_type": "listing_risk", "lead_focus": "exchange_notice_and_reason",
                        "avoid_frames": ["패닉", "투매", "주가 급락 원인"]},
        "major_management_matter": {"frame_type": "management_disclosure", "lead_focus": "decision_subject_and_terms",
                                   "avoid_frames": ["중대 변수", "시장 관심", "향후 흐름"]},
        "management_governance": {"frame_type": "governance", "lead_focus": "appointment_or_control_change",
                                 "avoid_frames": ["경영 불확실성 단정", "쇄신 기대", "시장 반응"]},
        "earnings": {"frame_type": "earnings", "lead_focus": "sales_operating_profit_net_income",
                    "avoid_frames": ["어닝 서프라이즈 단정", "투자심리 개선", "주가 상승 원인"]},
        "guidance": {"frame_type": "guidance", "lead_focus": "forecast_metric_and_period",
                    "avoid_frames": ["기대감 확대", "실적 개선 확정", "시장 호재"]},
        "dividend": {"frame_type": "dividend", "lead_focus": "dividend_per_share_record_date_payment",
                    "avoid_frames": ["주주환원 기대", "호재로 인식", "투자자 관심"]},
        "treasury_stock": {"frame_type": "treasury_stock", "lead_focus": "share_count_amount_method_period",
                          "avoid_frames": ["주가 방어 목적 단정", "신뢰 회복", "시장 호재"]},
    }

    DEFAULT_META = {
        "frame_type": "plain_disclosure",
        "lead_focus": "disclosed_action_subject_terms",
        "avoid_frames": ["주가 원인", "시장 반응 단정", "투자심리 추정"],
    }

    def __init__(self) -> None:
        self.action_resolver = ActionPrimitiveResolver()

    def build(self, bundle: Dict[str, Any], claim_level: str) -> Dict[str, Any]:
        family = str(bundle.get("event_family") or "").lower()
        meta = dict(self.FRAME_META_BY_FAMILY.get(family, self.DEFAULT_META))
        company = bundle.get("stock_name") or bundle.get("stock_code") or "회사"
        topic = bundle.get("candidate_topic") or "공시"

        action = self.action_resolver.resolve(bundle)

        frame = {
            "frame_type": meta.get("frame_type", "plain_disclosure"),
            "lead_focus": meta.get("lead_focus", "disclosed_action_subject_terms"),
            "company_reference": company,
            "candidate_topic": topic,

            # --- Action primitives (the writer builds the sentence from these) ---
            "action_type": action["action_type"],
            "plain_action_ko": action["plain_action_ko"],
            "corporate_actor": action["corporate_actor"],
            "actor_topic_particle_ko": action["actor_topic_particle_ko"],
            "object_ko": action["object_ko"],
            "allowed_verbs_ko": action["allowed_verbs_ko"],
            "avoid_verbs_ko": action["avoid_verbs_ko"],
            "usable_fact_slots": action["usable_fact_slots"],
            "sentence_style": action["sentence_style"],
            "do_not_reuse_template": True,

            "avoid_frames": meta.get("avoid_frames", []),
            "blocked_phrases": self.GLOBAL_BLOCKED_PHRASES,
            "banned_sentence_openers": [
                "이번 이벤트는",
                "이번 공시는",
                "해당 이벤트는",
                "해당 사안은",
                "시장 참여자들은",
                "투자자들은",
            ],
            "style_rules": [
                "첫 문장은 회사명 + 구체적 행위 동사로 시작한다.",
                "plain_action_ko와 allowed_verbs_ko를 조합해 자연스러운 한 문장을 만든다.",
                "absolutely do not reuse a fixed lead template; write a fresh sentence.",
                "usable_fact_slots 중 근거에 실제로 있는 값만 사용한다 (없으면 생략).",
                "avoid_verbs_ko(공시했다·밝혔다 등)를 반복 사용하지 않는다.",
                "가격 원인 claim이 허용되지 않으면 주가·거래량 문장을 쓰지 않는다.",
                "문장 끝은 과장 없이 다/했다/한다 계열로 처리한다.",
            ],
            "claim_level": normalize_claim_level(claim_level),
        }

        if "subject_of_action_ko" in action:
            frame["subject_of_action_ko"] = action["subject_of_action_ko"]

        if "writing_quality_risk" in action:
            frame["writing_quality_risk"] = action["writing_quality_risk"]

        if normalize_claim_level(claim_level) == ClaimLevel.NO_MARKET_CLAIM.value:
            frame["price_sentence_allowed"] = False
        else:
            frame["price_sentence_allowed"] = claim_rank(claim_level) >= claim_rank(ClaimLevel.REACTION_ONLY.value)

        # Legacy reference only. Kept so older consumers don't break, but explicitly
        # marked do-not-reuse. The generator must NOT paraphrase this.
        frame["reference_lead_template_ko"] = self._reference_lead(action)
        frame["recommended_lead_template_ko"] = frame["reference_lead_template_ko"]
        frame["template_is_reference_only"] = True

        return frame

    def _reference_lead(self, action: Dict[str, Any]) -> str:
        """Build a non-binding reference lead from primitives, only for auditing."""
        actor = action.get("corporate_actor", "회사")
        particle = action.get("actor_topic_particle_ko", "은/는")
        if particle == "은/는":
            subject = f"{actor},"
        else:
            subject = f"{actor}{particle}"
        plain = action.get("plain_action_ko", "")
        verbs = action.get("allowed_verbs_ko", ["결정했다"])
        verb = verbs[0] if verbs else "결정했다"
        if not plain:
            return f"{subject} 관련 결정을 내렸다. (reference only — do not reuse)"
        return f"{subject} {plain}을(를) {verb}. (reference only — do not reuse)"



# =============================================================================
# Judge input selection and request building
# =============================================================================


class JudgeInputSelector:
    """Select only bundles that need expensive LLM judging.

    Strict v2 principle:
    - If the deterministic ceiling is no_market_claim and the bundle has no price reaction,
      most DART-only bundles do not need LLM causal judging.
    - Routine families are preserved as bundles but blocked from judge input unless they have
      price reaction or stock-event/context corroboration.
    - Rank is still only a cost-priority signal; it never changes the claim ceiling.
    """

    ALWAYS_JUDGE_OFFICIAL_NO_PRICE_FAMILIES = {
        "earnings",
        "guidance",
        "contract",
        "legal_regulatory",
        "trading_status",
        "listing_risk",
        "management_governance",
    }

    def __init__(self, config: Pr05eConfig):
        self.config = config

    def mark_initial_allowed(self, bundle: Dict[str, Any]) -> Tuple[bool, str]:
        family = str(bundle.get("event_family") or "").lower()
        precheck = bundle.get("bundle_precheck", {})
        ceiling = normalize_claim_level(
            bundle.get(
                "max_allowed_market_claim_level_pre_llm",
                ClaimLevel.INSUFFICIENT_EVIDENCE.value,
            )
        )

        has_price = bool(precheck.get("has_price_reaction"))
        has_stock_signal = bool(precheck.get("has_stock_event_trigger")) or bool(
            precheck.get("has_stock_event_context")
        )
        has_dart = bool(precheck.get("has_dart"))

        if ceiling == ClaimLevel.INSUFFICIENT_EVIDENCE.value:
            return False, "blocked_insufficient_evidence"

        # Price reaction creates a real judgment problem: keep it.
        if has_price:
            return True, "has_price_reaction"

        # stock_event trigger/context means curated topic corroboration exists; judge may still
        # decide usable/background/reject, but cannot raise the ceiling past no_market_claim.
        if has_stock_signal:
            return True, "has_stock_event_trigger_or_context"

        # Routine families without price/context are usually low-value judge inputs.
        if family in ROUTINE_EVENT_FAMILIES:
            return False, "blocked_routine_without_price_or_context"

        # Weak/other families without price/context are not worth judge cost.
        if family in WEAK_EVENT_FAMILIES:
            return False, "blocked_weak_without_price_or_context"

        # DART-only/no-price official events are already deterministic no_market_claim.
        # Only keep a narrow set of highly material families for judge review.
        if has_dart and family in self.ALWAYS_JUDGE_OFFICIAL_NO_PRICE_FAMILIES:
            return True, "official_no_price_material_family"

        if has_dart:
            return False, "blocked_official_no_price_deterministic_no_market_claim"

        return False, "blocked_low_priority_no_price_no_context"

    def apply_caps(self, bundles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Sort candidates so stronger bundles consume caps first.
        def sort_key(b: Dict[str, Any]) -> Tuple[Any, ...]:
            pre = b.get("bundle_precheck", {})
            return (
                b.get("stock_code", ""),
                extract_year(b.get("anchor_date", "")),
                0 if pre.get("has_price_reaction") else 1,
                int(b.get("bundle_candidate_rank", 999)),
                -claim_rank(b.get("max_allowed_market_claim_level_pre_llm", ClaimLevel.INSUFFICIENT_EVIDENCE.value)),
                -int(pre.get("has_strong_price_reaction", False)),
                -self._total_evidence_count(b),
                b.get("anchor_date", ""),
                b.get("bundle_id", ""),
            )

        sorted_indices = sorted(range(len(bundles)), key=lambda i: sort_key(bundles[i]))
        stock_year_counts: Counter = Counter()
        family_stock_year_counts: Counter = Counter()
        official_no_price_counts: Counter = Counter()

        for i in sorted_indices:
            bundle = bundles[i]
            initially_allowed = bool(bundle.get("judge_input_allowed_initial", False))
            if not initially_allowed:
                bundle["judge_input_allowed"] = False
                bundle["judge_input_block_reason"] = bundle.get("judge_input_reason", "blocked_initial")
                continue

            stock = bundle.get("stock_code", "") or "unknown"
            year = extract_year(bundle.get("anchor_date", ""))
            family = str(bundle.get("event_family") or "").lower()
            precheck = bundle.get("bundle_precheck", {})
            key = (stock, year)

            if stock_year_counts[key] >= self.config.max_judge_inputs_per_stock_year:
                bundle["judge_input_allowed"] = False
                bundle["judge_input_block_reason"] = "blocked_by_max_judge_inputs_per_stock_year"
                continue

            family_key = (stock, year, family)
            family_cap = self._family_cap(family)
            if family_cap is not None and family_stock_year_counts[family_key] >= family_cap:
                bundle["judge_input_allowed"] = False
                bundle["judge_input_block_reason"] = f"blocked_by_{family}_stock_year_cap"
                continue

            if (
                precheck.get("has_dart")
                and not precheck.get("has_price_reaction")
                and not precheck.get("has_stock_event_trigger")
                and not precheck.get("has_stock_event_context")
            ):
                if official_no_price_counts[key] >= self.config.max_official_no_price_per_stock_year:
                    bundle["judge_input_allowed"] = False
                    bundle["judge_input_block_reason"] = "blocked_by_official_no_price_stock_year_cap"
                    continue
                official_no_price_counts[key] += 1

            bundle["judge_input_allowed"] = True
            bundle["judge_input_block_reason"] = ""
            stock_year_counts[key] += 1
            family_stock_year_counts[family_key] += 1

        return bundles

    def _family_cap(self, family: str) -> Optional[int]:
        if family == "dividend":
            return self.config.max_dividend_per_stock_year
        if family == "treasury_stock":
            return self.config.max_treasury_per_stock_year
        if family == "other_company_event":
            return self.config.max_other_per_stock_year
        return None

    def _total_evidence_count(self, bundle: Dict[str, Any]) -> int:
        return sum(len(bundle.get(f"{source}_evidence", [])) for source in SOURCE_NAMES)


class JudgeRequestBuilder:
    def __init__(self, model: str):
        self.model = model

    def build_request(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        compact_payload = self._compact_payload(bundle)
        return {
            "custom_id": f"bundle_judge_{bundle['bundle_id']}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": json_dumps_safe(compact_payload)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        }

    def _system_prompt(self) -> str:
        return (
            "You are a strict evidence-bundle judge for Korean stock-news generation. "
            "You do not write news. You only return JSON. "
            "Never exceed max_allowed_market_claim_level_pre_llm. "
            "DART disclosures are official topic evidence, not automatic market-cause evidence. "
            "Price-volume evidence is reaction evidence, not causal evidence by itself. "
            "Macro and GDELT evidence cannot become a stock-specific main cause alone. "
            "If you assign any level above no_market_claim, cite selected_evidence_ids. "
            "If selected evidence does not justify the requested level, choose a lower level. "
            "Return JSON with keys: bundle_decision, market_claim_level, corroboration_level, "
            "directional_consistency, selected_evidence_ids, rejected_evidence_ids, allowed_claims, "
            "forbidden_claims, confidence, reason_ko. "
            "Allowed market_claim_level values are: insufficient_evidence, no_market_claim, "
            "reaction_only, plausible_market_context, likely_contributor, "
            "strongest_attributable_disclosed_factor."
        )

    def _compact_payload(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "bundle_id": bundle["bundle_id"],
            "event_group_id": bundle["event_group_id"],
            "stock_code": bundle["stock_code"],
            "stock_name": bundle["stock_name"],
            "anchor_date": bundle["anchor_date"],
            "candidate_topic": bundle["candidate_topic"],
            "primary_topic_source": bundle["primary_topic_source"],
            "event_family": bundle["event_family"],
            "bundle_candidate_rank": bundle["bundle_candidate_rank"],
            "rank_reason": bundle["rank_reason"],
            "bundle_precheck": bundle["bundle_precheck"],
            "source_caps": bundle["source_caps"],
            "combination_rule_result": bundle["combination_rule_result"],
            "corroboration_level": bundle["corroboration_level"],
            "max_allowed_market_claim_level_pre_llm": bundle["max_allowed_market_claim_level_pre_llm"],
            "generation_policy": bundle["generation_policy"],
            "writing_frame": bundle.get("writing_frame", {}),
            "allowed_claims": bundle["allowed_claims"],
            "forbidden_claims": bundle["forbidden_claims"],
            "evidence": {
                "dart": self._compact_evidence(bundle.get("dart_evidence", [])),
                "stock_event": self._compact_evidence(bundle.get("stock_event_evidence", [])),
                "stock_event_context": self._compact_evidence(bundle.get("stock_event_context_evidence", [])),
                "price_volume": self._compact_evidence(bundle.get("price_volume_evidence", [])),
                "gdelt": self._compact_evidence(bundle.get("gdelt_evidence", [])),
                "macro": self._compact_evidence(bundle.get("macro_evidence", [])),
            },
            "pr05g_enforcement_contract": {
                "final_market_claim_level_rule": "min(pr05e.max_allowed_market_claim_level_pre_llm, llm_judge.market_claim_level)",
                "requires_selected_evidence_id_validation": True,
                "auto_demote_if_preconditions_fail": True,
            },
        }

    def _compact_evidence(self, items: List[Dict[str, Any]], max_items: int = 8) -> List[Dict[str, Any]]:
        compact_items: List[Dict[str, Any]] = []
        keep_keys = [
            "evidence_id",
            "evidence_source",
            "event_family",
            "report_nm",
            "source_report_name",
            "rcept_dt",
            "date",
            "ref_date",
            "title",
            "headline",
            "summary",
            "direction",
            "directional_consistency",
            "has_strong_price_reaction",
            "strong_price_reaction",
            "return_pct",
            "abs_return",
            "volume_z",
            "materiality_score",
            "final_can_be_news_trigger",
            "final_allowed_usage",
        ]
        for item in items[:max_items]:
            compact: Dict[str, Any] = {}
            for key in keep_keys:
                if key in item and not is_nullish(item[key]):
                    compact[key] = compact_text(item[key], 240) if isinstance(item[key], str) else item[key]
            if "text" not in compact:
                fallback = first_present(item, ["detail", "content", "body", "description", "value"])
                if not is_nullish(fallback):
                    compact["text"] = compact_text(fallback, 240)
            compact_items.append(compact)
        return compact_items


# =============================================================================
# Writers
# =============================================================================


class BundleWriter:
    CSV_COLUMNS = [
        "bundle_id",
        "event_group_id",
        "stock_code",
        "stock_name",
        "anchor_date",
        "event_family",
        "candidate_topic",
        "primary_topic_source",
        "bundle_candidate_rank",
        "rank_reason",
        "corroboration_level",
        "max_allowed_market_claim_level_pre_llm",
        "needs_bundle_llm_judge",
        "judge_input_allowed",
        "judge_input_reason",
        "judge_input_block_reason",
        "action_type",
        "plain_action_ko",
        "writing_quality_risk",
        "dart_count",
        "stock_event_count",
        "stock_event_context_count",
        "price_volume_count",
        "gdelt_count",
        "macro_count",
        "has_dart",
        "has_official_evidence",
        "has_stock_event_trigger",
        "has_stock_event_context",
        "has_price_reaction",
        "has_strong_price_reaction",
        "has_gdelt_support",
        "has_macro_background",
        "directional_consistency",
        "source_caps",
        "combination_rule_result",
        "generation_policy",
        "writing_frame",
        "allowed_claims",
        "forbidden_claims",
    ]

    def __init__(self, config: Pr05eConfig):
        self.config = config

    def write_all(self, bundles: List[Dict[str, Any]], judge_requests: List[Dict[str, Any]]) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(self.config.bundles_jsonl_path, bundles)
        self._write_csv(self.config.bundles_csv_path, bundles)
        write_jsonl(self.config.judge_inputs_jsonl_path, judge_requests)
        self._write_report(self.config.report_path, bundles, judge_requests)

    def _write_csv(self, path: Path, bundles: List[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.CSV_COLUMNS)
            writer.writeheader()
            for bundle in bundles:
                row = self._flatten_bundle_for_csv(bundle)
                writer.writerow(row)

    def _flatten_bundle_for_csv(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        pre = bundle.get("bundle_precheck", {})
        frame = bundle.get("writing_frame", {}) if isinstance(bundle.get("writing_frame"), dict) else {}
        row = {
            "bundle_id": bundle.get("bundle_id", ""),
            "event_group_id": bundle.get("event_group_id", ""),
            "stock_code": bundle.get("stock_code", ""),
            "stock_name": bundle.get("stock_name", ""),
            "anchor_date": bundle.get("anchor_date", ""),
            "event_family": bundle.get("event_family", ""),
            "candidate_topic": bundle.get("candidate_topic", ""),
            "primary_topic_source": bundle.get("primary_topic_source", ""),
            "bundle_candidate_rank": bundle.get("bundle_candidate_rank", ""),
            "rank_reason": bundle.get("rank_reason", ""),
            "corroboration_level": bundle.get("corroboration_level", ""),
            "max_allowed_market_claim_level_pre_llm": bundle.get("max_allowed_market_claim_level_pre_llm", ""),
            "needs_bundle_llm_judge": bundle.get("needs_bundle_llm_judge", ""),
            "judge_input_allowed": bundle.get("judge_input_allowed", ""),
            "judge_input_reason": bundle.get("judge_input_reason", ""),
            "judge_input_block_reason": bundle.get("judge_input_block_reason", ""),
            "action_type": frame.get("action_type", ""),
            "plain_action_ko": frame.get("plain_action_ko", ""),
            "writing_quality_risk": frame.get("writing_quality_risk", ""),
            "dart_count": len(bundle.get("dart_evidence", [])),
            "stock_event_count": len(bundle.get("stock_event_evidence", [])),
            "stock_event_context_count": len(bundle.get("stock_event_context_evidence", [])),
            "price_volume_count": len(bundle.get("price_volume_evidence", [])),
            "gdelt_count": len(bundle.get("gdelt_evidence", [])),
            "macro_count": len(bundle.get("macro_evidence", [])),
            "has_dart": pre.get("has_dart", False),
            "has_official_evidence": pre.get("has_official_evidence", False),
            "has_stock_event_trigger": pre.get("has_stock_event_trigger", False),
            "has_stock_event_context": pre.get("has_stock_event_context", False),
            "has_price_reaction": pre.get("has_price_reaction", False),
            "has_strong_price_reaction": pre.get("has_strong_price_reaction", False),
            "has_gdelt_support": pre.get("has_gdelt_support", False),
            "has_macro_background": pre.get("has_macro_background", False),
            "directional_consistency": pre.get("directional_consistency", ""),
            "source_caps": json_dumps_safe(bundle.get("source_caps", {})),
            "combination_rule_result": json_dumps_safe(bundle.get("combination_rule_result", {})),
            "generation_policy": json_dumps_safe(bundle.get("generation_policy", {})),
            "writing_frame": json_dumps_safe(bundle.get("writing_frame", {})),
            "allowed_claims": json_dumps_safe(bundle.get("allowed_claims", [])),
            "forbidden_claims": json_dumps_safe(bundle.get("forbidden_claims", [])),
        }
        return row

    def _write_report(self, path: Path, bundles: List[Dict[str, Any]], judge_requests: List[Dict[str, Any]]) -> None:
        lines: List[str] = []
        total = len(bundles)
        allowed = sum(1 for b in bundles if b.get("judge_input_allowed"))
        blocked = total - allowed

        lines.append("# Stock Evidence Bundle Report")
        lines.append("")
        lines.append("## Summary")
        lines.append(f"- total_bundles: {total}")
        lines.append(f"- judge_input_allowed: {allowed}")
        lines.append(f"- judge_input_blocked: {blocked}")
        lines.append(f"- bundle_judge_inputs_jsonl_rows: {len(judge_requests)}")
        lines.append("")

        self._append_counter_section(lines, "Counts by event_family", Counter(b.get("event_family", "unknown") for b in bundles))
        self._append_counter_section(lines, "Counts by bundle_candidate_rank", Counter(str(b.get("bundle_candidate_rank", "unknown")) for b in bundles))
        self._append_counter_section(
            lines,
            "Counts by max_allowed_market_claim_level_pre_llm",
            Counter(b.get("max_allowed_market_claim_level_pre_llm", "unknown") for b in bundles),
        )
        self._append_counter_section(lines, "Counts by corroboration_level", Counter(b.get("corroboration_level", "unknown") for b in bundles))
        self._append_counter_section(lines, "Counts by judge_input_allowed", Counter(str(b.get("judge_input_allowed")) for b in bundles))
        self._append_counter_section(lines, "Counts by primary_topic_source", Counter(b.get("primary_topic_source", "unknown") for b in bundles))
        self._append_counter_section(
            lines,
            "Counts by directional_consistency",
            Counter(b.get("bundle_precheck", {}).get("directional_consistency", "unknown") for b in bundles),
        )
        self._append_counter_section(
            lines,
            "Counts by action_type",
            Counter(b.get("writing_frame", {}).get("action_type", "unknown") for b in bundles),
        )
        self._append_counter_section(
            lines,
            "Counts by writing_quality_risk",
            Counter(b.get("writing_frame", {}).get("writing_quality_risk", "ok") or "ok" for b in bundles),
        )
        self._append_counter_section(lines, "Counts by judge_input_block_reason", Counter(b.get("judge_input_block_reason", "") or "allowed" for b in bundles))

        lines.append("## Safety notes")
        lines.append("- DART is official topic evidence, not automatic causal evidence.")
        lines.append("- stock_event cannot create market-cause claims by itself.")
        lines.append("- price-volume is reaction-only evidence.")
        lines.append("- macro/GDELT cannot become stock-specific main cause alone.")
        lines.append("- bundle_candidate_rank must not feed claim ceiling computation.")
        lines.append("- later LLM judge cannot exceed deterministic ceiling.")
        lines.append("- pr05g must apply: final_market_claim_level = min(pr05e ceiling, LLM judge level).")
        lines.append("- pr05g must validate selected_evidence_ids and auto-demote if level preconditions fail.")
        lines.append("- pr06 must respect generation_policy to avoid causal implicature through sentence adjacency.")
        lines.append("- pr06 must build sentences from writing_frame action primitives (plain_action_ko, allowed_verbs_ko, usable_fact_slots) and must NOT reuse recommended_lead_template_ko, which is reference-only.")
        lines.append("")

        lines.append("## Output files")
        lines.append(f"- {self.config.bundles_jsonl_path}")
        lines.append(f"- {self.config.bundles_csv_path}")
        lines.append(f"- {self.config.judge_inputs_jsonl_path}")
        lines.append(f"- {self.config.report_path}")
        lines.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

    def _append_counter_section(self, lines: List[str], title: str, counter: Counter) -> None:
        lines.append(f"## {title}")
        if not counter:
            lines.append("- none: 0")
        else:
            for key, value in counter.most_common():
                lines.append(f"- {key}: {value}")
        lines.append("")


# =============================================================================
# Pipeline
# =============================================================================


class Pr05ePipeline:
    def __init__(self, config: Pr05eConfig):
        self.config = config
        self.loader = EventGroupLoader(config.event_groups_jsonl)
        self.normalizer = EventGroupNormalizer()
        self.bundle_builder = EvidenceBundleBuilder()
        self.precheck_evaluator = BundlePrecheckEvaluator()
        self.ranker = BundleRanker()
        self.corroboration_evaluator = CorroborationEvaluator()
        self.source_cap_evaluator = SourceCapEvaluator()
        self.claim_ceiling_evaluator = ClaimCeilingEvaluator()
        self.claim_policy_builder = ClaimPolicyBuilder()
        self.writing_frame_builder = WritingFrameBuilder()
        self.judge_selector = JudgeInputSelector(config)
        self.judge_request_builder = JudgeRequestBuilder(config.model)
        self.writer = BundleWriter(config)

    def run(self) -> None:
        print("=" * 100)
        print("[pr05e] Build stock evidence bundles")
        print(f"event_groups_jsonl: {self.config.event_groups_jsonl}")
        print(f"output_dir: {self.config.output_dir}")
        print(f"model: {self.config.model}")
        print("=" * 100)

        raw_groups = self.loader.load()
        print(f"[load] event groups: {len(raw_groups):,}")

        bundles: List[Dict[str, Any]] = []
        for idx, raw_group in enumerate(raw_groups, start=1):
            normalized = self.normalizer.normalize(raw_group, idx)
            bundle = self.bundle_builder.build(normalized, idx)
            self._enrich_bundle(bundle)
            bundles.append(bundle)

        bundles = self.judge_selector.apply_caps(bundles)
        judge_requests = [
            self.judge_request_builder.build_request(bundle)
            for bundle in bundles
            if bundle.get("judge_input_allowed")
        ]

        # Remove raw event group from JSONL? Keep it by default for auditability.
        # If file size becomes too large, this can be made optional later.
        self.writer.write_all(bundles, judge_requests)
        self._print_summary(bundles, judge_requests)

    def _enrich_bundle(self, bundle: Dict[str, Any]) -> None:
        precheck = self.precheck_evaluator.evaluate(bundle)
        rank, rank_reason = self.ranker.rank(bundle, precheck)
        corroboration = self.corroboration_evaluator.evaluate(bundle, precheck)
        source_caps = self.source_cap_evaluator.evaluate(bundle, precheck)
        ceiling, combination_rule_result = self.claim_ceiling_evaluator.evaluate(bundle, precheck, source_caps)
        allowed_claims, forbidden_claims = self.claim_policy_builder.build_allowed_forbidden(ceiling)
        generation_policy = self.claim_policy_builder.build_generation_policy(ceiling)
        writing_frame = self.writing_frame_builder.build(bundle, ceiling)
        judge_allowed_initial, judge_reason = self.judge_selector.mark_initial_allowed({
            **bundle,
            "bundle_precheck": precheck,
            "bundle_candidate_rank": rank,
            "max_allowed_market_claim_level_pre_llm": ceiling,
        })

        bundle["bundle_precheck"] = precheck
        bundle["bundle_candidate_rank"] = rank
        bundle["rank_reason"] = rank_reason
        bundle["corroboration_level"] = corroboration
        bundle["source_caps"] = source_caps
        bundle["combination_rule_result"] = combination_rule_result
        bundle["max_allowed_market_claim_level_pre_llm"] = ceiling
        bundle["needs_bundle_llm_judge"] = judge_allowed_initial
        bundle["judge_input_allowed_initial"] = judge_allowed_initial
        bundle["judge_input_reason"] = judge_reason
        bundle["judge_input_allowed"] = judge_allowed_initial
        bundle["judge_input_block_reason"] = "" if judge_allowed_initial else judge_reason
        bundle["allowed_claims"] = allowed_claims
        bundle["forbidden_claims"] = forbidden_claims
        bundle["generation_policy"] = generation_policy
        bundle["writing_frame"] = writing_frame
        bundle["pr05g_enforcement_contract"] = {
            "final_market_claim_level_rule": "min(pr05e.max_allowed_market_claim_level_pre_llm, llm_judge.market_claim_level)",
            "requires_selected_evidence_id_validation": True,
            "auto_demote_if_preconditions_fail": True,
        }

    def _print_summary(self, bundles: List[Dict[str, Any]], judge_requests: List[Dict[str, Any]]) -> None:
        total = len(bundles)
        allowed = sum(1 for b in bundles if b.get("judge_input_allowed"))
        print("\n[summary]")
        print(f"total_bundles: {total:,}")
        print(f"judge_input_allowed: {allowed:,}")
        print(f"judge_input_blocked: {total - allowed:,}")
        print(f"bundle_judge_inputs_jsonl_rows: {len(judge_requests):,}")

        print("\n[claim levels]")
        for k, v in Counter(b.get("max_allowed_market_claim_level_pre_llm", "unknown") for b in bundles).most_common():
            print(f"  {k}: {v:,}")

        print("\n[event families]")
        for k, v in Counter(b.get("event_family", "unknown") for b in bundles).most_common(30):
            print(f"  {k}: {v:,}")

        print("\n[action types]")
        for k, v in Counter(b.get("writing_frame", {}).get("action_type", "unknown") for b in bundles).most_common(30):
            print(f"  {k}: {v:,}")

        print("\n[outputs]")
        print(f"  {self.config.bundles_jsonl_path}")
        print(f"  {self.config.bundles_csv_path}")
        print(f"  {self.config.judge_inputs_jsonl_path}")
        print(f"  {self.config.report_path}")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build stock evidence bundles from pr05d stock event groups."
    )
    parser.add_argument(
        "--event-groups-jsonl",
        type=Path,
        default=Path("/Users/hgs/Desktop/IISE CD/data/interim/pr05d_stock_event_groups/stock_event_groups.jsonl"),
        help="Input pr05d stock_event_groups.jsonl path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles"),
        help="Output directory.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o-mini",
        help="Model name to store inside batch-style judge input JSONL. No API call is made.",
    )
    parser.add_argument(
        "--max-judge-inputs-per-stock-year",
        type=int,
        default=8,
        help="Maximum judge inputs per stock-year.",
    )
    parser.add_argument(
        "--max-dividend-per-stock-year",
        type=int,
        default=0,
        help="Maximum dividend bundles sent to judge per stock-year.",
    )
    parser.add_argument(
        "--max-treasury-per-stock-year",
        type=int,
        default=1,
        help="Maximum treasury-stock bundles sent to judge per stock-year.",
    )
    parser.add_argument(
        "--max-other-per-stock-year",
        type=int,
        default=1,
        help="Maximum other_company_event bundles sent to judge per stock-year.",
    )
    parser.add_argument(
        "--max-official-no-price-per-stock-year",
        type=int,
        default=3,
        help="Maximum DART-only/no-price official bundles sent to judge per stock-year.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Pr05eConfig:
    return Pr05eConfig(
        event_groups_jsonl=args.event_groups_jsonl,
        output_dir=args.output_dir,
        model=args.model,
        max_judge_inputs_per_stock_year=args.max_judge_inputs_per_stock_year,
        max_dividend_per_stock_year=args.max_dividend_per_stock_year,
        max_treasury_per_stock_year=args.max_treasury_per_stock_year,
        max_other_per_stock_year=args.max_other_per_stock_year,
        max_official_no_price_per_stock_year=args.max_official_no_price_per_stock_year,
    )


def main() -> None:
    args = parse_args()
    config = build_config(args)
    pipeline = Pr05ePipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()