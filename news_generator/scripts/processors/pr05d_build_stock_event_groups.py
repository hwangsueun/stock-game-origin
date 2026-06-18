#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pr05d_build_stock_event_groups.py

Build stock-level event groups before evidence-bundle construction.

This script is intentionally NOT a distance-based clustering model.
It performs rule-based event deduplication / grouping:

    source-level evidence rows
    -> same real-world stock event group
    -> later evidence bundle builder
    -> later bundle-level LLM judge
    -> deterministic permission merge
    -> constrained news generation

Core principles preserved here:
1. DART is official topic evidence and has priority as primary_topic_source.
2. DART existence does NOT imply market causality.
3. stock_event can be a topic anchor only when final_can_be_news_trigger=True.
4. stock_event alone never permits source-level market-cause claims.
5. price-volume is reaction evidence only and never creates a causal event group.
6. GDELT is supporting/background context only at this stage.
7. macro is exposure/background context only at this stage.
8. community/forum/board data is not used.

Expected inputs:
- stock_event_context_annotations.csv from pr05c
- optional DART evidence detail CSV
- optional price-volume evidence CSV/JSONL/PARQUET
- optional GDELT evidence CSV/JSONL/PARQUET
- optional macro evidence CSV/JSONL/PARQUET

Outputs:
- stock_event_groups.jsonl
- stock_event_groups.csv
- stock_event_group_report.md
- unexplained_price_moves.csv

Recommended next stage:
- pr05e_build_stock_evidence_bundles.py
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


# =============================================================================
# Default paths
# =============================================================================

PROJECT_ROOT = Path("/Users/hgs/Desktop/IISE CD")

DEFAULT_STOCK_EVENT = (
    PROJECT_ROOT / "data/interim/pr05c_stock_event_context/stock_event_context_annotations.csv"
)

DEFAULT_DART = (
    PROJECT_ROOT / "npc_generator/data/processed/dart_event_evidence/dart_event_evidence_detail.csv"
)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data/interim/pr05d_stock_event_groups"


# =============================================================================
# Utilities
# =============================================================================

NULL_STRINGS = {"", "none", "null", "nan", "na", "n/a", "<na>"}


def none_path(s: Optional[str]) -> Optional[Path]:
    if s is None:
        return None
    t = str(s).strip()
    if t.lower() in NULL_STRINGS:
        return None
    return Path(t).expanduser()


def first_col(cols: Iterable[str], candidates: Sequence[str]) -> Optional[str]:
    colset = set(cols)
    for c in candidates:
        if c in colset:
            return c
    return None


def is_null(x: Any) -> bool:
    if x is None:
        return True
    try:
        if pd.isna(x):
            return True
    except Exception:
        return False
    if isinstance(x, str) and x.strip().lower() in NULL_STRINGS:
        return True
    return False


def clean_str(x: Any, limit: int = 700) -> str:
    if is_null(x):
        return ""
    s = re.sub(r"\s+", " ", str(x)).strip()
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def parse_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if is_null(x):
        return default
    if isinstance(x, (int, float)):
        if isinstance(x, float) and math.isnan(x):
            return default
        return bool(int(x))
    s = str(x).strip().lower()
    if s in {"true", "t", "yes", "y", "1", "1.0"}:
        return True
    if s in {"false", "f", "no", "n", "0", "0.0"}:
        return False
    return default


def normalize_stock_code(x: Any) -> str:
    if is_null(x):
        return ""
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    s = str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"\D+", "", s)
    if not s:
        return ""
    if len(s) <= 6:
        return s.zfill(6)
    return s


def to_date_value(x: Any) -> Optional[pd.Timestamp]:
    if is_null(x):
        return None
    dt = pd.to_datetime(x, errors="coerce")
    if pd.isna(dt):
        return None
    return pd.Timestamp(dt).normalize()


def to_date_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def days_between(a: Optional[pd.Timestamp], b: Optional[pd.Timestamp]) -> int:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return 999999
    return abs((pd.Timestamp(a).normalize() - pd.Timestamp(b).normalize()).days)


def safe_float(x: Any) -> Optional[float]:
    if is_null(x):
        return None
    try:
        v = float(str(x).replace(",", "").replace("%", ""))
    except Exception:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def jsonable(x: Any) -> Any:
    if is_null(x):
        return None
    if isinstance(x, pd.Timestamp):
        return x.strftime("%Y-%m-%d")
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def jdumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=jsonable)


def read_table(path: Optional[Path]) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame()
    if not path.exists():
        return pd.DataFrame()

    suffix = path.suffix.lower()
    if suffix == ".csv":
        for enc in ["utf-8-sig", "utf-8", "cp949", "euc-kr"]:
            try:
                return pd.read_csv(path, encoding=enc)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(path)

    if suffix == ".jsonl":
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return pd.DataFrame(rows)

    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return pd.DataFrame(payload)
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                return pd.DataFrame(payload["data"])
            return pd.DataFrame([payload])
        return pd.DataFrame()

    if suffix == ".parquet":
        return pd.read_parquet(path)

    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)

    raise ValueError(f"Unsupported file type: {path}")


# =============================================================================
# Text normalization and event-family classification
# =============================================================================

