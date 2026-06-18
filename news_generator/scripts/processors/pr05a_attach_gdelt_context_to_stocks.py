# news_generator/scripts/processors/pr05a_attach_gdelt_context_to_stocks.py
# -*- coding: utf-8 -*-
"""
pr05a_attach_gdelt_context_to_stocks.py

목적:
    pr_gdelt01(v4)에서 만든 GDELT context 후보 pool을
    stock profile / DART / price-volume / community context와 결합하여
    종목별(stock-date) LLM judge용 context 후보 card를 만든다.

핵심 원칙:
    - GDELT 단독으로 개별 종목 원인을 단정하지 않는다.
    - v3부터 pr05a는 최종 확정기가 아니라 LLM judge 후보 생성기다.
    - sector_linkable은 hard_match_tags와 stock sensitivity_tags 교집합이 있어야 attach 가능하다.
    - company_profile_dependent는 profile_factor_tags와 stock sensitivity_tags 교집합이 있어야 하며 단독 사용 금지다.
    - broad_macro는 stock-specific driver가 있는 stock-date에만 background로 붙인다.
    - low confidence GDELT는 DART / price-volume / community 중 하나 이상의 corroboration이 없으면 사용을 제한한다.

입력 예:
    data/processed/gdelt_context/gdelt_context_summary.csv
    data/processed/stock_profiles/stock_profile_yearly.csv
    선택: DART events csv
    선택: price-volume context csv
    선택: community activity context csv

출력:
    gdelt_stock_context_cards.csv
    gdelt_stock_context_cards.jsonl
    gdelt_stock_context_attach_report.txt
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd


# =============================================================================
# Basic utils
# =============================================================================


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>", "nat"}:
        return ""
    return re.sub(r"\s+", " ", text)


def normalize_date(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 8:
        try:
            return pd.Timestamp(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}").strftime("%Y-%m-%d")
        except Exception:
            pass
    dt = pd.to_datetime(text, errors="coerce")
    if pd.isna(dt):
        return ""
    return dt.strftime("%Y-%m-%d")


def normalize_stock_code(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return text
    return digits.zfill(6)[-6:]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def parse_tags(value: Any) -> List[str]:
    """pipe/list/json-like tag parser."""
    text = clean_text(value)
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple, set)):
                return unique_list([clean_text(x) for x in parsed if clean_text(x)])
        except Exception:
            pass

    # JSON list fallback
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return unique_list([clean_text(x) for x in parsed if clean_text(x)])
    except Exception:
        pass

    parts = re.split(r"[|,;/]+", text)
    return unique_list([p.strip() for p in parts if p.strip()])


def unique_list(values: Iterable[str], max_items: Optional[int] = None) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for value in values:
        value = clean_text(value)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if max_items is not None and len(out) >= max_items:
            break
    return out


def join_tags(values: Iterable[str], max_items: Optional[int] = None) -> str:
    return "|".join(unique_list(values, max_items=max_items))


def find_first_existing_column(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    lower_map = {str(c).lower(): c for c in df.columns}
    for c in candidates:
        if c in df.columns:
            return c
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def ensure_columns(df: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = ""
    return out


# =============================================================================
# Canonical tag rules
# =============================================================================


class TagOntology:
    """profile text에서 canonical sensitivity tag를 추출하기 위한 보수적 규칙."""

    KEYWORD_RULES: List[Tuple[str, str]] = [
        (r"반도체|semiconductor|chip|메모리|dram|nand|foundry|파운드리", "sector_semiconductor"),
        (r"배터리|2차전지|secondary battery|lithium|리튬|양극재|음극재", "sector_battery"),
        (r"해운|shipping|선박|운임|freight|container|컨테이너|항만|물류|logistics", "sector_shipping"),
        (r"항공|airline|aviation|공항|여객|화물기", "sector_airline"),
        (r"자동차|auto|vehicle|완성차|전기차|ev|부품", "sector_auto"),
        (r"화학|chemical|석유화학|petrochemical|나프타|naphtha|정유", "sector_chemical"),
        (r"철강|steel|금속|metal|구리|copper|알루미늄|원자재", "sector_steel"),
        (r"제약|바이오|bio|pharma|의약|신약|의료|헬스케어|healthcare", "sector_bio_pharma"),
        (r"콘텐츠|엔터|entertainment|게임|game|미디어|드라마|영화|음악|k-pop|kpop", "sector_content"),
        (r"건설|construction|부동산|real estate|주택|토목|분양|플랜트", "sector_construction"),
        (r"은행|금융|보험|증권|brokerage|financial|card|카드", "sector_financial"),
        (r"소비|retail|유통|면세|duty free|호텔|관광|화장품|cosmetic|식품|음료", "sector_consumer"),
        (r"통신|telecom|5g|lte|이동통신|sk텔레콤|kt|lg유플러스", "sector_telecom"),
        (r"기술|technology|ict|software|소프트웨어|플랫폼|ai|cloud|클라우드", "sector_technology"),
        (r"금리|interest rate|rate|채권|bond|통화정책|monetary|대출|예대마진", "rate_sensitive"),
        (r"물가|인플레이션|inflation|cpi|ppi|가격 부담|원가 부담", "inflation_sensitive"),
        (r"수출|export|무역|trade|관세|tariff|글로벌|해외 매출|해외시장", "exposure_export"),
        (r"중국|china|chinese|중화권|방한|유커", "exposure_china"),
        (r"유가|oil|wti|brent|원유|연료비|fuel|에너지 가격", "commodity_oil"),
        (r"원자재|raw material|철광석|구리|lithium|리튬|니켈|원재료", "commodity_raw_material"),
    ]

    SECTOR_TAGS: Set[str] = {
        "sector_semiconductor", "sector_battery", "sector_shipping", "sector_airline",
        "sector_auto", "sector_chemical", "sector_steel", "sector_bio_pharma",
        "sector_content", "sector_construction", "sector_financial", "sector_consumer",
        "sector_telecom", "sector_technology",
    }

    PROFILE_FACTOR_TAGS: Set[str] = {
        "rate_sensitive", "inflation_sensitive", "exposure_export", "exposure_china",
        "commodity_oil", "commodity_raw_material",
    }

    @classmethod
    def extract_tags_from_text(cls, text: str) -> List[str]:
        source = clean_text(text).lower()
        if not source:
            return []
        tags: List[str] = []
        for pattern, tag in cls.KEYWORD_RULES:
            if re.search(pattern, source, flags=re.IGNORECASE):
                tags.append(tag)
        return unique_list(tags)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class StockProfile:
    stock_code: str
    stock_name: str
    business_year: str
    sensitivity_tags: Set[str]
    sector_tags: Set[str]
    profile_factor_tags: Set[str]
    profile_text: str
    business_text: str


@dataclass
class AttachDecision:
    allowed: bool
    gate_reason: str
    matched_tags: Set[str]
    needs_corroboration: bool
    attach_type: str


# =============================================================================
# Loaders
# =============================================================================


class StockProfileLoader:
    """
    Stock profile에서 종목별 canonical tag를 만든다.

    v2 수정 원칙:
        - sector tag는 사업/제품/세그먼트 설명에서만 추출한다.
        - risk/macro 텍스트에서 sector tag를 뽑으면 오탐이 커진다.
          예: 자동차 회사가 금융위험/채권회수능력 때문에 sector_financial로 붙는 문제.
        - factor tag(rate/oil/export/china 등)는 risk/macro/news 텍스트에서도 추출한다.
    """

    BUSINESS_TEXT_COLUMNS = [
        "business_summary_asof",
        "main_products_asof",
        "business_segments_asof",
        "beginner_description",
        "stock_name",
    ]

    FACTOR_TEXT_COLUMNS = [
        "sensitivity_tags",
        "macro_sensitive_factors",
        "news_reaction_tags",
        "asset_personality",
        "risk_keywords_asof",
        "business_summary_asof",
        "main_products_asof",
        "business_segments_asof",
        "beginner_description",
        "stock_name",
    ]

    EXPLICIT_CANONICAL_TAG_COLUMNS = [
        "sensitivity_tags",
        "sector_tags",
        "profile_factor_tags",
    ]

    DISPLAY_TAG_COLUMNS = [
        "sensitivity_tags",
        "macro_sensitive_factors",
        "news_reaction_tags",
        "risk_keywords_asof",
    ]

    @classmethod
    def load(cls, path: Path, target_year: Optional[int] = None) -> List[StockProfile]:
        df = pd.read_csv(path)

        code_col = find_first_existing_column(df, ["stock_code", "종목코드", "code"])
        name_col = find_first_existing_column(df, ["stock_name", "종목명", "name"])
        year_col = find_first_existing_column(df, ["business_year", "year", "기준연도"])

        if code_col is None:
            raise ValueError("stock profile에 stock_code 컬럼이 필요합니다.")
        if name_col is None:
            raise ValueError("stock profile에 stock_name 컬럼이 필요합니다.")

        df = df.copy()
        df[code_col] = df[code_col].astype("string")
        df["_stock_code"] = df[code_col].map(normalize_stock_code)
        df["_stock_name"] = df[name_col].map(clean_text)

        if year_col is not None:
            df["_business_year"] = pd.to_numeric(df[year_col], errors="coerce")
            if target_year is not None:
                eligible = df[df["_business_year"].le(target_year)].copy()
                if not eligible.empty:
                    df = eligible
            df = df.sort_values(["_stock_code", "_business_year"], ascending=[True, False])
            df = df.drop_duplicates("_stock_code", keep="first")
        else:
            df["_business_year"] = ""
            df = df.drop_duplicates("_stock_code", keep="first")

        profiles: List[StockProfile] = []
        for _, row in df.iterrows():
            code = clean_text(row.get("_stock_code"))
            name = clean_text(row.get("_stock_name"))
            if not code or not name:
                continue

            business_text_parts: List[str] = []
            factor_text_parts: List[str] = []
            display_raw_tags: List[str] = []
            explicit_canonical_tags: List[str] = []

            for col in cls.BUSINESS_TEXT_COLUMNS:
                if col in df.columns:
                    business_text_parts.append(clean_text(row.get(col)))

            for col in cls.FACTOR_TEXT_COLUMNS:
                if col in df.columns:
                    factor_text_parts.append(clean_text(row.get(col)))

            for col in cls.DISPLAY_TAG_COLUMNS:
                if col in df.columns:
                    display_raw_tags.extend(parse_tags(row.get(col)))

            for col in cls.EXPLICIT_CANONICAL_TAG_COLUMNS:
                if col in df.columns:
                    for tag in parse_tags(row.get(col)):
                        if tag in TagOntology.SECTOR_TAGS or tag in TagOntology.PROFILE_FACTOR_TAGS:
                            explicit_canonical_tags.append(tag)

            business_text = " ".join([p for p in business_text_parts if p])
            factor_text = " ".join([p for p in factor_text_parts if p])
            profile_text = " ".join([business_text, factor_text]).strip()

            business_inferred = set(TagOntology.extract_tags_from_text(business_text))
            factor_inferred = set(TagOntology.extract_tags_from_text(factor_text))

            sector_tags = {t for t in business_inferred if t in TagOntology.SECTOR_TAGS}
            sector_tags.update({t for t in explicit_canonical_tags if t in TagOntology.SECTOR_TAGS})

            factor_tags = {t for t in factor_inferred if t in TagOntology.PROFILE_FACTOR_TAGS}
            factor_tags.update({t for t in explicit_canonical_tags if t in TagOntology.PROFILE_FACTOR_TAGS})

            # candidate lookup에는 canonical tag만 사용한다. raw 한글 tag는 표시용으로만 일부 보존.
            all_tags = set(sector_tags) | set(factor_tags)
            all_tags.update([t for t in display_raw_tags if not t.startswith("sector_")][:12])

            profiles.append(
                StockProfile(
                    stock_code=code,
                    stock_name=name,
                    business_year=str(row.get("_business_year", "")),
                    sensitivity_tags=all_tags,
                    sector_tags=sector_tags,
                    profile_factor_tags=factor_tags,
                    profile_text=profile_text,
                    business_text=business_text,
                )
            )

        return profiles


class OptionalSignalLoader:
    """DART / price-volume / community csv를 stock-date corroboration signal로 표준화."""

    DATE_CANDIDATES = ["date", "event_date", "rcept_date", "source_rcept_date", "ref_date", "trading_date"]
    CODE_CANDIDATES = ["stock_code", "종목코드", "code"]
    NAME_CANDIDATES = ["stock_name", "종목명", "name"]

    @classmethod
    def load_signal_map(
        cls,
        path: Optional[Path],
        signal_name: str,
        default_strength: float,
        max_rows: Optional[int] = None,
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if path is None:
            return {}
        if not path.exists():
            raise FileNotFoundError(path)

        df = pd.read_csv(path, nrows=max_rows)
        date_col = find_first_existing_column(df, cls.DATE_CANDIDATES)
        code_col = find_first_existing_column(df, cls.CODE_CANDIDATES)
        name_col = find_first_existing_column(df, cls.NAME_CANDIDATES)

        if date_col is None:
            raise ValueError(f"{signal_name}: date 컬럼을 찾지 못했습니다.")
        if code_col is None and name_col is None:
            raise ValueError(f"{signal_name}: stock_code 또는 stock_name 컬럼이 필요합니다.")

        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for _, row in df.iterrows():
            date = normalize_date(row.get(date_col))
            code = normalize_stock_code(row.get(code_col)) if code_col else ""
            name = clean_text(row.get(name_col)) if name_col else ""
            key_id = code or name
            if not date or not key_id:
                continue

            strength = cls._infer_strength(row, default_strength)
            title = cls._infer_title(row)
            key = (date, key_id)
            bucket = out.setdefault(
                key,
                {
                    "signal_name": signal_name,
                    "count": 0,
                    "strength": 0.0,
                    "titles": [],
                },
            )
            bucket["count"] += 1
            bucket["strength"] = max(bucket["strength"], strength)
            if title:
                bucket["titles"].append(title)

        return out

    @staticmethod
    def _infer_strength(row: pd.Series, default_strength: float) -> float:
        candidates = [
            "signal_strength", "event_strength", "abnormal_score", "volume_z", "return_abs_z",
            "residual_z", "activity_z", "burst_score", "score",
        ]
        vals = []
        for col in candidates:
            if col in row.index:
                vals.append(abs(safe_float(row.get(col), 0.0)))
        if not vals:
            return default_strength
        return max(default_strength, min(1.0, max(vals) / 5.0))

    @staticmethod
    def _infer_title(row: pd.Series) -> str:
        candidates = [
            "event_title", "title", "headline", "source_report_name", "report_name",
            "reason", "theme", "summary",
        ]
        for col in candidates:
            if col in row.index:
                value = clean_text(row.get(col))
                if value:
                    return value[:120]
        return ""


# =============================================================================
# GDELT context preparation
# =============================================================================

class ThemePrimarySectorRules:
    """
    theme 문구에서 대표 sector gate를 산출한다.

    v2 수정 원칙:
        - sector_linkable attach는 context의 모든 hard_match_tags가 아니라
          theme의 대표 sector와 stock sector가 맞을 때만 허용한다.
        - co-occurrence 때문에 hard_match_tags에 섞인 보조 sector가
          엉뚱한 종목 attach를 만드는 문제를 막는다.
    """

    THEME_TO_PRIMARY_SECTORS: List[Tuple[str, Tuple[str, ...]]] = [
        (r"반도체", ("sector_semiconductor", "sector_technology")),
        (r"기술", ("sector_technology", "sector_semiconductor", "sector_telecom")),
        (r"배터리", ("sector_battery",)),
        (r"해운|물류", ("sector_shipping",)),
        (r"항공", ("sector_airline",)),
        (r"자동차", ("sector_auto",)),
        (r"화학", ("sector_chemical",)),
        (r"철강|금속", ("sector_steel",)),
        (r"제약|바이오", ("sector_bio_pharma",)),
        (r"콘텐츠|엔터", ("sector_content",)),
        (r"건설|부동산", ("sector_construction",)),
        (r"금융", ("sector_financial",)),
        (r"소비|관광", ("sector_consumer",)),
        (r"통신", ("sector_telecom",)),
    ]

    @classmethod
    def primary_sectors_for_theme(cls, theme: Any, fallback_hard_tags: Set[str]) -> Set[str]:
        text = clean_text(theme)
        for pattern, tags in cls.THEME_TO_PRIMARY_SECTORS:
            if re.search(pattern, text):
                return set(tags) & set(fallback_hard_tags or set()) or set(tags)
        return set(fallback_hard_tags or set())



class GdeltContextLoader:
    REQUIRED = [
        "date", "theme", "evidence_class", "confidence_level", "stock_link_score",
        "hard_match_tags", "profile_factor_tags", "macro_tags", "modifier_tags",
        "source", "raw_count", "reason_code",
        "forbidden_as_standalone_evidence", "requires_profile_match", "requires_corroboration",
    ]

    @classmethod
    def load(cls, path: Path, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        df = pd.read_csv(path)
        df = ensure_columns(df, cls.REQUIRED)
        df = df.copy()
        df["date"] = df["date"].map(normalize_date)
        df = df[df["date"] != ""].copy()

        if start_date:
            df = df[df["date"].ge(normalize_date(start_date))].copy()
        if end_date:
            df = df[df["date"].le(normalize_date(end_date))].copy()

        for col in [
            "hard_match_tags", "profile_factor_tags", "macro_tags", "modifier_tags",
            "sector_tags", "factor_tags", "exposure_tags", "matched_tokens", "strong_tokens",
        ]:
            if col not in df.columns:
                df[col] = ""

        df["_hard_tags"] = df["hard_match_tags"].map(lambda x: set(parse_tags(x)))
        df["_primary_hard_tags"] = df.apply(
            lambda r: ThemePrimarySectorRules.primary_sectors_for_theme(r.get("theme"), r.get("_hard_tags", set())),
            axis=1,
        )
        df["_profile_factor_tags"] = df["profile_factor_tags"].map(lambda x: set(parse_tags(x)))
        df["_macro_tags"] = df["macro_tags"].map(lambda x: set(parse_tags(x)))
        df["_modifier_tags"] = df["modifier_tags"].map(lambda x: set(parse_tags(x)))
        df["stock_link_score"] = pd.to_numeric(df["stock_link_score"], errors="coerce").fillna(0.0)
        df["raw_count"] = pd.to_numeric(df["raw_count"], errors="coerce").fillna(0).astype(int)
        return df


# =============================================================================
# Attach logic
# =============================================================================


class ContextAttachPolicy:
    @staticmethod
    def decide(context: pd.Series, stock: StockProfile) -> AttachDecision:
        evidence_class = clean_text(context.get("evidence_class"))
        confidence = clean_text(context.get("confidence_level"))
        hard_tags: Set[str] = set(context.get("_hard_tags", set()))
        profile_factor_tags: Set[str] = set(context.get("_profile_factor_tags", set()))
        modifier_tags: Set[str] = set(context.get("_modifier_tags", set()))

        if evidence_class == "sector_linkable":
            primary_tags: Set[str] = set(context.get("_primary_hard_tags", set()))
            if not primary_tags:
                primary_tags = hard_tags

            matched = primary_tags & stock.sector_tags
            # explicit sensitivity_tags에 canonical sector가 들어간 경우도 허용
            matched = matched | (primary_tags & {t for t in stock.sensitivity_tags if t in TagOntology.SECTOR_TAGS})

            if not matched:
                return AttachDecision(False, "primary_sector_tag_mismatch", set(), True, "reject")

            needs = confidence in {"low", ""}
            return AttachDecision(True, "primary_sector_tag_match", matched, needs, "sector_context")

        if evidence_class == "company_profile_dependent":
            matched = profile_factor_tags & stock.sensitivity_tags
            matched = matched | (profile_factor_tags & stock.profile_factor_tags)
            if not matched:
                return AttachDecision(False, "profile_factor_mismatch", set(), True, "reject")
            return AttachDecision(True, "profile_factor_match_requires_corroboration", matched, True, "profile_dependent_context")

        if evidence_class == "broad_macro":
            # broad는 직접 stock attach 금지. stock-date에 sector/profile driver가 있을 때 후처리로 붙인다.
            return AttachDecision(False, "broad_macro_background_only", set(), True, "background_only")

        return AttachDecision(False, "noisy_or_unknown", set(), True, "reject")


class CorroborationIndex:
    def __init__(
        self,
        dart: Dict[Tuple[str, str], Dict[str, Any]],
        price: Dict[Tuple[str, str], Dict[str, Any]],
        community: Dict[Tuple[str, str], Dict[str, Any]],
        stock_name_to_code: Dict[str, str],
    ):
        self.dart = dart
        self.price = price
        self.community = community
        self.stock_name_to_code = stock_name_to_code

    def lookup(self, date: str, stock: StockProfile) -> Dict[str, Any]:
        keys = [(date, stock.stock_code), (date, stock.stock_name)]
        result = {
            "has_dart": False,
            "has_price_volume": False,
            "has_community": False,
            "corroboration_score": 0.0,
            "corroboration_sources": [],
            "corroboration_notes": [],
        }

        self._merge_signal(result, self.dart, keys, "dart", 0.18)
        self._merge_signal(result, self.price, keys, "price_volume", 0.22)
        self._merge_signal(result, self.community, keys, "community", 0.16)

        result["corroboration_score"] = min(0.45, result["corroboration_score"])
        result["corroboration_sources"] = unique_list(result["corroboration_sources"])
        result["corroboration_notes"] = unique_list(result["corroboration_notes"], max_items=5)
        return result

    @staticmethod
    def _merge_signal(
        result: Dict[str, Any],
        signal_map: Dict[Tuple[str, str], Dict[str, Any]],
        keys: Sequence[Tuple[str, str]],
        label: str,
        base_bonus: float,
    ) -> None:
        for key in keys:
            if key not in signal_map:
                continue
            signal = signal_map[key]
            if label == "dart":
                result["has_dart"] = True
            elif label == "price_volume":
                result["has_price_volume"] = True
            elif label == "community":
                result["has_community"] = True
            strength = safe_float(signal.get("strength"), 0.0)
            result["corroboration_score"] += base_bonus + 0.08 * strength
            result["corroboration_sources"].append(label)
            titles = signal.get("titles") or []
            for title in titles[:2]:
                result["corroboration_notes"].append(f"{label}:{title}")
            break



class RuleJudgePolicy:
    """
    pr05a v3: rule 단계는 최종 뉴스 사용 여부를 확정하지 않는다.

    역할:
        - 명백한 직접 sector match만 auto_accept_rule로 둔다.
        - company_profile_dependent 및 애매한 sector 연결은 needs_llm_judge로 보낸다.
        - broad_macro는 stock-specific driver가 있을 때만 background 후보로 둔다.

    이 단계의 usable_for_generation은 "LLM judge 없이 바로 generation input으로 쓸 수 있는가"를 뜻한다.
    """

    AMBIGUOUS_SECTOR_TAGS: Set[str] = {
        "sector_consumer",
        "sector_technology",
        "sector_financial",
        "sector_shipping",
        "sector_content",
    }

    @classmethod
    def classify(
        cls,
        context: pd.Series,
        stock: StockProfile,
        decision: AttachDecision,
        corr: Dict[str, Any],
        attach_score: float,
    ) -> Dict[str, Any]:
        evidence_class = clean_text(context.get("evidence_class"))
        confidence = clean_text(context.get("confidence_level"))
        raw_count = int(safe_float(context.get("raw_count"), 0))
        matched = set(decision.matched_tags)
        has_corr = bool(corr.get("has_dart") or corr.get("has_price_volume") or corr.get("has_community"))

        # 기본값: LLM judge 필요.
        out = {
            "rule_decision": "needs_llm_judge",
            "llm_judge_required": True,
            "usable_for_generation": False,
            "allowed_usage_pre_judge": "judge_before_use",
            "usage_rule": "needs_llm_judge_before_generation",
            "relation_directness_prior": 1,
            "judge_reason": "rule_stage_does_not_confirm_stock_specific_causality",
        }

        if evidence_class == "company_profile_dependent":
            out.update({
                "rule_decision": "needs_llm_judge",
                "llm_judge_required": True,
                "usable_for_generation": False,
                "allowed_usage_pre_judge": "judge_profile_factor_context",
                "usage_rule": "needs_llm_judge_profile_factor",
                "relation_directness_prior": 1 if not has_corr else 2,
                "judge_reason": "profile_factor_context_requires_business_path_validation",
            })
            return out

        if evidence_class == "sector_linkable":
            ambiguous = bool(matched & cls.AMBIGUOUS_SECTOR_TAGS)
            strong_direct = (
                confidence in {"high", "medium"}
                and raw_count >= 3
                and attach_score >= 0.62
                and not ambiguous
            )
            very_strong_with_corr = has_corr and attach_score >= 0.70

            if strong_direct or very_strong_with_corr:
                out.update({
                    "rule_decision": "auto_accept_rule",
                    "llm_judge_required": False,
                    "usable_for_generation": True,
                    "allowed_usage_pre_judge": "supporting_context",
                    "usage_rule": "use_as_supporting_context",
                    "relation_directness_prior": 3 if has_corr else 2,
                    "judge_reason": "direct_primary_sector_match_with_sufficient_signal",
                })
                return out

            out.update({
                "rule_decision": "needs_llm_judge",
                "llm_judge_required": True,
                "usable_for_generation": False,
                "allowed_usage_pre_judge": "judge_sector_context",
                "usage_rule": "needs_llm_judge_sector_context",
                "relation_directness_prior": 1 if confidence in {"low", ""} else 2,
                "judge_reason": "sector_match_is_possible_but_business_relevance_must_be_verified",
            })
            return out

        if evidence_class == "broad_macro":
            out.update({
                "rule_decision": "background_candidate",
                "llm_judge_required": False,
                "usable_for_generation": True,
                "allowed_usage_pre_judge": "background_only",
                "usage_rule": "background_only_after_stock_specific_driver",
                "relation_directness_prior": 0,
                "judge_reason": "broad_macro_never_primary_driver",
            })
            return out

        out.update({
            "rule_decision": "reject_by_rule",
            "llm_judge_required": False,
            "usable_for_generation": False,
            "allowed_usage_pre_judge": "do_not_use",
            "usage_rule": "reject_by_rule",
            "relation_directness_prior": 0,
            "judge_reason": "unknown_evidence_class",
        })
        return out

class AttachScorer:
    @staticmethod
    def score(context: pd.Series, decision: AttachDecision, corr: Dict[str, Any]) -> Tuple[float, str, bool]:
        base = safe_float(context.get("stock_link_score"), 0.0)
        evidence_class = clean_text(context.get("evidence_class"))
        confidence = clean_text(context.get("confidence_level"))
        raw_count = int(safe_float(context.get("raw_count"), 0))

        score = base
        if decision.attach_type == "sector_context":
            score += 0.12
        elif decision.attach_type == "profile_dependent_context":
            score += 0.04

        if confidence == "high":
            score += 0.08
        elif confidence == "medium":
            score += 0.04

        if raw_count >= 10:
            score += 0.04
        elif raw_count >= 3:
            score += 0.02

        score += safe_float(corr.get("corroboration_score"), 0.0)

        has_corr = bool(corr.get("has_dart") or corr.get("has_price_volume") or corr.get("has_community"))
        usable = True
        usage_rule = "use_as_supporting_context"

        if evidence_class == "company_profile_dependent":
            if not has_corr:
                usable = False
                usage_rule = "hold_until_stock_specific_corroboration"
            else:
                usage_rule = "use_only_with_stock_specific_driver"

        if decision.needs_corroboration and not has_corr:
            # sector low는 완전 제거하지 않고 후보로 남기되 LLM direct evidence 사용은 금지
            if evidence_class == "sector_linkable":
                usage_rule = "candidate_only_needs_corroboration"
                score -= 0.10
            else:
                usable = False
                usage_rule = "hold_until_stock_specific_corroboration"

        if evidence_class == "sector_linkable" and not has_corr:
            # GDELT + stock profile만으로는 개별주 원인을 확정하지 않는다.
            # 단, high/medium sector 후보는 supporting context로는 남긴다.
            if confidence == "high":
                score = min(score, 0.78)
            elif confidence == "medium":
                score = min(score, 0.68)
            else:
                score = min(score, 0.52)
            if raw_count <= 1:
                score = min(score, 0.55)

        if evidence_class == "company_profile_dependent" and not has_corr:
            score = min(score, 0.55)

        score = max(0.0, min(1.0, score))
        return round(score, 4), usage_rule, usable


# =============================================================================
# Pipeline
# =============================================================================


@dataclass
class PipelineConfig:
    gdelt_context_csv: Path
    stock_profile_csv: Path
    output_csv: Path
    output_jsonl: Optional[Path]
    judge_input_jsonl: Optional[Path]
    report_txt: Optional[Path]
    dart_csv: Optional[Path]
    price_volume_csv: Optional[Path]
    community_csv: Optional[Path]
    start_date: Optional[str]
    end_date: Optional[str]
    target_year: Optional[int]
    max_contexts_per_stock_day: int
    include_candidate_only: bool
    include_broad_macro: bool
    max_broad_per_stock_day: int
    min_attach_score: float
    max_stocks: Optional[int]
    max_gdelt_rows: Optional[int]


class GdeltStockContextAttachPipeline:
    def __init__(self, config: PipelineConfig):
        self.config = config

    def run(self) -> None:
        print("=" * 100)
        print("[pr05a GDELT context stock attach 시작]")
        print(f"gdelt_context_csv: {self.config.gdelt_context_csv}")
        print(f"stock_profile_csv: {self.config.stock_profile_csv}")
        print(f"output_csv: {self.config.output_csv}")
        print("=" * 100)

        gdelt = GdeltContextLoader.load(
            self.config.gdelt_context_csv,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
        )
        if self.config.max_gdelt_rows is not None:
            gdelt = gdelt.head(self.config.max_gdelt_rows).copy()

        profiles = StockProfileLoader.load(
            self.config.stock_profile_csv,
            target_year=self.config.target_year,
        )
        if self.config.max_stocks is not None:
            profiles = profiles[: self.config.max_stocks]

        stock_name_to_code = {p.stock_name: p.stock_code for p in profiles}
        corr_index = CorroborationIndex(
            dart=OptionalSignalLoader.load_signal_map(self.config.dart_csv, "dart", 0.6),
            price=OptionalSignalLoader.load_signal_map(self.config.price_volume_csv, "price_volume", 0.7),
            community=OptionalSignalLoader.load_signal_map(self.config.community_csv, "community", 0.5),
            stock_name_to_code=stock_name_to_code,
        )

        attached = self._attach_contexts(gdelt, profiles, corr_index)
        attached = self._attach_broad_macro(gdelt, attached) if self.config.include_broad_macro else attached
        selected = self._select_top_contexts(attached)

        self._write_outputs(selected, attached, gdelt, profiles)

    def _attach_contexts(
        self,
        gdelt: pd.DataFrame,
        profiles: List[StockProfile],
        corr_index: CorroborationIndex,
    ) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []
        reject_counter = Counter()

        stock_by_tag: Dict[str, List[StockProfile]] = defaultdict(list)
        for profile in profiles:
            for tag in profile.sensitivity_tags:
                stock_by_tag[tag].append(profile)

        direct_candidates = gdelt[gdelt["evidence_class"].isin(["sector_linkable", "company_profile_dependent"])].copy()

        for _, context in direct_candidates.iterrows():
            evidence_class = clean_text(context.get("evidence_class"))
            if evidence_class == "sector_linkable":
                candidate_tags = set(context.get("_hard_tags", set()))
            else:
                candidate_tags = set(context.get("_profile_factor_tags", set()))

            candidate_stocks: Dict[str, StockProfile] = {}
            for tag in candidate_tags:
                for profile in stock_by_tag.get(tag, []):
                    candidate_stocks[profile.stock_code] = profile

            if not candidate_stocks:
                reject_counter["no_candidate_stock_for_tags"] += 1
                continue

            for stock in candidate_stocks.values():
                decision = ContextAttachPolicy.decide(context, stock)
                if not decision.allowed:
                    reject_counter[decision.gate_reason] += 1
                    continue

                corr = corr_index.lookup(clean_text(context.get("date")), stock)
                attach_score, usage_rule, usable = AttachScorer.score(context, decision, corr)
                judge_info = RuleJudgePolicy.classify(context, stock, decision, corr, attach_score)
                usage_rule = clean_text(judge_info.get("usage_rule")) or usage_rule
                usable = bool(judge_info.get("usable_for_generation"))

                if attach_score < self.config.min_attach_score:
                    reject_counter["below_min_attach_score"] += 1
                    continue

                # v3에서는 LLM judge 대상 후보도 출력하는 것이 기본 목적이다.
                # include_candidate_only=False이면 auto_accept_rule/background만 남긴다.
                if (not usable) and (not self.config.include_candidate_only):
                    reject_counter["candidate_requires_llm_judge_hidden"] += 1
                    continue

                rows.append(self._build_row(context, stock, decision, corr, attach_score, usage_rule, usable, judge_info))

        out = pd.DataFrame(rows)
        if out.empty:
            out = self._empty_output_df()
        out.attrs["reject_counter"] = reject_counter
        return out

    def _build_row(
        self,
        context: pd.Series,
        stock: StockProfile,
        decision: AttachDecision,
        corr: Dict[str, Any],
        attach_score: float,
        usage_rule: str,
        usable: bool,
        judge_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        judge_info = judge_info or {}
        return {
            "date": clean_text(context.get("date")),
            "stock_code": stock.stock_code,
            "stock_name": stock.stock_name,
            "business_year": stock.business_year,
            "theme": clean_text(context.get("theme")),
            "evidence_class": clean_text(context.get("evidence_class")),
            "attach_type": decision.attach_type,
            "attach_score": attach_score,
            "usable_for_generation": bool(usable),
            "usage_rule": usage_rule,
            "rule_decision": clean_text(judge_info.get("rule_decision")),
            "llm_judge_required": bool(judge_info.get("llm_judge_required", False)),
            "allowed_usage_pre_judge": clean_text(judge_info.get("allowed_usage_pre_judge")),
            "relation_directness_prior": int(safe_float(judge_info.get("relation_directness_prior"), 0)),
            "judge_reason": clean_text(judge_info.get("judge_reason")),
            "gate_reason": decision.gate_reason,
            "matched_stock_tags": join_tags(decision.matched_tags),
            "stock_sensitivity_tags": join_tags(stock.sensitivity_tags, max_items=30),
            "stock_sector_tags": join_tags(stock.sector_tags, max_items=20),
            "stock_profile_factor_tags": join_tags(stock.profile_factor_tags, max_items=20),
            "business_profile_excerpt": stock.profile_text[:700],
            "context_confidence_level": clean_text(context.get("confidence_level")),
            "context_stock_link_score": round(safe_float(context.get("stock_link_score"), 0.0), 4),
            "context_specificity_score": round(safe_float(context.get("context_specificity_score", context.get("specificity_score")), 0.0), 4),
            "gdelt_signal_strength": round(safe_float(context.get("gdelt_signal_strength"), 0.0), 4),
            "raw_count": int(safe_float(context.get("raw_count"), 0)),
            "weighted_count": round(safe_float(context.get("weighted_count"), 0.0), 4),
            "avg_tone": context.get("avg_tone", ""),
            "source": clean_text(context.get("source")),
            "hard_match_tags": clean_text(context.get("hard_match_tags")),
            "profile_factor_tags": clean_text(context.get("profile_factor_tags")),
            "modifier_tags": clean_text(context.get("modifier_tags")),
            "macro_tags": clean_text(context.get("macro_tags")),
            "matched_tokens": clean_text(context.get("matched_tokens")),
            "reason_code": clean_text(context.get("reason_code")),
            "requires_corroboration": clean_text(context.get("requires_corroboration")),
            "has_dart": bool(corr.get("has_dart")),
            "has_price_volume": bool(corr.get("has_price_volume")),
            "has_community": bool(corr.get("has_community")),
            "corroboration_score": round(safe_float(corr.get("corroboration_score"), 0.0), 4),
            "corroboration_sources": join_tags(corr.get("corroboration_sources", [])),
            "corroboration_notes": " || ".join(corr.get("corroboration_notes", [])),
        }

    def _attach_broad_macro(self, gdelt: pd.DataFrame, attached: pd.DataFrame) -> pd.DataFrame:
        if attached.empty:
            return attached

        usable_driver = attached[
            attached["usable_for_generation"].astype(bool)
            & attached["evidence_class"].isin(["sector_linkable", "company_profile_dependent"])
        ][["date", "stock_code", "stock_name", "business_year", "stock_sensitivity_tags"]].drop_duplicates()

        if usable_driver.empty:
            return attached

        broad = gdelt[gdelt["evidence_class"].eq("broad_macro")].copy()
        if broad.empty:
            return attached

        broad = broad.sort_values(["date", "stock_link_score", "raw_count"], ascending=[True, False, False])
        broad = broad.groupby("date", as_index=False, group_keys=False).head(self.config.max_broad_per_stock_day)

        rows: List[Dict[str, Any]] = []
        for _, driver in usable_driver.iterrows():
            day_broad = broad[broad["date"].eq(driver["date"])]
            for _, context in day_broad.iterrows():
                rows.append({
                    "date": driver["date"],
                    "stock_code": driver["stock_code"],
                    "stock_name": driver["stock_name"],
                    "business_year": driver["business_year"],
                    "theme": clean_text(context.get("theme")),
                    "evidence_class": "broad_macro",
                    "attach_type": "background_context",
                    "attach_score": round(min(0.45, safe_float(context.get("stock_link_score"), 0.0)), 4),
                    "usable_for_generation": True,
                    "usage_rule": "background_only_after_stock_specific_driver",
                    "rule_decision": "background_candidate",
                    "llm_judge_required": False,
                    "allowed_usage_pre_judge": "background_only",
                    "relation_directness_prior": 0,
                    "judge_reason": "broad_macro_never_primary_driver",
                    "gate_reason": "broad_macro_attached_after_driver",
                    "matched_stock_tags": "",
                    "stock_sensitivity_tags": driver["stock_sensitivity_tags"],
                    "stock_sector_tags": "",
                    "stock_profile_factor_tags": "",
                    "business_profile_excerpt": "",
                    "context_confidence_level": clean_text(context.get("confidence_level")),
                    "context_stock_link_score": round(safe_float(context.get("stock_link_score"), 0.0), 4),
                    "context_specificity_score": round(safe_float(context.get("context_specificity_score", context.get("specificity_score")), 0.0), 4),
                    "gdelt_signal_strength": round(safe_float(context.get("gdelt_signal_strength"), 0.0), 4),
                    "raw_count": int(safe_float(context.get("raw_count"), 0)),
                    "weighted_count": round(safe_float(context.get("weighted_count"), 0.0), 4),
                    "avg_tone": context.get("avg_tone", ""),
                    "source": clean_text(context.get("source")),
                    "hard_match_tags": clean_text(context.get("hard_match_tags")),
                    "profile_factor_tags": clean_text(context.get("profile_factor_tags")),
                    "modifier_tags": clean_text(context.get("modifier_tags")),
                    "macro_tags": clean_text(context.get("macro_tags")),
                    "matched_tokens": clean_text(context.get("matched_tokens")),
                    "reason_code": clean_text(context.get("reason_code")),
                    "requires_corroboration": clean_text(context.get("requires_corroboration")),
                    "has_dart": False,
                    "has_price_volume": False,
                    "has_community": False,
                    "corroboration_score": 0.0,
                    "corroboration_sources": "",
                    "corroboration_notes": "",
                })

        if not rows:
            return attached
        return pd.concat([attached, pd.DataFrame(rows)], ignore_index=True)

    def _select_top_contexts(self, attached: pd.DataFrame) -> pd.DataFrame:
        if attached.empty:
            return attached

        df = attached.copy()
        df["_usable_rank"] = np.where(df["usable_for_generation"].astype(bool), 0, 1)
        df["_class_rank"] = df["evidence_class"].map({
            "sector_linkable": 0,
            "company_profile_dependent": 1,
            "broad_macro": 2,
        }).fillna(9)
        df["_rule_rank"] = df["rule_decision"].map({
            "auto_accept_rule": 0,
            "needs_llm_judge": 1,
            "background_candidate": 2,
            "reject_by_rule": 9,
        }).fillna(5)

        df = df.sort_values(
            ["date", "stock_code", "theme", "_rule_rank", "_usable_rank", "attach_score", "_class_rank", "raw_count"],
            ascending=[True, True, True, True, True, False, True, False],
        )

        # 같은 stock-date-theme가 여러 GDELT record 조합으로 중복 생성되는 것을 줄인다.
        df = (
            df.groupby(["date", "stock_code", "theme", "evidence_class"], as_index=False, group_keys=False)
            .head(1)
            .copy()
        )

        df = df.sort_values(
            ["date", "stock_code", "_rule_rank", "_usable_rank", "attach_score", "_class_rank", "raw_count"],
            ascending=[True, True, True, True, False, True, False],
        )

        selected = (
            df.groupby(["date", "stock_code"], as_index=False, group_keys=False)
            .head(self.config.max_contexts_per_stock_day)
            .copy()
        )

        selected = selected.drop(columns=["_usable_rank", "_class_rank", "_rule_rank"], errors="ignore")
        selected = selected.sort_values(["date", "stock_code", "attach_score"], ascending=[True, True, False])
        return selected.reset_index(drop=True)

    def _write_outputs(
        self,
        selected: pd.DataFrame,
        attached_all: pd.DataFrame,
        gdelt: pd.DataFrame,
        profiles: List[StockProfile],
    ) -> None:
        self.config.output_csv.parent.mkdir(parents=True, exist_ok=True)
        selected.to_csv(self.config.output_csv, index=False, encoding="utf-8-sig")

        if self.config.output_jsonl is not None:
            self.config.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
            with self.config.output_jsonl.open("w", encoding="utf-8") as f:
                for _, row in selected.iterrows():
                    f.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")

        if self.config.judge_input_jsonl is not None:
            self.config.judge_input_jsonl.parent.mkdir(parents=True, exist_ok=True)
            judge_rows = selected[selected.get("llm_judge_required", False).astype(bool)].copy()
            with self.config.judge_input_jsonl.open("w", encoding="utf-8") as f:
                for _, row in judge_rows.iterrows():
                    payload = self._build_judge_payload(row)
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        report = self._build_report(selected, attached_all, gdelt, profiles)
        if self.config.report_txt is not None:
            self.config.report_txt.parent.mkdir(parents=True, exist_ok=True)
            self.config.report_txt.write_text(report, encoding="utf-8")

        print(report)


    @staticmethod
    def _build_judge_payload(row: pd.Series) -> Dict[str, Any]:
        return {
            "custom_id": f"gdelt_judge__{clean_text(row.get('date'))}__{clean_text(row.get('stock_code'))}__{abs(hash((clean_text(row.get('theme')), clean_text(row.get('evidence_class')), clean_text(row.get('source'))))) % 10**10}",
            "task": "judge_stock_context_relevance",
            "stock": {
                "stock_code": clean_text(row.get("stock_code")),
                "stock_name": clean_text(row.get("stock_name")),
                "business_year": clean_text(row.get("business_year")),
                "business_profile_excerpt": clean_text(row.get("business_profile_excerpt")),
                "sector_tags": parse_tags(row.get("stock_sector_tags")),
                "profile_factor_tags": parse_tags(row.get("stock_profile_factor_tags")),
                "sensitivity_tags": parse_tags(row.get("stock_sensitivity_tags")),
            },
            "context": {
                "date": clean_text(row.get("date")),
                "theme": clean_text(row.get("theme")),
                "evidence_class": clean_text(row.get("evidence_class")),
                "source": clean_text(row.get("source")),
                "confidence_level": clean_text(row.get("context_confidence_level")),
                "stock_link_score": safe_float(row.get("context_stock_link_score"), 0.0),
                "raw_count": int(safe_float(row.get("raw_count"), 0)),
                "hard_match_tags": parse_tags(row.get("hard_match_tags")),
                "profile_factor_tags": parse_tags(row.get("profile_factor_tags")),
                "modifier_tags": parse_tags(row.get("modifier_tags")),
                "matched_tokens": parse_tags(row.get("matched_tokens")),
                "reason_code": clean_text(row.get("reason_code")),
            },
            "rule_prior": {
                "attach_score": safe_float(row.get("attach_score"), 0.0),
                "matched_stock_tags": parse_tags(row.get("matched_stock_tags")),
                "gate_reason": clean_text(row.get("gate_reason")),
                "relation_directness_prior": int(safe_float(row.get("relation_directness_prior"), 0)),
                "judge_reason": clean_text(row.get("judge_reason")),
                "corroboration_sources": parse_tags(row.get("corroboration_sources")),
            },
            "required_output_schema": {
                "decision": "accept_direct | accept_indirect | background_only | reject",
                "directness": "integer 0-3",
                "confidence": "float 0-1",
                "allowed_usage": "primary_driver | supporting_context | background_only | do_not_use",
                "requires_corroboration": "boolean",
                "better_factor_tags": "list[str]",
                "reason_ko": "short Korean reason, no invented facts",
            },
        }

    def _build_report(
        self,
        selected: pd.DataFrame,
        attached_all: pd.DataFrame,
        gdelt: pd.DataFrame,
        profiles: List[StockProfile],
    ) -> str:
        lines: List[str] = []
        lines.append("=" * 100)
        lines.append("[pr05a GDELT context stock attach 완료]")
        lines.append(f"gdelt_rows: {len(gdelt):,}")
        lines.append(f"stock_profiles: {len(profiles):,}")
        lines.append(f"attached_all_rows: {len(attached_all):,}")
        lines.append(f"selected_rows: {len(selected):,}")
        lines.append(f"output_csv: {self.config.output_csv}")
        if self.config.output_jsonl:
            lines.append(f"output_jsonl: {self.config.output_jsonl}")
        if self.config.report_txt:
            lines.append(f"report_txt: {self.config.report_txt}")

        reject_counter = attached_all.attrs.get("reject_counter", Counter())
        if reject_counter:
            lines.append("\n[reject_counter]")
            for k, v in reject_counter.most_common(30):
                lines.append(f"- {k}: {v}")

        if selected.empty:
            lines.append("\n[WARN] selected output is empty")
            lines.append("=" * 100)
            return "\n".join(lines)

        lines.append("\n[evidence_class]")
        lines.append(selected["evidence_class"].value_counts(dropna=False).to_string())

        lines.append("\n[usage_rule]")
        lines.append(selected["usage_rule"].value_counts(dropna=False).head(20).to_string())

        lines.append("\n[usable_for_generation]")
        lines.append(selected["usable_for_generation"].value_counts(dropna=False).to_string())

        lines.append("\n[attach_score describe]")
        lines.append(str(selected["attach_score"].describe().to_dict()))

        lines.append("\n[corroboration source counts]")
        if "corroboration_sources" in selected.columns:
            exploded = []
            for value in selected["corroboration_sources"].fillna(""):
                exploded.extend(parse_tags(value))
            lines.append(str(Counter(exploded).most_common(20)))

        lines.append("\n[preview]")
        preview_cols = [
            "date", "stock_code", "stock_name", "theme", "evidence_class", "attach_score",
            "usable_for_generation", "usage_rule", "rule_decision", "llm_judge_required", "matched_stock_tags", "corroboration_sources",
        ]
        lines.append(selected[preview_cols].head(40).to_string(index=False))
        lines.append("=" * 100)
        return "\n".join(lines)

    @staticmethod
    def _empty_output_df() -> pd.DataFrame:
        cols = [
            "date", "stock_code", "stock_name", "business_year", "theme", "evidence_class",
            "attach_type", "attach_score", "usable_for_generation", "usage_rule",
            "rule_decision", "llm_judge_required", "allowed_usage_pre_judge",
            "relation_directness_prior", "judge_reason", "gate_reason",
            "matched_stock_tags", "stock_sensitivity_tags", "stock_sector_tags",
            "stock_profile_factor_tags", "business_profile_excerpt", "context_confidence_level",
            "context_stock_link_score", "context_specificity_score", "gdelt_signal_strength",
            "raw_count", "weighted_count", "avg_tone", "source", "hard_match_tags",
            "profile_factor_tags", "modifier_tags", "macro_tags", "matched_tokens", "reason_code",
            "requires_corroboration", "has_dart", "has_price_volume", "has_community",
            "corroboration_score", "corroboration_sources", "corroboration_notes",
        ]
        return pd.DataFrame(columns=cols)


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gdelt-context-csv", type=str, required=True)
    parser.add_argument("--stock-profile-csv", type=str, required=True)
    parser.add_argument("--output-csv", type=str, required=True)
    parser.add_argument("--output-jsonl", type=str, default=None)
    parser.add_argument("--judge-input-jsonl", type=str, default=None)
    parser.add_argument("--report-txt", type=str, default=None)

    parser.add_argument("--dart-csv", type=str, default=None)
    parser.add_argument("--price-volume-csv", type=str, default=None)
    parser.add_argument("--community-csv", type=str, default=None)

    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--target-year", type=int, default=None)

    parser.add_argument("--max-contexts-per-stock-day", type=int, default=5)
    parser.add_argument("--include-candidate-only", action="store_true")
    parser.add_argument("--no-broad-macro", action="store_true")
    parser.add_argument("--max-broad-per-stock-day", type=int, default=1)
    parser.add_argument("--min-attach-score", type=float, default=0.35)

    parser.add_argument("--max-stocks", type=int, default=None)
    parser.add_argument("--max-gdelt-rows", type=int, default=None)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    output_csv = Path(args.output_csv).expanduser().resolve()
    output_jsonl = Path(args.output_jsonl).expanduser().resolve() if args.output_jsonl else output_csv.with_suffix(".jsonl")
    judge_input_jsonl = Path(args.judge_input_jsonl).expanduser().resolve() if args.judge_input_jsonl else output_csv.with_name(output_csv.stem + "_judge_input.jsonl")
    report_txt = Path(args.report_txt).expanduser().resolve() if args.report_txt else output_csv.with_name(output_csv.stem + "_report.txt")

    config = PipelineConfig(
        gdelt_context_csv=Path(args.gdelt_context_csv).expanduser().resolve(),
        stock_profile_csv=Path(args.stock_profile_csv).expanduser().resolve(),
        output_csv=output_csv,
        output_jsonl=output_jsonl,
        judge_input_jsonl=judge_input_jsonl,
        report_txt=report_txt,
        dart_csv=Path(args.dart_csv).expanduser().resolve() if args.dart_csv else None,
        price_volume_csv=Path(args.price_volume_csv).expanduser().resolve() if args.price_volume_csv else None,
        community_csv=Path(args.community_csv).expanduser().resolve() if args.community_csv else None,
        start_date=args.start_date,
        end_date=args.end_date,
        target_year=args.target_year,
        max_contexts_per_stock_day=args.max_contexts_per_stock_day,
        include_candidate_only=bool(args.include_candidate_only),
        include_broad_macro=not bool(args.no_broad_macro),
        max_broad_per_stock_day=args.max_broad_per_stock_day,
        min_attach_score=args.min_attach_score,
        max_stocks=args.max_stocks,
        max_gdelt_rows=args.max_gdelt_rows,
    )

    pipeline = GdeltStockContextAttachPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