class TextNormalizer:
    STOPWORDS = {
        "관련", "공시", "보고서", "기재정정", "정정", "안내", "주식", "회사", "기업",
        "대한", "따른", "이번", "해당", "전반", "기준", "발표", "결정", "개최",
        "시장", "전망", "예상", "기대", "영향", "확대", "감소", "증가", "개선", "악화",
        "및", "또는", "에서", "으로", "에게", "보다", "까지", "부터", "하는", "했다", "있다",
        "the", "and", "for", "with", "from", "inc", "ltd", "co", "corp",
    }

    @staticmethod
    def normalize(text: str) -> str:
        s = clean_str(text, limit=2000).lower()
        s = s.replace("ㆍ", "·")
        s = re.sub(r"[\[\](){}<>『』「」'\"`.,:;!?/\\|_+=~^*#@]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @classmethod
    def tokens(cls, text: str, stock_name: str = "") -> set[str]:
        s = cls.normalize(text)
        stock_name_norm = cls.normalize(stock_name)
        raw = re.findall(r"[가-힣A-Za-z0-9%]+", s)
        out: set[str] = set()
        for tok in raw:
            tok = tok.strip().lower()
            if not tok or len(tok) <= 1:
                continue
            if tok in cls.STOPWORDS:
                continue
            if stock_name_norm and tok == stock_name_norm:
                continue
            out.add(tok)
        return out

    @staticmethod
    def quarter_terms(text: str) -> set[str]:
        s = clean_str(text)
        terms: set[str] = set()
        patterns = [
            (r"1\s*분기|1q|q1|1/4", "Q1"),
            (r"2\s*분기|2q|q2|2/4", "Q2"),
            (r"3\s*분기|3q|q3|3/4", "Q3"),
            (r"4\s*분기|4q|q4|4/4", "Q4"),
            (r"상반기|반기", "H1"),
            (r"하반기", "H2"),
            (r"연간|사업연도|결산", "FY"),
        ]
        low = s.lower()
        for pat, label in patterns:
            if re.search(pat, low):
                terms.add(label)
        return terms

    @staticmethod
    def year_terms(text: str) -> set[str]:
        s = clean_str(text)
        years = set(re.findall(r"20\d{2}", s))
        yy = re.findall(r"(?<!\d)(\d{2})년", s)
        for y in yy:
            val = int(y)
            if 10 <= val <= 30:
                years.add(f"20{y}")
        return years


class EventFamilyClassifier:
    """Rule-based source family classifier.

    The output is used for blocking/deduplication only. It is not a causal claim.
    """

    FAMILY_ORDER = [
        "earnings",
        "guidance",
        "dividend",
        "contract",
        "investment",
        "legal_regulatory",
        "management_governance",
        "product_supply_chain",
        "sector_theme",
        "macro_exposure",
        "other_company_event",
        "unknown",
    ]

    DIVIDEND_TERMS = ["배당", "현금배당", "현물배당", "중간배당", "분기배당"]
    GUIDANCE_TERMS = [
        "영업실적등에대한전망", "영업실적 등에 대한 전망", "실적전망", "매출액전망",
        "손익전망", "전망공시", "가이던스", "guidance",
    ]
    TRUE_EARNINGS_TERMS = [
        "매출액또는손익구조", "매출액 또는 손익구조", "잠정실적", "영업실적",
        "결산실적", "실적발표", "실적 공시", "분기보고서", "반기보고서",
        "사업보고서", "영업이익", "순이익", "매출액", "적자", "흑자",
    ]
    CONTRACT_TERMS = ["공급계약", "수주", "계약체결", "단일판매", "판매·공급", "판매공급", "납품"]
    INVESTMENT_TERMS = ["시설투자", "신규투자", "투자결정", "증설", "공장", "라인", "capex", "인수", "합병", "m&a"]
    LEGAL_TERMS = ["소송", "판결", "제재", "과징금", "조사", "검찰", "공정위", "금감원", "분쟁", "손해배상"]
    MANAGEMENT_TERMS = ["대표이사", "임원", "최대주주", "지분", "경영권", "이사회", "주주총회", "합병", "분할"]
    PRODUCT_TERMS = ["신제품", "출시", "제품", "생산", "공급망", "원재료", "반도체", "배터리", "d램", "dram", "oled", "전기차"]
    MACRO_TERMS = ["금리", "환율", "유가", "물가", "수출", "수입", "중국", "미국", "정책", "규제", "부양책"]
    SECTOR_TERMS = ["업황", "업종", "섹터", "테마", "산업", "수혜", "반도체", "자동차", "건설", "바이오"]

    @classmethod
    def classify_stock_event(cls, row: pd.Series) -> str:
        explicit = clean_str(row.get("dart_mapping_family"))
        if explicit:
            mapped = cls._map_explicit_family(explicit)
            if mapped != "unknown":
                return mapped

        allowed = clean_str(row.get("final_allowed_usage"))
        klass = clean_str(row.get("stock_event_class"))
        interp = clean_str(row.get("final_event_interpretation")) or clean_str(row.get("llm_event_interpretation"))
        event_type = clean_str(row.get("event_type"))
        text = " ".join([
            clean_str(row.get("title")),
            clean_str(row.get("description")),
            clean_str(row.get("sector")),
            allowed,
            klass,
            interp,
            event_type,
        ])

        if "macro" in allowed or "macro" in klass:
            return "macro_exposure"
        if "sector" in allowed or "sector" in klass:
            return "sector_theme"
        if "peer" in klass or "group" in klass:
            if "earnings" in klass or "earnings" in interp:
                return "earnings"
            return "other_company_event"
        if "earnings" in allowed or "earnings" in klass or "earnings" in interp:
            if cls._contains_any(text, cls.GUIDANCE_TERMS) or "guidance" in interp:
                return "guidance"
            return "earnings"

        return cls.classify_text(text)

    @classmethod
    def classify_dart(cls, row: pd.Series) -> str:
        group = clean_str(row.get("dart_event_group"))
        text = " ".join([
            clean_str(row.get("report_name_clean")),
            clean_str(row.get("report_name")),
            group,
        ])
        mapped = cls._map_explicit_family(group)
        if mapped != "unknown":
            return mapped
        return cls.classify_text(text)

    @classmethod
    def classify_text(cls, text: str) -> str:
        s = TextNormalizer.normalize(text)

        # Priority matters. Dividend/guidance should not be swallowed by generic 실적.
        if cls._contains_any(s, cls.DIVIDEND_TERMS):
            return "dividend"
        if cls._contains_any(s, cls.GUIDANCE_TERMS):
            return "guidance"
        if cls._contains_any(s, cls.CONTRACT_TERMS):
            return "contract"
        if cls._contains_any(s, cls.LEGAL_TERMS):
            return "legal_regulatory"
        if cls._contains_any(s, cls.INVESTMENT_TERMS):
            return "investment"
        if cls._contains_any(s, cls.MANAGEMENT_TERMS):
            return "management_governance"
        if cls._contains_any(s, cls.TRUE_EARNINGS_TERMS):
            return "earnings"
        if cls._contains_any(s, cls.MACRO_TERMS):
            return "macro_exposure"
        if cls._contains_any(s, cls.SECTOR_TERMS):
            return "sector_theme"
        if cls._contains_any(s, cls.PRODUCT_TERMS):
            return "product_supply_chain"
        return "other_company_event"

    @staticmethod
    def _contains_any(text: str, terms: Sequence[str]) -> bool:
        return any(t.lower() in text.lower() for t in terms)

    @staticmethod
    def _map_explicit_family(value: str) -> str:
        s = value.lower().strip()
        if "dividend" in s or "배당" in s:
            return "dividend"
        if "guidance" in s or "전망" in s:
            return "guidance"
        if "earning" in s or "실적" in s or "손익" in s:
            return "earnings"
        if "sector" in s or "theme" in s or "업종" in s or "테마" in s:
            return "sector_theme"
        if "macro" in s or "policy" in s or "exposure" in s:
            return "macro_exposure"
        if "contract" in s or "계약" in s or "수주" in s:
            return "contract"
        return "unknown"


class FamilyCompatibility:
    COMPATIBLE = {
        ("earnings", "guidance"),
        ("guidance", "earnings"),
        ("product_supply_chain", "sector_theme"),
        ("sector_theme", "product_supply_chain"),
        ("management_governance", "other_company_event"),
        ("other_company_event", "management_governance"),
    }

    WEAK_FAMILIES = {"sector_theme", "macro_exposure", "unknown"}

    @classmethod
    def compatible(cls, a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        return (a, b) in cls.COMPATIBLE

    @classmethod
    def merge_requires_text_overlap(cls, family_a: str, family_b: str) -> bool:
        if family_a in cls.WEAK_FAMILIES or family_b in cls.WEAK_FAMILIES:
            return True
        if family_a != family_b and (family_a, family_b) not in cls.COMPATIBLE:
            return True
        return False


# =============================================================================
# Data objects
# =============================================================================

@dataclass(frozen=True)
class EventGroupConfig:
    stock_event_path: Path
    dart_path: Optional[Path]
    price_volume_path: Optional[Path]
    gdelt_path: Optional[Path]
    macro_path: Optional[Path]
    output_dir: Path

    dart_stock_event_window_days: int = 5
    same_source_window_days: int = 3
    context_window_days: int = 14
    price_window_days: int = 3
    gdelt_window_days: int = 7
    macro_window_days: int = 7

    min_topic_jaccard: float = 0.12
    min_shared_topic_tokens: int = 1
    strong_abs_return_pct: float = 3.0
    strong_volume_ratio: float = 2.0
    max_items_per_type: int = 12


@dataclass
class EvidenceItem:
    source_type: str
    evidence_id: str
    stock_code: str
    stock_name: str
    event_date: pd.Timestamp
    event_family: str
    title: str
    description: str
    expected_direction: str
    role_hint: str
    priority: int
    materiality_score: float
    raw: Dict[str, Any]

    @property
    def topic_text(self) -> str:
        return " ".join([self.title, self.description])

    def to_compact_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "evidence_id": self.evidence_id,
            "event_date": jsonable(self.event_date),
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "event_family": self.event_family,
            "title": clean_str(self.title, 250),
            "description": clean_str(self.description, 450),
            "expected_direction": self.expected_direction or "unknown",
            "role_hint": self.role_hint,
            "materiality_score": self.materiality_score,
        }


# =============================================================================
# Loaders
# =============================================================================

class StockEventLoader:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> pd.DataFrame:
        df = read_table(self.path)
        if df.empty:
            return df

        required = ["event_date", "stock_code", "stock_name", "title"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"stock_event annotations missing required columns: {missing}")

        df = df.copy()
        df["stock_code"] = df["stock_code"].map(normalize_stock_code)
        df["event_date"] = to_date_series(df["event_date"])
        df = df[df["stock_code"].ne("") & df["event_date"].notna()].copy()

        if "evidence_id" not in df.columns:
            df["evidence_id"] = [f"STOCK_EVENT_{i+1:06d}" for i in range(len(df))]

        if "final_can_be_news_trigger" in df.columns:
            df["final_can_be_news_trigger"] = df["final_can_be_news_trigger"].map(parse_bool)
        else:
            df["final_can_be_news_trigger"] = False

        df["event_family"] = df.apply(EventFamilyClassifier.classify_stock_event, axis=1)
        df["expected_direction_norm"] = df.apply(self._direction_from_row, axis=1)
        return df

    @staticmethod
    def _direction_from_row(row: pd.Series) -> str:
        for col in ["directionality_default", "original_direction", "direction", "directionality_source"]:
            s = clean_str(row.get(col)).lower()
            if s in {"positive", "pos", "bullish", "up", "상승", "호재"}:
                return "positive"
            if s in {"negative", "neg", "bearish", "down", "하락", "악재"}:
                return "negative"
            if s in {"neutral", "mixed", "flat", "중립", "혼조"}:
                return "neutral"
        return "unknown"

    @staticmethod
    def to_anchor_items(df: pd.DataFrame) -> List[EvidenceItem]:
        if df.empty:
            return []
        anchors = df[df["final_can_be_news_trigger"].map(parse_bool)].copy()
        items: List[EvidenceItem] = []
        for _, row in anchors.iterrows():
            items.append(
                EvidenceItem(
                    source_type="stock_event",
                    evidence_id=clean_str(row.get("evidence_id")) or "STOCK_EVENT_UNKNOWN",
                    stock_code=normalize_stock_code(row.get("stock_code")),
                    stock_name=clean_str(row.get("stock_name")),
                    event_date=pd.Timestamp(row.get("event_date")).normalize(),
                    event_family=clean_str(row.get("event_family")) or "unknown",
                    title=clean_str(row.get("title"), 500),
                    description=clean_str(row.get("description"), 900),
                    expected_direction=clean_str(row.get("expected_direction_norm")) or "unknown",
                    role_hint="topic_anchor_candidate_no_source_market_cause",
                    priority=20,
                    materiality_score=StockEventLoader._stock_event_materiality(row),
                    raw=StockEventLoader._compact_stock_event_row(row),
                )
            )
        return items

    @staticmethod
    def to_context_items(df: pd.DataFrame) -> List[EvidenceItem]:
        if df.empty:
            return []
        context = df[~df["final_can_be_news_trigger"].map(parse_bool)].copy()
        items: List[EvidenceItem] = []
        for _, row in context.iterrows():
            items.append(
                EvidenceItem(
                    source_type="stock_event_context",
                    evidence_id=clean_str(row.get("evidence_id")) or "STOCK_EVENT_CONTEXT_UNKNOWN",
                    stock_code=normalize_stock_code(row.get("stock_code")),
                    stock_name=clean_str(row.get("stock_name")),
                    event_date=pd.Timestamp(row.get("event_date")).normalize(),
                    event_family=clean_str(row.get("event_family")) or "unknown",
                    title=clean_str(row.get("title"), 500),
                    description=clean_str(row.get("description"), 900),
                    expected_direction=clean_str(row.get("expected_direction_norm")) or "unknown",
                    role_hint=clean_str(row.get("final_allowed_usage")) or "background_or_supporting_context",
                    priority=80,
                    materiality_score=0.0,
                    raw=StockEventLoader._compact_stock_event_row(row),
                )
            )
        return items

    @staticmethod
    def _stock_event_materiality(row: pd.Series) -> float:
        score = 0.0
        severity = clean_str(row.get("original_severity")).lower()
        if severity == "high":
            score += 3.0
        elif severity == "medium":
            score += 2.0
        elif severity == "low":
            score += 1.0
        conf = safe_float(row.get("llm_confidence"))
        if conf is not None:
            score += conf
        if clean_str(row.get("evidence_role")) == "primary_candidate_for_news_topic":
            score += 1.0
        return score

    @staticmethod
    def _compact_stock_event_row(row: pd.Series) -> Dict[str, Any]:
        cols = [
            "evidence_id",
            "event_date",
            "stock_code",
            "stock_name",
            "event_type",
            "title",
            "description",
            "sector",
            "region",
            "original_direction",
            "original_severity",
            "stock_event_class",
            "evidence_role",
            "final_can_be_news_trigger",
            "final_allowed_usage",
            "source_market_claim_level",
            "can_be_market_cause_ceiling",
            "can_be_main_cause_ceiling",
            "bundle_market_claim_level_hint",
            "allowed_claim_scope",
            "forbidden_claim_scope",
            "validator_flags",
            "final_event_interpretation",
            "llm_decision",
            "llm_event_interpretation",
        ]
        return {c: jsonable(row.get(c)) for c in cols if c in row.index and jsonable(row.get(c)) is not None}


class DartLoader:
    def __init__(self, path: Optional[Path]):
        self.path = path

    def load(self) -> pd.DataFrame:
        df = read_table(self.path)
        if df.empty:
            return df

        df = df.copy()
        date_col = first_col(df.columns, ["dart_date", "rcept_dt", "rcept_date", "event_date", "date"])
        if date_col is None:
            raise ValueError("DART evidence needs one date column: dart_date/rcept_dt/rcept_date/event_date/date")
        if "stock_code" not in df.columns:
            raise ValueError("DART evidence missing stock_code")

        df["event_date"] = to_date_series(df[date_col])
        df["stock_code"] = df["stock_code"].map(normalize_stock_code)
        if "stock_name" not in df.columns:
            df["stock_name"] = ""
        df = df[df["stock_code"].ne("") & df["event_date"].notna()].copy()

        excluded_col = first_col(df.columns, ["is_excluded_routine", "excluded", "is_routine"])
        if excluded_col:
            if excluded_col == "is_routine":
                df["is_excluded_routine_norm"] = df[excluded_col].map(parse_bool)
            else:
                df["is_excluded_routine_norm"] = df[excluded_col].map(parse_bool)
        else:
            df["is_excluded_routine_norm"] = False

        df["event_family"] = df.apply(EventFamilyClassifier.classify_dart, axis=1)
        return df

    @staticmethod
    def to_anchor_items(df: pd.DataFrame) -> List[EvidenceItem]:
        if df.empty:
            return []
        use = df[~df["is_excluded_routine_norm"].map(parse_bool)].copy()
        items: List[EvidenceItem] = []
        for idx, row in use.iterrows():
            report = clean_str(row.get("report_name_clean")) or clean_str(row.get("report_name"))
            rid = clean_str(row.get("rcept_no")) or f"ROW_{idx}"
            items.append(
                EvidenceItem(
                    source_type="dart",
                    evidence_id=f"DART_{rid}",
                    stock_code=normalize_stock_code(row.get("stock_code")),
                    stock_name=clean_str(row.get("stock_name")),
                    event_date=pd.Timestamp(row.get("event_date")).normalize(),
                    event_family=clean_str(row.get("event_family")) or "unknown",
                    title=report,
                    description=clean_str(row.get("dart_event_group")) or report,
                    expected_direction="unknown",
                    role_hint="official_topic_source_not_market_cause",
                    priority=10,
                    materiality_score=safe_float(row.get("dart_materiality_score")) or 0.0,
                    raw=DartLoader._compact_dart_row(row),
                )
            )
        return items

    @staticmethod
    def _compact_dart_row(row: pd.Series) -> Dict[str, Any]:
        cols = [
            "event_date",
            "dart_date",
            "stock_code",
            "stock_name",
            "corp_code",
            "report_name",
            "report_name_clean",
            "rcept_no",
            "is_excluded_routine",
            "dart_event_group",
            "dart_materiality_score",
            "event_family",
        ]
        return {c: jsonable(row.get(c)) for c in cols if c in row.index and jsonable(row.get(c)) is not None}


class FlexibleEvidenceLoader:
    """Load optional price/GDELT/macro tables with schema-tolerant normalization."""

    DATE_CANDIDATES = [
        "event_date",
        "date",
        "ref_date",
        "published_at",
        "datetime",
        "trade_date",
        "일자",
        "날짜",
    ]

    STOCK_CODE_CANDIDATES = ["stock_code", "ticker", "종목코드", "code"]
    STOCK_NAME_CANDIDATES = ["stock_name", "corp_name", "name", "종목명", "company_name"]

    @classmethod
    def load_optional(cls, path: Optional[Path], kind: str) -> pd.DataFrame:
        df = read_table(path)
        if df.empty:
            return df
        df = df.copy()

        date_col = first_col(df.columns, cls.DATE_CANDIDATES)
        if date_col:
            df["event_date"] = to_date_series(df[date_col])
        else:
            df["event_date"] = pd.NaT

        stock_code_col = first_col(df.columns, cls.STOCK_CODE_CANDIDATES)
        if stock_code_col:
            df["stock_code"] = df[stock_code_col].map(normalize_stock_code)
        elif "stock_code" not in df.columns:
            df["stock_code"] = ""

        stock_name_col = first_col(df.columns, cls.STOCK_NAME_CANDIDATES)
        if stock_name_col and stock_name_col != "stock_name":
            df["stock_name"] = df[stock_name_col].map(clean_str)
        elif "stock_name" not in df.columns:
            df["stock_name"] = ""

        df["optional_evidence_kind"] = kind
        df = df[df["event_date"].notna()].copy()
        return df


# =============================================================================
# Union-find grouping
# =============================================================================

class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


# =============================================================================
# Matching logic
# =============================================================================

class EventMatcher:
    def __init__(self, config: EventGroupConfig):
        self.config = config

    def should_merge(self, a: EvidenceItem, b: EvidenceItem) -> Tuple[bool, str]:
        if a.stock_code != b.stock_code:
            return False, "different_stock"

        d = days_between(a.event_date, b.event_date)
        max_window = self._window_for_pair(a, b)
        if d > max_window:
            return False, f"date_gap>{max_window}"

        if not FamilyCompatibility.compatible(a.event_family, b.event_family):
            return False, f"family_incompatible:{a.event_family}!={b.event_family}"

        if self._quarter_conflict(a, b):
            return False, "quarter_conflict"
        if self._year_conflict(a, b):
            return False, "year_conflict"

        score, shared = self.topic_similarity(a, b)
        needs_overlap = FamilyCompatibility.merge_requires_text_overlap(a.event_family, b.event_family)

        if not needs_overlap and d <= 1:
            return True, f"same_family_near_date:d={d},jaccard={score:.2f},shared={shared}"

        if shared >= self.config.min_shared_topic_tokens and score >= self.config.min_topic_jaccard:
            return True, f"topic_overlap:d={d},jaccard={score:.2f},shared={shared}"

        # Earnings/guidance rows often have terse DART names. Allow a weak merge only when
        # date is very close and family is specific enough.
        if {a.event_family, b.event_family}.issubset({"earnings", "guidance", "dividend"}) and d <= 2:
            return True, f"specific_financial_family_near_date:d={d},jaccard={score:.2f},shared={shared}"

        return False, f"insufficient_topic_overlap:jaccard={score:.2f},shared={shared}"

    def topic_similarity(self, a: EvidenceItem, b: EvidenceItem) -> Tuple[float, int]:
        ta = TextNormalizer.tokens(a.topic_text, a.stock_name)
        tb = TextNormalizer.tokens(b.topic_text, b.stock_name)
        if not ta or not tb:
            return 0.0, 0
        inter = ta & tb
        union = ta | tb
        return len(inter) / max(1, len(union)), len(inter)

    def _window_for_pair(self, a: EvidenceItem, b: EvidenceItem) -> int:
        if {a.source_type, b.source_type} == {"dart", "stock_event"}:
            return self.config.dart_stock_event_window_days
        return self.config.same_source_window_days

    @staticmethod
    def _quarter_conflict(a: EvidenceItem, b: EvidenceItem) -> bool:
        qa = TextNormalizer.quarter_terms(a.topic_text)
        qb = TextNormalizer.quarter_terms(b.topic_text)
        return bool(qa and qb and qa.isdisjoint(qb))

    @staticmethod
    def _year_conflict(a: EvidenceItem, b: EvidenceItem) -> bool:
        ya = TextNormalizer.year_terms(a.topic_text)
        yb = TextNormalizer.year_terms(b.topic_text)
        return bool(ya and yb and ya.isdisjoint(yb))


# =============================================================================
# Optional attachment analyzers
# =============================================================================

class PriceReactionAnalyzer:
    RETURN_COLS = [
        "return_pct",
        "price_return_pct",
        "close_return_pct",
        "ret_pct",
        "pct_change",
        "change_pct",
        "등락률",
        "변동률",
    ]
    RETURN_DECIMAL_COLS = ["return", "price_return", "close_return", "ret"]
    VOLUME_RATIO_COLS = [
        "volume_ratio",
        "vol_ratio",
        "volume_z_ratio",
        "volume_multiple",
        "거래량배율",
    ]
    VOLUME_Z_COLS = ["volume_z", "vol_z", "abnormal_volume_z"]
    DIRECTION_COLS = ["price_direction", "direction", "reaction_direction", "등락방향"]

    def __init__(self, config: EventGroupConfig):
        self.config = config

    def compact_price_row(self, row: pd.Series) -> Dict[str, Any]:
        ret = self.extract_return_pct(row)
        vol_ratio = self.extract_volume_ratio(row)
        direction = self.extract_price_direction(row)
        out: Dict[str, Any] = {
            "source_type": "price_volume",
            "event_date": jsonable(row.get("event_date")),
            "stock_code": normalize_stock_code(row.get("stock_code")),
            "stock_name": clean_str(row.get("stock_name")),
            "reaction_role": "reaction_evidence_only_not_cause",
            "price_reaction_direction": direction,
        }
        if ret is not None:
            out["return_pct"] = ret
        if vol_ratio is not None:
            out["volume_ratio"] = vol_ratio

        for c in [
            "open", "high", "low", "close", "volume", "거래량", "종가", "시가",
            "abnormal_return", "abnormal_volume", "is_strong_reaction",
        ]:
            if c in row.index and jsonable(row.get(c)) is not None:
                out[c] = jsonable(row.get(c))
        return out

    def is_strong_reaction(self, row: pd.Series) -> bool:
        explicit_cols = ["is_strong_reaction", "strong_reaction", "abnormal_reaction", "is_anomaly"]
        for c in explicit_cols:
            if c in row.index and parse_bool(row.get(c), default=False):
                return True
        ret = self.extract_return_pct(row)
        vol_ratio = self.extract_volume_ratio(row)
        vol_z = self.extract_first_float(row, self.VOLUME_Z_COLS)
        if ret is not None and abs(ret) >= self.config.strong_abs_return_pct:
            return True
        if vol_ratio is not None and vol_ratio >= self.config.strong_volume_ratio:
            return True
        if vol_z is not None and abs(vol_z) >= 2.0:
            return True
        return False

    def extract_price_direction(self, row: pd.Series) -> str:
        for c in self.DIRECTION_COLS:
            if c in row.index:
                s = clean_str(row.get(c)).lower()
                if s in {"positive", "up", "rise", "rising", "상승", "+"}:
                    return "positive"
                if s in {"negative", "down", "fall", "falling", "하락", "-"}:
                    return "negative"
                if s in {"flat", "neutral", "mixed", "보합", "중립"}:
                    return "neutral"
        ret = self.extract_return_pct(row)
        if ret is None:
            return "unknown"
        if ret > 0.1:
            return "positive"
        if ret < -0.1:
            return "negative"
        return "neutral"

    def extract_return_pct(self, row: pd.Series) -> Optional[float]:
        v = self.extract_first_float(row, self.RETURN_COLS)
        if v is not None:
            return v
        d = self.extract_first_float(row, self.RETURN_DECIMAL_COLS)
        if d is not None:
            # Heuristic: if the value looks like a decimal return, convert to percent.
            if -1.0 <= d <= 1.0:
                return d * 100.0
            return d
        return None

    def extract_volume_ratio(self, row: pd.Series) -> Optional[float]:
        return self.extract_first_float(row, self.VOLUME_RATIO_COLS)

    @staticmethod
    def extract_first_float(row: pd.Series, cols: Sequence[str]) -> Optional[float]:
        for c in cols:
            if c in row.index:
                v = safe_float(row.get(c))
                if v is not None:
                    return v
        return None


class OptionalContextCompactor:
    def __init__(self, kind: str):
        self.kind = kind

    def compact_row(self, row: pd.Series) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "source_type": self.kind,
            "event_date": jsonable(row.get("event_date")),
            "stock_code": normalize_stock_code(row.get("stock_code")),
            "stock_name": clean_str(row.get("stock_name")),
        }
        likely_cols = [
            "title", "headline", "summary", "description", "detail", "url", "source_name", "domain",
            "tone_score", "themes_json", "orgs_json", "persons_json", "event_type", "asset_class",
            "related_assets", "direction", "severity", "macro_event", "indicator", "value",
        ]
        for c in likely_cols:
            if c in row.index and jsonable(row.get(c)) is not None:
                out[c] = clean_str(row.get(c), 500) if isinstance(row.get(c), str) else jsonable(row.get(c))
        return out


# =============================================================================
# Event group builder
# =============================================================================

class EventGroupBuilder:
    def __init__(self, config: EventGroupConfig):
        self.config = config
        self.matcher = EventMatcher(config)
        self.price_analyzer = PriceReactionAnalyzer(config)

    def build(
        self,
        dart_items: List[EvidenceItem],
        stock_anchor_items: List[EvidenceItem],
        stock_context_items: List[EvidenceItem],
        price_df: pd.DataFrame,
        gdelt_df: pd.DataFrame,
        macro_df: pd.DataFrame,
    ) -> Tuple[List[Dict[str, Any]], pd.DataFrame]:
        anchor_items = dart_items + stock_anchor_items
        anchor_items = [x for x in anchor_items if x.stock_code and x.event_date is not None]
        anchor_items.sort(key=lambda x: (x.stock_code, x.event_date, x.priority, -x.materiality_score, x.evidence_id))

        base_groups = self._merge_anchor_items(anchor_items)
        groups = [self._build_group_payload(i + 1, items) for i, items in enumerate(base_groups)]

        self._attach_stock_context(groups, stock_context_items)
        self._attach_price_reactions(groups, price_df)
        self._attach_gdelt_context(groups, gdelt_df)
        self._attach_macro_context(groups, macro_df)
        self._finalize_permissions(groups)
        unexplained = self._build_unexplained_price_moves(groups, price_df)

        return groups, unexplained

    def _merge_anchor_items(self, items: List[EvidenceItem]) -> List[List[EvidenceItem]]:
        if not items:
            return []
        uf = UnionFind(len(items))

        by_stock: Dict[str, List[int]] = defaultdict(list)
        for i, item in enumerate(items):
            by_stock[item.stock_code].append(i)

        for indices in by_stock.values():
            indices.sort(key=lambda i: items[i].event_date)
            for pos, i in enumerate(indices):
                a = items[i]
                for j in indices[pos + 1 :]:
                    b = items[j]
                    # Once the date gap exceeds the largest possible window, stop scanning.
                    if days_between(a.event_date, b.event_date) > max(
                        self.config.dart_stock_event_window_days,
                        self.config.same_source_window_days,
                    ):
                        break
                    ok, _reason = self.matcher.should_merge(a, b)
                    if ok:
                        uf.union(i, j)

        buckets: Dict[int, List[EvidenceItem]] = defaultdict(list)
        for i, item in enumerate(items):
            buckets[uf.find(i)].append(item)

        groups = list(buckets.values())
        for g in groups:
            g.sort(key=lambda x: (x.priority, -x.materiality_score, x.event_date, x.evidence_id))
        groups.sort(key=lambda g: (g[0].stock_code, g[0].event_date, g[0].priority, g[0].evidence_id))
        return groups

    def _build_group_payload(self, seq: int, items: List[EvidenceItem]) -> Dict[str, Any]:
        primary = self._choose_primary(items)
        start = min(x.event_date for x in items)
        end = max(x.event_date for x in items)
        families = Counter(x.event_family for x in items)
        family = primary.event_family or families.most_common(1)[0][0]
        stock_name = primary.stock_name or next((x.stock_name for x in items if x.stock_name), "")

        dart_items = [x for x in items if x.source_type == "dart"]
        stock_event_items = [x for x in items if x.source_type == "stock_event"]

        return {
            "event_group_id": f"STOCK_EVT_GROUP_{seq:06d}",
            "stock_code": primary.stock_code,
            "stock_name": stock_name,
            "event_date_start": jsonable(start),
            "event_date_end": jsonable(end),
            "anchor_date": jsonable(primary.event_date),
            "event_family": family,
            "canonical_topic": self._canonical_topic(primary, items),
            "primary_topic_source": primary.source_type,
            "primary_evidence_id": primary.evidence_id,
            "source_evidence_ids": [x.evidence_id for x in items],
            "source_types": sorted(set(x.source_type for x in items)),
            "dart_items": [x.to_compact_dict() for x in dart_items],
            "stock_event_items": [x.to_compact_dict() for x in stock_event_items],
            "stock_event_context_items": [],
            "price_volume_items": [],
            "gdelt_items": [],
            "macro_items": [],
            "event_expected_direction": self._derive_expected_direction(items),
            "precheck": {
                "has_official_evidence": bool(dart_items),
                "has_stock_event_trigger": bool(stock_event_items),
                "has_stock_event_context": False,
                "has_price_reaction": False,
                "has_strong_price_reaction": False,
                "has_gdelt_support": False,
                "has_macro_background": False,
                "price_volume_is_reaction_only": True,
                "source_level_market_cause_allowed": False,
                "price_volume_anchor_allowed": False,
            },
            "directional_consistency": "not_evaluated",
            "directional_consistency_detail": "price reaction not attached yet",
            "max_market_claim_level_pre_llm": "no_market_claim",
            "judge_allowed_level_band": [],
            "permission_notes": [
                "This is an event group, not final news text.",
                "DART/stock_event can define a topic anchor, not a market cause by themselves.",
                "Price-volume evidence, if attached, is reaction-only.",
            ],
            "forbidden_claims": [
                "Do not claim price causality from stock_event alone.",
                "Do not claim DART existence proves market impact.",
                "Do not treat price-volume movement as a cause.",
                "Do not use macro/GDELT as stock-specific main cause at this stage.",
            ],
        }

    @staticmethod
    def _choose_primary(items: List[EvidenceItem]) -> EvidenceItem:
        # DART first, then materiality, then earliest date.
        return sorted(items, key=lambda x: (x.priority, -x.materiality_score, x.event_date, x.evidence_id))[0]

    @staticmethod
    def _canonical_topic(primary: EvidenceItem, items: List[EvidenceItem]) -> str:
        title = clean_str(primary.title, 220)
        if title:
            return title
        for item in items:
            if item.title:
                return clean_str(item.title, 220)
        return f"{primary.stock_name or primary.stock_code} {primary.event_family} event"

    @staticmethod
    def _derive_expected_direction(items: List[EvidenceItem]) -> str:
        dirs = [x.expected_direction for x in items if x.expected_direction in {"positive", "negative", "neutral"}]
        if not dirs:
            return "unknown"
        cnt = Counter(dirs)
        if cnt["positive"] and cnt["negative"]:
            return "mixed"
        return cnt.most_common(1)[0][0]

    def _attach_stock_context(self, groups: List[Dict[str, Any]], context_items: List[EvidenceItem]) -> None:
        if not groups or not context_items:
            return
        by_stock: Dict[str, List[EvidenceItem]] = defaultdict(list)
        for item in context_items:
            by_stock[item.stock_code].append(item)

        for group in groups:
            candidates = by_stock.get(group["stock_code"], [])
            attached: List[EvidenceItem] = []
            anchor = to_date_value(group["anchor_date"])
            for item in candidates:
                if days_between(anchor, item.event_date) > self.config.context_window_days:
                    continue
                if not self._context_compatible(group, item):
                    continue
                attached.append(item)

            attached.sort(key=lambda x: (days_between(anchor, x.event_date), x.event_family, x.evidence_id))
            attached = attached[: self.config.max_items_per_type]
            group["stock_event_context_items"] = [x.to_compact_dict() for x in attached]
            group["precheck"]["has_stock_event_context"] = bool(attached)

    @staticmethod
    def _context_compatible(group: Dict[str, Any], item: EvidenceItem) -> bool:
        group_family = clean_str(group.get("event_family"))
        if item.event_family in {"macro_exposure", "sector_theme"}:
            return True
        return FamilyCompatibility.compatible(group_family, item.event_family)

    def _attach_price_reactions(self, groups: List[Dict[str, Any]], price_df: pd.DataFrame) -> None:
        if not groups or price_df.empty:
            return
        if "stock_code" not in price_df.columns or "event_date" not in price_df.columns:
            return

        p = price_df.copy()
        p["stock_code"] = p["stock_code"].map(normalize_stock_code)
        p = p[p["stock_code"].ne("") & p["event_date"].notna()].copy()
        by_stock = {k: v.sort_values("event_date") for k, v in p.groupby("stock_code")}

        for group in groups:
            anchor = to_date_value(group["anchor_date"])
            if anchor is None:
                continue
            sdf = by_stock.get(group["stock_code"])
            if sdf is None or sdf.empty:
                continue
            mask = (sdf["event_date"] >= anchor - pd.Timedelta(days=self.config.price_window_days)) & (
                sdf["event_date"] <= anchor + pd.Timedelta(days=self.config.price_window_days)
            )
            cand = sdf[mask].copy()
            if cand.empty:
                continue

            cand["_strong"] = cand.apply(self.price_analyzer.is_strong_reaction, axis=1)
            cand["_date_gap"] = cand["event_date"].map(lambda d: days_between(anchor, d))
            cand = cand.sort_values(["_strong", "_date_gap"], ascending=[False, True])
            rows = [self.price_analyzer.compact_price_row(r) for _, r in cand.head(self.config.max_items_per_type).iterrows()]
            group["price_volume_items"] = rows
            group["precheck"]["has_price_reaction"] = bool(rows)
            group["precheck"]["has_strong_price_reaction"] = bool(cand["_strong"].any())

    def _attach_gdelt_context(self, groups: List[Dict[str, Any]], gdelt_df: pd.DataFrame) -> None:
        if not groups or gdelt_df.empty:
            return
        if "event_date" not in gdelt_df.columns:
            return
        compactor = OptionalContextCompactor("gdelt")
        g = gdelt_df.copy()
        if "stock_code" in g.columns:
            g["stock_code"] = g["stock_code"].map(normalize_stock_code)

        for group in groups:
            anchor = to_date_value(group["anchor_date"])
            if anchor is None:
                continue
            mask_date = (g["event_date"] >= anchor - pd.Timedelta(days=self.config.gdelt_window_days)) & (
                g["event_date"] <= anchor + pd.Timedelta(days=self.config.gdelt_window_days)
            )
            cand = g[mask_date].copy()
            if cand.empty:
                continue

            stock_code = group["stock_code"]
            stock_name = clean_str(group.get("stock_name"))
            if "stock_code" in cand.columns and cand["stock_code"].astype(str).str.len().gt(0).any():
                cand = cand[cand["stock_code"].map(normalize_stock_code).eq(stock_code)]
            else:
                cand = self._filter_rows_by_stock_name(cand, stock_name)

            if cand.empty:
                continue
            cand["_date_gap"] = cand["event_date"].map(lambda d: days_between(anchor, d))
            cand = cand.sort_values("_date_gap")
            rows = [compactor.compact_row(r) for _, r in cand.head(self.config.max_items_per_type).iterrows()]
            group["gdelt_items"] = rows
            group["precheck"]["has_gdelt_support"] = bool(rows)

    def _attach_macro_context(self, groups: List[Dict[str, Any]], macro_df: pd.DataFrame) -> None:
        if not groups or macro_df.empty:
            return
        if "event_date" not in macro_df.columns:
            return
        compactor = OptionalContextCompactor("macro")
        m = macro_df.copy()

        for group in groups:
            anchor = to_date_value(group["anchor_date"])
            if anchor is None:
                continue
            mask_date = (m["event_date"] >= anchor - pd.Timedelta(days=self.config.macro_window_days)) & (
                m["event_date"] <= anchor + pd.Timedelta(days=self.config.macro_window_days)
            )
            cand = m[mask_date].copy()
            if cand.empty:
                continue
            cand["_date_gap"] = cand["event_date"].map(lambda d: days_between(anchor, d))
            cand = cand.sort_values("_date_gap")
            rows = [compactor.compact_row(r) for _, r in cand.head(self.config.max_items_per_type).iterrows()]
            group["macro_items"] = rows
            group["precheck"]["has_macro_background"] = bool(rows)

    @staticmethod
    def _filter_rows_by_stock_name(df: pd.DataFrame, stock_name: str) -> pd.DataFrame:
        if not stock_name:
            return df.iloc[0:0]
        text_cols = [
            c for c in ["title", "headline", "summary", "description", "orgs_json", "persons_json", "source_name", "domain"] if c in df.columns
        ]
        if not text_cols:
            return df.iloc[0:0]
        pattern = re.escape(stock_name)
        mask = pd.Series(False, index=df.index)
        for c in text_cols:
            mask = mask | df[c].astype(str).str.contains(pattern, case=False, na=False)
        return df[mask].copy()

    def _finalize_permissions(self, groups: List[Dict[str, Any]]) -> None:
        for group in groups:
            group["directional_consistency"], group["directional_consistency_detail"] = self._directional_consistency(group)
            group["max_market_claim_level_pre_llm"] = self._max_market_claim_level(group)
            group["judge_allowed_level_band"] = self._allowed_level_band(group["max_market_claim_level_pre_llm"])

    @staticmethod
    def _directional_consistency(group: Dict[str, Any]) -> Tuple[str, str]:
        expected = clean_str(group.get("event_expected_direction")) or "unknown"
        prices = group.get("price_volume_items") or []
        if not prices:
            return "not_evaluated", "no price-volume reaction attached"
        price_dirs = [clean_str(x.get("price_reaction_direction")) for x in prices]
        price_dirs = [x for x in price_dirs if x in {"positive", "negative", "neutral"}]
        if not price_dirs:
            return "unknown", "price direction unavailable"
        price_counter = Counter(price_dirs)
        dominant_price = price_counter.most_common(1)[0][0]

        if expected in {"unknown", "mixed", "neutral"}:
            return "unknown", f"event expected direction is {expected}; dominant price direction is {dominant_price}"
        if dominant_price == "neutral":
            return "weak_or_neutral", f"event expected direction is {expected}; dominant price direction is neutral"
        if expected == dominant_price:
            return "consistent", f"event expected direction and dominant price direction are both {expected}"
        return "conflict", f"event expected direction is {expected}; dominant price direction is {dominant_price}"

    @staticmethod
    def _max_market_claim_level(group: Dict[str, Any]) -> str:
        pre = group.get("precheck", {})
        has_anchor = pre.get("has_official_evidence") or pre.get("has_stock_event_trigger")
        has_price = pre.get("has_price_reaction")
        has_strong_price = pre.get("has_strong_price_reaction")
        has_official = pre.get("has_official_evidence")
        has_stock_event = pre.get("has_stock_event_trigger")
        has_gdelt = pre.get("has_gdelt_support")
        consistency = clean_str(group.get("directional_consistency"))

        if not has_anchor:
            return "no_market_claim"
        if not has_price:
            # Topic/background can be discussed, but no price reaction narrative exists.
            return "plausible_market_context"
        if consistency in {"conflict", "weak_or_neutral"}:
            return "reaction_only"
        if consistency == "unknown":
            return "reaction_only"
        if consistency == "consistent":
            if has_official and has_stock_event and has_gdelt and has_strong_price:
                return "primary_market_driver_candidate"
            if has_official and has_strong_price:
                return "likely_contributor"
            if has_stock_event and has_strong_price:
                return "plausible_market_context"
            return "reaction_only"
        return "reaction_only"

    @staticmethod
    def _allowed_level_band(max_level: str) -> List[str]:
        ladder = [
            "insufficient_evidence",
            "no_market_claim",
            "reaction_only",
            "plausible_market_context",
            "likely_contributor",
            "primary_market_driver_candidate",
        ]
        if max_level not in ladder:
            max_level = "no_market_claim"
        idx = ladder.index(max_level)
        return ladder[: idx + 1]

    def _build_unexplained_price_moves(self, groups: List[Dict[str, Any]], price_df: pd.DataFrame) -> pd.DataFrame:
        if price_df.empty or "stock_code" not in price_df.columns or "event_date" not in price_df.columns:
            return pd.DataFrame()

        p = price_df.copy()
        p["stock_code"] = p["stock_code"].map(normalize_stock_code)
        p = p[p["stock_code"].ne("") & p["event_date"].notna()].copy()
        if p.empty:
            return pd.DataFrame()
        p["is_strong_reaction_norm"] = p.apply(self.price_analyzer.is_strong_reaction, axis=1)
        p = p[p["is_strong_reaction_norm"]].copy()
        if p.empty:
            return pd.DataFrame()

        group_windows: Dict[str, List[Tuple[pd.Timestamp, pd.Timestamp, str]]] = defaultdict(list)
        for g in groups:
            anchor = to_date_value(g.get("anchor_date"))
            if anchor is None:
                continue
            group_windows[g["stock_code"]].append(
                (
                    anchor - pd.Timedelta(days=self.config.price_window_days),
                    anchor + pd.Timedelta(days=self.config.price_window_days),
                    g["event_group_id"],
                )
            )

        unexplained_rows = []
        for _, row in p.iterrows():
            code = normalize_stock_code(row.get("stock_code"))
            dt = to_date_value(row.get("event_date"))
            if dt is None:
                continue
            explained_by = []
            for start, end, group_id in group_windows.get(code, []):
                if start <= dt <= end:
                    explained_by.append(group_id)
            if explained_by:
                continue
            compact = self.price_analyzer.compact_price_row(row)
            compact["unexplained_price_move_id"] = f"UNEXPLAINED_PV_{len(unexplained_rows)+1:06d}"
            compact["allowed_usage"] = "non_causal_unexplained_movement_only"
            compact["forbidden_claim"] = "Do not infer or invent a catalyst from price-volume movement alone."
            unexplained_rows.append(compact)

        return pd.DataFrame(unexplained_rows)


# =============================================================================
# Writer and report
# =============================================================================

class EventGroupWriter:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, groups: List[Dict[str, Any]], unexplained_price_moves: pd.DataFrame) -> None:
        self._write_jsonl(groups)
        self._write_csv(groups)
        self._write_unexplained(unexplained_price_moves)
        self._write_report(groups, unexplained_price_moves)

    def _write_jsonl(self, groups: List[Dict[str, Any]]) -> None:
        path = self.output_dir / "stock_event_groups.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for g in groups:
                f.write(jdumps(g) + "\n")

    def _write_csv(self, groups: List[Dict[str, Any]]) -> None:
        rows = []
        for g in groups:
            pre = g.get("precheck", {})
            rows.append(
                {
                    "event_group_id": g.get("event_group_id"),
                    "stock_code": g.get("stock_code"),
                    "stock_name": g.get("stock_name"),
                    "event_date_start": g.get("event_date_start"),
                    "event_date_end": g.get("event_date_end"),
                    "anchor_date": g.get("anchor_date"),
                    "event_family": g.get("event_family"),
                    "canonical_topic": g.get("canonical_topic"),
                    "primary_topic_source": g.get("primary_topic_source"),
                    "primary_evidence_id": g.get("primary_evidence_id"),
                    "source_types": ",".join(g.get("source_types") or []),
                    "source_evidence_count": len(g.get("source_evidence_ids") or []),
                    "dart_count": len(g.get("dart_items") or []),
                    "stock_event_count": len(g.get("stock_event_items") or []),
                    "stock_event_context_count": len(g.get("stock_event_context_items") or []),
                    "price_volume_count": len(g.get("price_volume_items") or []),
                    "gdelt_count": len(g.get("gdelt_items") or []),
                    "macro_count": len(g.get("macro_items") or []),
                    "event_expected_direction": g.get("event_expected_direction"),
                    "directional_consistency": g.get("directional_consistency"),
                    "max_market_claim_level_pre_llm": g.get("max_market_claim_level_pre_llm"),
                    "has_official_evidence": pre.get("has_official_evidence"),
                    "has_stock_event_trigger": pre.get("has_stock_event_trigger"),
                    "has_price_reaction": pre.get("has_price_reaction"),
                    "has_strong_price_reaction": pre.get("has_strong_price_reaction"),
                    "has_gdelt_support": pre.get("has_gdelt_support"),
                    "has_macro_background": pre.get("has_macro_background"),
                    "judge_allowed_level_band": jdumps(g.get("judge_allowed_level_band") or []),
                    "source_evidence_ids": jdumps(g.get("source_evidence_ids") or []),
                }
            )
        pd.DataFrame(rows).to_csv(self.output_dir / "stock_event_groups.csv", index=False, encoding="utf-8-sig")

    def _write_unexplained(self, unexplained_price_moves: pd.DataFrame) -> None:
        path = self.output_dir / "unexplained_price_moves.csv"
        if unexplained_price_moves.empty:
            pd.DataFrame(
                columns=[
                    "unexplained_price_move_id",
                    "event_date",
                    "stock_code",
                    "stock_name",
                    "price_reaction_direction",
                    "return_pct",
                    "volume_ratio",
                    "allowed_usage",
                    "forbidden_claim",
                ]
            ).to_csv(path, index=False, encoding="utf-8-sig")
        else:
            unexplained_price_moves.to_csv(path, index=False, encoding="utf-8-sig")

    def _write_report(self, groups: List[Dict[str, Any]], unexplained_price_moves: pd.DataFrame) -> None:
        path = self.output_dir / "stock_event_group_report.md"
        lines: List[str] = []
        lines.append("# Stock Event Group Report")
        lines.append("")
        lines.append("## Summary")
        lines.append(f"- event_groups: {len(groups)}")
        lines.append(f"- unexplained_price_moves: {len(unexplained_price_moves)}")
        lines.append("")

        def count_by(key: str) -> Counter:
            return Counter(clean_str(g.get(key)) or "<NA>" for g in groups)

        lines.append("## Counts by primary_topic_source")
        for k, v in count_by("primary_topic_source").most_common():
            lines.append(f"- {k}: {v}")
        lines.append("")

        lines.append("## Counts by event_family")
        for k, v in count_by("event_family").most_common():
            lines.append(f"- {k}: {v}")
        lines.append("")

        lines.append("## Counts by max_market_claim_level_pre_llm")
        for k, v in count_by("max_market_claim_level_pre_llm").most_common():
            lines.append(f"- {k}: {v}")
        lines.append("")

        lines.append("## Counts by directional_consistency")
        for k, v in count_by("directional_consistency").most_common():
            lines.append(f"- {k}: {v}")
        lines.append("")

        lines.append("## Evidence attachment counts")
        lines.append(f"- groups_with_dart: {sum(bool(g.get('dart_items')) for g in groups)}")
        lines.append(f"- groups_with_stock_event_trigger: {sum(bool(g.get('stock_event_items')) for g in groups)}")
        lines.append(f"- groups_with_stock_event_context: {sum(bool(g.get('stock_event_context_items')) for g in groups)}")
        lines.append(f"- groups_with_price_volume: {sum(bool(g.get('price_volume_items')) for g in groups)}")
        lines.append(f"- groups_with_gdelt: {sum(bool(g.get('gdelt_items')) for g in groups)}")
        lines.append(f"- groups_with_macro: {sum(bool(g.get('macro_items')) for g in groups)}")
        lines.append("")

        lines.append("## Safety rules preserved")
        lines.append("- DART is used as official topic evidence, not as automatic market-cause evidence.")
        lines.append("- stock_event can be a topic anchor only when `final_can_be_news_trigger=True`.")
        lines.append("- source-level market causality remains forbidden for stock_event.")
        lines.append("- price-volume is reaction evidence only and cannot create an event group by itself.")
        lines.append("- macro and GDELT are attached only as background/supporting context at this stage.")
        lines.append("- `max_market_claim_level_pre_llm` is a deterministic ceiling for the later judge.")
        lines.append("")

        lines.append("## Output files")
        lines.append("- stock_event_groups.jsonl")
        lines.append("- stock_event_groups.csv")
        lines.append("- unexplained_price_moves.csv")
        lines.append("- stock_event_group_report.md")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")


# =============================================================================
# Pipeline
# =============================================================================

class EventGroupPipeline:
    def __init__(self, config: EventGroupConfig):
        self.config = config

    def run(self) -> None:
        print("=" * 100)
        print("[pr05d] Build stock event groups")
        print(f"stock_event_path: {self.config.stock_event_path}")
        print(f"dart_path: {self.config.dart_path}")
        print(f"price_volume_path: {self.config.price_volume_path}")
        print(f"gdelt_path: {self.config.gdelt_path}")
        print(f"macro_path: {self.config.macro_path}")
        print(f"output_dir: {self.config.output_dir}")
        print("=" * 100)

        stock_df = StockEventLoader(self.config.stock_event_path).load()
        dart_df = DartLoader(self.config.dart_path).load()
        price_df = FlexibleEvidenceLoader.load_optional(self.config.price_volume_path, "price_volume")
        gdelt_df = FlexibleEvidenceLoader.load_optional(self.config.gdelt_path, "gdelt")
        macro_df = FlexibleEvidenceLoader.load_optional(self.config.macro_path, "macro")

        stock_anchor_items = StockEventLoader.to_anchor_items(stock_df)
        stock_context_items = StockEventLoader.to_context_items(stock_df)
        dart_items = DartLoader.to_anchor_items(dart_df)

        print(f"[loaded] stock_event rows: {len(stock_df)}")
        print(f"[loaded] stock_event anchor candidates: {len(stock_anchor_items)}")
        print(f"[loaded] stock_event context rows: {len(stock_context_items)}")
        print(f"[loaded] dart anchor candidates: {len(dart_items)}")
        print(f"[loaded] price rows: {len(price_df)}")
        print(f"[loaded] gdelt rows: {len(gdelt_df)}")
        print(f"[loaded] macro rows: {len(macro_df)}")

        builder = EventGroupBuilder(self.config)
        groups, unexplained = builder.build(
            dart_items=dart_items,
            stock_anchor_items=stock_anchor_items,
            stock_context_items=stock_context_items,
            price_df=price_df,
            gdelt_df=gdelt_df,
            macro_df=macro_df,
        )

        writer = EventGroupWriter(self.config.output_dir)
        writer.write(groups, unexplained)

        print("-" * 100)
        print(f"[done] event_groups: {len(groups)}")
        print(f"[done] unexplained_price_moves: {len(unexplained)}")
        print(f"[done] wrote: {self.config.output_dir / 'stock_event_groups.jsonl'}")
        print(f"[done] wrote: {self.config.output_dir / 'stock_event_groups.csv'}")
        print(f"[done] wrote: {self.config.output_dir / 'unexplained_price_moves.csv'}")
        print(f"[done] wrote: {self.config.output_dir / 'stock_event_group_report.md'}")
        print("=" * 100)


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build rule-based stock event groups before evidence-bundle construction."
    )
    ap.add_argument("--stock-event-annotations", type=str, default=str(DEFAULT_STOCK_EVENT))
    ap.add_argument("--dart-evidence", type=str, default=str(DEFAULT_DART))
    ap.add_argument("--price-volume-evidence", type=str, default="")
    ap.add_argument("--gdelt-evidence", type=str, default="")
    ap.add_argument("--macro-evidence", type=str, default="")
    ap.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR))

    ap.add_argument("--dart-stock-event-window-days", type=int, default=5)
    ap.add_argument("--same-source-window-days", type=int, default=3)
    ap.add_argument("--context-window-days", type=int, default=14)
    ap.add_argument("--price-window-days", type=int, default=3)
    ap.add_argument("--gdelt-window-days", type=int, default=7)
    ap.add_argument("--macro-window-days", type=int, default=7)
    ap.add_argument("--min-topic-jaccard", type=float, default=0.12)
    ap.add_argument("--min-shared-topic-tokens", type=int, default=1)
    ap.add_argument("--strong-abs-return-pct", type=float, default=3.0)
    ap.add_argument("--strong-volume-ratio", type=float, default=2.0)
    ap.add_argument("--max-items-per-type", type=int, default=12)
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    config = EventGroupConfig(
        stock_event_path=Path(args.stock_event_annotations).expanduser(),
        dart_path=none_path(args.dart_evidence),
        price_volume_path=none_path(args.price_volume_evidence),
        gdelt_path=none_path(args.gdelt_evidence),
        macro_path=none_path(args.macro_evidence),
        output_dir=Path(args.output_dir).expanduser(),
        dart_stock_event_window_days=args.dart_stock_event_window_days,
        same_source_window_days=args.same_source_window_days,
        context_window_days=args.context_window_days,
        price_window_days=args.price_window_days,
        gdelt_window_days=args.gdelt_window_days,
        macro_window_days=args.macro_window_days,
        min_topic_jaccard=args.min_topic_jaccard,
        min_shared_topic_tokens=args.min_shared_topic_tokens,
        strong_abs_return_pct=args.strong_abs_return_pct,
        strong_volume_ratio=args.strong_volume_ratio,
        max_items_per_type=args.max_items_per_type,
    )
    EventGroupPipeline(config).run()


if __name__ == "__main__":
    main()
