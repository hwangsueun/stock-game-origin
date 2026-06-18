# processor/pr_dci05a_build_stock_news_context_cards.py
# -*- coding: utf-8 -*-

"""
pr05a_build_stock_news_context_cards.py

목적:
    pr05 출력(event_thread_units.jsonl)을 기반으로,
    pr06 뉴스 생성용 stock_news_context_cards.jsonl을 생성한다.

핵심 원칙:
    1. pr05a는 뉴스 생성 단계가 아니다.
    2. pr05a는 종토방 반응 생성 단계가 아니다.
    3. source_threads / comments 원문을 pr06 입력으로 넘기지 않는다.
    4. raw numeric field(residual_z, return_pct, volume_ratio 등)를 LLM payload에 직접 넣지 않는다.
    5. DART / 직접 종목 이벤트는 high evidence.
    6. macro / sector / GDELT context는 종목 profile의 sensitivity_tags와 연결될 때만 medium evidence.
    7. price-only / volume-only / board-only event는 low evidence이며 market_price_alert로 보낸다.
    8. macro/sector context는 background로만 사용하고 직접 원인 단정 금지 guardrail을 함께 넣는다.

입력 기본값:
    data/processed/dci_llm_event_inputs/event_thread_units.jsonl

선택 입력:
    data/raw/market_event/macro_event_calendar_2013_2023.csv
    data/raw/market_event/stock_event_calendar_2013_2023.csv
    data/processed/gdelt_context/gdelt_context_summary.csv
    data/processed/stock_profile/yearly_stock_profiles.csv

출력:
    data/processed/dci_stock_news_context_cards/stock_news_context_cards.jsonl
    data/processed/dci_stock_news_context_cards/stock_news_context_cards_preview.csv
    data/processed/dci_stock_news_context_cards/stock_news_context_cards_report.txt
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd


# =============================================================================
# 공통 유틸
# =============================================================================


def normalize_stock_code(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return ""
    text = re.sub(r"\.0$", "", text)
    text = re.sub(r"[^0-9]", "", text)
    if text == "":
        return ""
    return text.zfill(6)[-6:]


def clean_text(value: Any, max_len: Optional[int] = None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value)
    text = text.replace("\u200b", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    if max_len is not None and len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    text = text.replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None

    text = re.sub(r"\.0$", "", text)

    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y%m%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    try:
        dt = pd.to_datetime(text, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None


def parse_list_like(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [clean_text(x) for x in value if clean_text(x)]
    if isinstance(value, tuple) or isinstance(value, set):
        return [clean_text(x) for x in value if clean_text(x)]
    if isinstance(value, float) and math.isnan(value):
        return []

    text = clean_text(value)
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple, set)):
                return [clean_text(x) for x in parsed if clean_text(x)]
        except Exception:
            pass

    parts = re.split(r"[,;/|]+", text)
    result = []
    for part in parts:
        part = clean_text(part)
        part = part.strip("'\"[]{}() ")
        if part:
            result.append(part)
    return result


def first_non_empty_from_dict(data: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in data:
            value = data.get(key)
            if value is not None and clean_text(value) != "":
                return value
    return None


def recursive_find_first(
    obj: Any,
    target_keys: Set[str],
    max_depth: int = 4,
    skip_keys: Optional[Set[str]] = None,
) -> Any:
    if skip_keys is None:
        skip_keys = {"source_threads", "comments", "comment_texts", "raw_comments"}

    if max_depth < 0:
        return None

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in target_keys:
                return value

        for key, value in obj.items():
            if key in skip_keys:
                continue
            found = recursive_find_first(value, target_keys, max_depth - 1, skip_keys)
            if found is not None:
                return found

    elif isinstance(obj, list):
        for item in obj[:20]:
            found = recursive_find_first(item, target_keys, max_depth - 1, skip_keys)
            if found is not None:
                return found

    return None


def read_jsonl(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if limit is not None and idx >= limit:
                break
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_csv_flexible(path: Path) -> pd.DataFrame:
    encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]
    last_error: Optional[Exception] = None

    for enc in encodings:
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc)
            df.columns = [str(c).strip() for c in df.columns]
            return df
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"CSV 읽기 실패: {path} / last_error={last_error}")


def pick_column(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    lower_map = {str(col).strip().lower(): col for col in df.columns}
    for cand in candidates:
        key = cand.strip().lower()
        if key in lower_map:
            return lower_map[key]
    return None


def unique_keep_order(values: Iterable[str], max_items: Optional[int] = None) -> List[str]:
    seen = set()
    result = []
    for value in values:
        value = clean_text(value)
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if max_items is not None and len(result) >= max_items:
            break
    return result


# =============================================================================
# 태그 정규화
# =============================================================================


class TagNormalizer:
    GENERIC_TAGS = {
        "시장",
        "증시",
        "경제",
        "투자심리",
        "변동성",
        "리스크",
        "불확실성",
        "수급",
    }

    KEYWORD_RULES: List[Tuple[str, List[str]]] = [
        (r"환율|원달러|달러|원화|외환", ["환율", "수출입"]),
        (r"수출|수입|무역|교역|통관", ["수출입", "글로벌 경기"]),
        (r"유가|원유|WTI|두바이유|브렌트", ["유가", "에너지"]),
        (r"금리|기준금리|국채|채권|긴축|인하|인상", ["금리"]),
        (r"물가|인플레이션|CPI|PPI", ["물가", "금리"]),
        (r"반도체|D램|낸드|파운드리|메모리", ["반도체"]),
        (r"중국|차이나", ["중국", "글로벌 경기"]),
        (r"미국|연준|Fed|FOMC", ["미국", "금리"]),
        (r"소비|소매|내수|가계", ["소비"]),
        (r"건설|부동산|분양|주택|SOC", ["건설", "부동산"]),
        (r"해운|운임|컨테이너|벌크|물류|항만", ["해운", "운임", "수출입"]),
        (r"항공|여객|화물기|항공유", ["항공", "유가"]),
        (r"화학|석유화학|나프타|정유", ["화학", "유가"]),
        (r"자동차|완성차|부품|전기차", ["자동차"]),
        (r"배터리|2차전지|이차전지|양극재|음극재", ["배터리"]),
        (r"바이오|제약|임상|의약품", ["바이오", "제약"]),
        (r"게임|콘텐츠|엔터|미디어", ["콘텐츠"]),
        (r"은행|보험|증권|금융지주", ["금융", "금리"]),
        (r"조선|선박|LNG선", ["조선", "해운"]),
        (r"철강|강재|원자재", ["철강", "원자재"]),
        (r"식품|음식료|곡물|농산물", ["음식료", "원자재"]),
        (r"면세|화장품|관광|여행", ["소비", "중국", "관광"]),
        (r"전력|전기|가스|유틸리티", ["에너지", "유틸리티"]),
    ]

    @classmethod
    def normalize_one(cls, tag: Any) -> str:
        text = clean_text(tag)
        if not text:
            return ""
        text = text.strip("#[]{}()'\" ")
        return text

    @classmethod
    def infer_tags_from_text(cls, text: Any) -> List[str]:
        source = clean_text(text)
        if not source:
            return []

        tags: List[str] = []
        for pattern, inferred in cls.KEYWORD_RULES:
            if re.search(pattern, source, flags=re.IGNORECASE):
                tags.extend(inferred)

        return unique_keep_order(tags)

    @classmethod
    def normalize_many(cls, values: Iterable[Any]) -> List[str]:
        tags: List[str] = []
        for value in values:
            tags.append(cls.normalize_one(value))
            tags.extend(cls.infer_tags_from_text(value))
        return unique_keep_order(tags)

    @classmethod
    def meaningful_intersection(cls, left: Sequence[str], right: Sequence[str]) -> List[str]:
        left_set = {cls.normalize_one(x) for x in left if cls.normalize_one(x)}
        right_set = {cls.normalize_one(x) for x in right if cls.normalize_one(x)}
        common = left_set.intersection(right_set)
        meaningful = [x for x in common if x not in cls.GENERIC_TAGS]
        return sorted(meaningful)


# =============================================================================
# 경로 설정
# =============================================================================


@dataclass
class PathConfig:
    project_root: Path
    event_units_path: Path
    output_dir: Path
    macro_event_csv: Optional[Path] = None
    stock_event_csv: Optional[Path] = None
    gdelt_context_path: Optional[Path] = None
    stock_profile_csv: Optional[Path] = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "PathConfig":
        root = Path(args.project_root).expanduser().resolve()

        event_units_path = cls._resolve_path(
            root,
            args.event_units_path,
            "data/processed/dci_llm_event_inputs/event_thread_units.jsonl",
        )

        output_dir = cls._resolve_path(
            root,
            args.output_dir,
            "data/processed/dci_stock_news_context_cards",
        )

        macro_event_csv = cls._resolve_optional_path(
            root,
            args.macro_event_csv,
            [
                "data/raw/market_event/macro_event_calendar_2013_2023.csv",
                "data/processed/market_event/macro_event_calendar_2013_2023.csv",
                "data/processed/macro_event_calendar_2013_2023.csv",
            ],
        )

        stock_event_csv = cls._resolve_optional_path(
            root,
            args.stock_event_csv,
            [
                "data/raw/market_event/stock_event_calendar_2013_2023.csv",
                "data/processed/market_event/stock_event_calendar_2013_2023.csv",
                "data/processed/stock_event_calendar_2013_2023.csv",
                "data/processed/stock_events/stock_event_calendar_2013_2023.csv",
            ],
        )

        gdelt_context_path = cls._resolve_optional_path(
            root,
            args.gdelt_context_path,
            [
                "data/processed/gdelt_context/gdelt_context_summary.csv",
                "data/processed/gdelt_context/gdelt_context_summary.jsonl",
                "data/processed/gdelt/gdelt_context_summary.csv",
                "data/processed/gdelt/gdelt_context_summary.jsonl",
            ],
        )

        stock_profile_csv = cls._resolve_optional_path(
            root,
            args.stock_profile_csv,
            [
                "data/processed/stock_profile/yearly_stock_profiles.csv",
                "data/processed/stock_profile/stock_profiles_yearly.csv",
                "data/processed/stock_profiles/yearly_stock_profiles.csv",
                "data/processed/stock_profiles/stock_profiles_yearly.csv",
                "data/processed/stock_profile/stock_profile_yearly.csv",
                "data/processed/dart_stock_profiles/stock_profile_yearly.csv",
                "data/processed/stock_profiles.csv",
            ],
        )

        return cls(
            project_root=root,
            event_units_path=event_units_path,
            output_dir=output_dir,
            macro_event_csv=macro_event_csv,
            stock_event_csv=stock_event_csv,
            gdelt_context_path=gdelt_context_path,
            stock_profile_csv=stock_profile_csv,
        )

    @staticmethod
    def _resolve_path(root: Path, value: Optional[str], default_relative: str) -> Path:
        if value:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = root / path
            return path.resolve()
        return (root / default_relative).resolve()

    @staticmethod
    def _resolve_optional_path(
        root: Path,
        explicit_value: Optional[str],
        relative_candidates: Sequence[str],
    ) -> Optional[Path]:
        if explicit_value:
            path = Path(explicit_value).expanduser()
            if not path.is_absolute():
                path = root / path
            return path.resolve()

        for rel in relative_candidates:
            path = (root / rel).resolve()
            if path.exists():
                return path

        return None


# =============================================================================
# 데이터 모델
# =============================================================================


@dataclass
class StockProfile:
    stock_code: str
    stock_name: str = ""
    business_year: Optional[int] = None
    sector: str = ""
    business_summary: str = ""
    macro_sensitive_factors: List[str] = field(default_factory=list)
    news_reaction_tags: List[str] = field(default_factory=list)
    asset_personality: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.stock_code and (self.business_summary or self.sensitivity_tags))

    @property
    def sensitivity_tags(self) -> List[str]:
        raw = []
        raw.extend(self.macro_sensitive_factors)
        raw.extend(self.news_reaction_tags)
        raw.append(self.sector)
        raw.append(self.asset_personality)
        raw.append(self.business_summary)
        return TagNormalizer.normalize_many(raw)[:16]


@dataclass
class DartEvidence:
    exists: bool
    cleaned_reports: List[Dict[str, Any]]
    disclosure_dates: str

    @classmethod
    def empty(cls) -> "DartEvidence":
        return cls(exists=False, cleaned_reports=[], disclosure_dates="")


@dataclass
class MarketSignal:
    price_label: str
    volume_label: str
    board_label: str
    signal_summary: str

    @property
    def has_price_or_volume_signal(self) -> bool:
        return self.price_label != "특이 없음" or self.volume_label != "특이 없음"


@dataclass
class ContextMatch:
    source: str
    theme: str
    event_date: Optional[date]
    scope: str
    relation_basis: str
    matched_tags: List[str]
    event_strength: str = ""
    is_direct_stock_event: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "theme": self.theme,
            "date": self.event_date.isoformat() if self.event_date else "",
            "scope": self.scope,
            "relation_basis": self.relation_basis,
            "matched_tags": self.matched_tags,
            "event_strength": self.event_strength,
            "usage_rule": "background_only_no_direct_causation",
        }


# =============================================================================
# Repository
# =============================================================================


class StockProfileRepository:
    STOCK_CODE_COLS = ["stock_code", "종목코드", "code", "ticker"]
    STOCK_NAME_COLS = ["stock_name", "종목명", "name", "corp_name", "회사명"]
    YEAR_COLS = ["business_year", "year", "기준연도", "사업연도"]
    SECTOR_COLS = ["sector", "industry", "업종", "섹터", "sector_name", "industry_name"]
    BUSINESS_SUMMARY_COLS = [
        "business_summary",
        "business_summary_asof",
        "사업요약",
        "기업개요",
        "description",
        "beginner_description",
    ]
    MACRO_FACTOR_COLS = [
        "macro_sensitive_factors",
        "sensitivity_tags",
        "민감요인",
        "거시민감요인",
    ]
    NEWS_TAG_COLS = [
        "news_reaction_tags",
        "reaction_tags",
        "related_tags",
        "뉴스반응태그",
    ]
    PERSONALITY_COLS = [
        "asset_personality",
        "personality",
        "자산성격",
    ]

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.by_code: Dict[str, List[StockProfile]] = defaultdict(list)
        self.load_warning = ""

        if path is not None and path.exists():
            self._load(path)
        else:
            self.load_warning = "stock_profile_csv 없음: macro/sector context 연결이 보수적으로 제한됨"

    def _load(self, path: Path) -> None:
        df = read_csv_flexible(path)

        code_col = pick_column(df, self.STOCK_CODE_COLS)
        name_col = pick_column(df, self.STOCK_NAME_COLS)
        year_col = pick_column(df, self.YEAR_COLS)
        sector_col = pick_column(df, self.SECTOR_COLS)
        summary_col = pick_column(df, self.BUSINESS_SUMMARY_COLS)
        macro_col = pick_column(df, self.MACRO_FACTOR_COLS)
        news_col = pick_column(df, self.NEWS_TAG_COLS)
        personality_col = pick_column(df, self.PERSONALITY_COLS)

        if not code_col:
            self.load_warning = f"stock_profile_csv에 stock_code 컬럼 없음: {path}"
            return

        for _, row in df.iterrows():
            code = normalize_stock_code(row.get(code_col))
            if not code:
                continue

            year = None
            if year_col:
                year_value = safe_float(row.get(year_col))
                if year_value is not None:
                    year = int(year_value)

            profile = StockProfile(
                stock_code=code,
                stock_name=clean_text(row.get(name_col)) if name_col else "",
                business_year=year,
                sector=clean_text(row.get(sector_col)) if sector_col else "",
                business_summary=clean_text(row.get(summary_col), max_len=240) if summary_col else "",
                macro_sensitive_factors=parse_list_like(row.get(macro_col)) if macro_col else [],
                news_reaction_tags=parse_list_like(row.get(news_col)) if news_col else [],
                asset_personality=clean_text(row.get(personality_col), max_len=120) if personality_col else "",
            )
            self.by_code[code].append(profile)

        for code in self.by_code:
            self.by_code[code].sort(
                key=lambda p: -1 if p.business_year is None else p.business_year,
                reverse=True,
            )

    def get(self, stock_code: str, target_date: Optional[date], fallback_name: str = "") -> StockProfile:
        code = normalize_stock_code(stock_code)
        profiles = self.by_code.get(code, [])

        if not profiles:
            return StockProfile(stock_code=code, stock_name=fallback_name)

        if target_date is None:
            selected = profiles[0]
            if not selected.stock_name and fallback_name:
                selected.stock_name = fallback_name
            return selected

        target_year = target_date.year

        dated = [p for p in profiles if p.business_year is not None and p.business_year <= target_year]
        if dated:
            selected = sorted(dated, key=lambda p: p.business_year or -1, reverse=True)[0]
        else:
            selected = profiles[0]

        if not selected.stock_name and fallback_name:
            selected.stock_name = fallback_name

        return selected


class EventContextRepository:
    DATE_COLS = ["date", "event_date", "base_date", "candidate_date", "날짜", "일자", "기준일"]
    THEME_COLS = [
        "theme",
        "event_theme",
        "event_name",
        "headline",
        "title",
        "event",
        "macro_theme",
        "stock_theme",
        "description",
        "내용",
        "지표명",
    ]
    TAG_COLS = [
        "related_tags",
        "tags",
        "asset_tags",
        "macro_tags",
        "stock_tags",
        "sensitivity_tags",
        "관련태그",
        "영향태그",
    ]
    SCOPE_COLS = ["scope", "event_scope", "범위"]
    STRENGTH_COLS = ["event_strength", "strength", "severity", "importance", "강도"]
    STOCK_CODE_COLS = ["stock_code", "종목코드", "code", "ticker"]
    STOCK_NAME_COLS = ["stock_name", "종목명", "name", "corp_name", "회사명"]
    SECTOR_COLS = ["sector", "industry", "업종", "섹터"]

    def __init__(self, path: Optional[Path], source_name: str):
        self.path = path
        self.source_name = source_name
        self.rows: List[Dict[str, Any]] = []
        self.load_warning = ""

        if path is not None and path.exists():
            self._load(path)
        else:
            self.load_warning = f"{source_name} 파일 없음"

    def _load(self, path: Path) -> None:
        if path.suffix.lower() == ".jsonl":
            raw_rows = read_jsonl(path)
            self.rows = [self._standardize_dict(row) for row in raw_rows]
            return

        df = read_csv_flexible(path)
        date_col = pick_column(df, self.DATE_COLS)
        theme_col = pick_column(df, self.THEME_COLS)
        tag_col = pick_column(df, self.TAG_COLS)
        scope_col = pick_column(df, self.SCOPE_COLS)
        strength_col = pick_column(df, self.STRENGTH_COLS)
        stock_code_col = pick_column(df, self.STOCK_CODE_COLS)
        stock_name_col = pick_column(df, self.STOCK_NAME_COLS)
        sector_col = pick_column(df, self.SECTOR_COLS)

        for _, row in df.iterrows():
            theme = clean_text(row.get(theme_col), max_len=140) if theme_col else ""
            if not theme:
                text_parts = []
                for col in df.columns[:8]:
                    text_parts.append(clean_text(row.get(col)))
                theme = clean_text(" ".join([x for x in text_parts if x]), max_len=140)

            tag_values = parse_list_like(row.get(tag_col)) if tag_col else []
            tag_values.extend(TagNormalizer.infer_tags_from_text(theme))

            sector = clean_text(row.get(sector_col)) if sector_col else ""
            tag_values.extend(TagNormalizer.infer_tags_from_text(sector))

            self.rows.append(
                {
                    "source": self.source_name,
                    "date": parse_date(row.get(date_col)) if date_col else None,
                    "theme": theme,
                    "tags": unique_keep_order(TagNormalizer.normalize_many(tag_values), max_items=16),
                    "scope": clean_text(row.get(scope_col)) if scope_col else "market_wide",
                    "event_strength": clean_text(row.get(strength_col)) if strength_col else "",
                    "stock_code": normalize_stock_code(row.get(stock_code_col)) if stock_code_col else "",
                    "stock_name": clean_text(row.get(stock_name_col)) if stock_name_col else "",
                    "sector": sector,
                }
            )

    def _standardize_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        theme = clean_text(
            first_non_empty_from_dict(row, self.THEME_COLS),
            max_len=140,
        )
        tag_values = parse_list_like(first_non_empty_from_dict(row, self.TAG_COLS))
        tag_values.extend(TagNormalizer.infer_tags_from_text(theme))

        return {
            "source": self.source_name,
            "date": parse_date(first_non_empty_from_dict(row, self.DATE_COLS)),
            "theme": theme,
            "tags": unique_keep_order(TagNormalizer.normalize_many(tag_values), max_items=16),
            "scope": clean_text(first_non_empty_from_dict(row, self.SCOPE_COLS)) or "market_wide",
            "event_strength": clean_text(first_non_empty_from_dict(row, self.STRENGTH_COLS)),
            "stock_code": normalize_stock_code(first_non_empty_from_dict(row, self.STOCK_CODE_COLS)),
            "stock_name": clean_text(first_non_empty_from_dict(row, self.STOCK_NAME_COLS)),
            "sector": clean_text(first_non_empty_from_dict(row, self.SECTOR_COLS)),
        }

    def find_macro_like_contexts(
        self,
        target_date: Optional[date],
        profile: StockProfile,
        window_days: int,
        limit: int,
    ) -> List[ContextMatch]:
        if target_date is None:
            return []
        if not profile.exists:
            return []

        profile_tags = profile.sensitivity_tags
        if not profile_tags:
            return []

        start = target_date - timedelta(days=window_days)
        end = target_date

        matches: List[ContextMatch] = []

        for row in self.rows:
            event_date = row.get("date")
            if event_date is not None:
                if event_date < start or event_date > end:
                    continue

            theme = clean_text(row.get("theme"))
            event_tags = row.get("tags") or []
            if not theme and not event_tags:
                continue

            matched_tags = TagNormalizer.meaningful_intersection(profile_tags, event_tags)

            if not matched_tags:
                for tag in profile_tags:
                    if tag in TagNormalizer.GENERIC_TAGS:
                        continue
                    if tag and tag in theme:
                        matched_tags.append(tag)

            if not matched_tags:
                continue

            relation_basis = self._build_relation_basis(profile, matched_tags)

            matches.append(
                ContextMatch(
                    source=self.source_name,
                    theme=theme,
                    event_date=event_date,
                    scope=clean_text(row.get("scope")) or "market_wide",
                    relation_basis=relation_basis,
                    matched_tags=unique_keep_order(matched_tags, max_items=5),
                    event_strength=clean_text(row.get("event_strength")),
                    is_direct_stock_event=False,
                )
            )

        matches.sort(
            key=lambda x: (
                x.event_date or date(1900, 1, 1),
                len(x.matched_tags),
            ),
            reverse=True,
        )
        return matches[:limit]

    @staticmethod
    def _build_relation_basis(profile: StockProfile, matched_tags: Sequence[str]) -> str:
        stock_name = profile.stock_name or profile.stock_code
        tag_text = ", ".join(matched_tags[:3])
        if profile.sector:
            return f"{stock_name}은/는 {profile.sector} 관련 종목이며, profile sensitivity tag 중 {tag_text}와 연결됨"
        return f"{stock_name}의 profile sensitivity tag 중 {tag_text}와 연결됨"

    def find_stock_event_contexts(
        self,
        target_date: Optional[date],
        stock_code: str,
        stock_name: str,
        profile: StockProfile,
        window_days: int,
        limit: int,
    ) -> List[ContextMatch]:
        if target_date is None:
            return []

        code = normalize_stock_code(stock_code)
        start = target_date - timedelta(days=window_days)
        end = target_date

        direct_matches: List[ContextMatch] = []
        sector_matches: List[ContextMatch] = []

        profile_tags = profile.sensitivity_tags

        for row in self.rows:
            event_date = row.get("date")
            if event_date is not None:
                if event_date < start or event_date > end:
                    continue

            row_code = normalize_stock_code(row.get("stock_code"))
            row_name = clean_text(row.get("stock_name"))
            theme = clean_text(row.get("theme"))
            event_tags = row.get("tags") or []
            row_sector = clean_text(row.get("sector"))

            is_direct = False
            if code and row_code and code == row_code:
                is_direct = True
            elif stock_name and row_name and stock_name == row_name:
                is_direct = True

            if is_direct:
                direct_matches.append(
                    ContextMatch(
                        source=self.source_name,
                        theme=theme,
                        event_date=event_date,
                        scope="stock_specific",
                        relation_basis=f"{stock_name or code}에 직접 연결된 종목 이벤트",
                        matched_tags=unique_keep_order(event_tags, max_items=5),
                        event_strength=clean_text(row.get("event_strength")),
                        is_direct_stock_event=True,
                    )
                )
                continue

            if not profile.exists:
                continue

            matched_tags = TagNormalizer.meaningful_intersection(profile_tags, event_tags)

            if profile.sector and row_sector and profile.sector == row_sector:
                matched_tags.append(profile.sector)

            if not matched_tags:
                continue

            sector_matches.append(
                ContextMatch(
                    source=self.source_name,
                    theme=theme,
                    event_date=event_date,
                    scope="sector_background",
                    relation_basis=f"{stock_name or code}의 profile/sector tag와 연결되는 업종 배경 이벤트",
                    matched_tags=unique_keep_order(matched_tags, max_items=5),
                    event_strength=clean_text(row.get("event_strength")),
                    is_direct_stock_event=False,
                )
            )

        direct_matches.sort(key=lambda x: x.event_date or date(1900, 1, 1), reverse=True)
        sector_matches.sort(key=lambda x: x.event_date or date(1900, 1, 1), reverse=True)

        return (direct_matches + sector_matches)[:limit]


# =============================================================================
# Extractor
# =============================================================================


class EventUnitExtractor:
    EVENT_ID_KEYS = {"event_id", "candidate_id", "id"}
    DATE_KEYS = {"date", "candidate_date", "event_date", "base_date"}
    STOCK_CODE_KEYS = {"stock_code", "종목코드", "code", "ticker"}
    STOCK_NAME_KEYS = {"stock_name", "종목명", "name", "corp_name"}

    @classmethod
    def event_id(cls, unit: Dict[str, Any], fallback_idx: int) -> str:
        value = recursive_find_first(unit, cls.EVENT_ID_KEYS)
        text = clean_text(value)
        if text:
            return text
        return f"EVT_{fallback_idx:06d}"

    @classmethod
    def event_date(cls, unit: Dict[str, Any]) -> Optional[date]:
        value = recursive_find_first(unit, cls.DATE_KEYS)
        return parse_date(value)

    @classmethod
    def stock_code(cls, unit: Dict[str, Any]) -> str:
        value = recursive_find_first(unit, cls.STOCK_CODE_KEYS)
        return normalize_stock_code(value)

    @classmethod
    def stock_name(cls, unit: Dict[str, Any]) -> str:
        value = recursive_find_first(unit, cls.STOCK_NAME_KEYS)
        return clean_text(value)


class DartEvidenceExtractor:
    DART_KEYS = {
        "dart",
        "dart_evidence",
        "dart_context",
        "disclosure",
        "disclosure_evidence",
        "filing",
        "filing_evidence",
    }

    REPORT_LIST_KEYS = [
        "cleaned_reports",
        "reports",
        "items",
        "filings",
        "disclosures",
        "evidence",
    ]

    REPORT_NAME_KEYS = [
        "report_name",
        "source_report_name",
        "disclosure_name",
        "title",
        "name",
        "공시명",
    ]

    REPORT_DATE_KEYS = [
        "rcept_date",
        "source_rcept_date",
        "disclosure_date",
        "date",
        "공시일",
    ]

    REPORT_NO_KEYS = [
        "rcept_no",
        "source_rcept_no",
        "disclosure_id",
        "id",
        "공시번호",
    ]

    SUMMARY_KEYS = [
        "summary",
        "cleaned_summary",
        "cleaned_report",
        "body",
        "text",
        "content",
        "요약",
    ]

    @classmethod
    def extract(cls, unit: Dict[str, Any]) -> DartEvidence:
        dart_obj = recursive_find_first(unit, cls.DART_KEYS)
        if dart_obj is None:
            return DartEvidence.empty()

        reports = cls._extract_reports(dart_obj)
        exists = len(reports) > 0

        if not exists and isinstance(dart_obj, str) and clean_text(dart_obj):
            reports = [
                {
                    "report_name": "DART 공시",
                    "rcept_date": "",
                    "rcept_no": "",
                    "summary": clean_text(dart_obj, max_len=180),
                }
            ]
            exists = True

        dates = unique_keep_order([r.get("rcept_date", "") for r in reports if r.get("rcept_date")])
        return DartEvidence(
            exists=exists,
            cleaned_reports=reports[:3],
            disclosure_dates=", ".join(dates),
        )

    @classmethod
    def _extract_reports(cls, dart_obj: Any) -> List[Dict[str, Any]]:
        if isinstance(dart_obj, list):
            return [cls._normalize_report(x) for x in dart_obj if cls._normalize_report(x)][:3]

        if isinstance(dart_obj, dict):
            for key in cls.REPORT_LIST_KEYS:
                value = dart_obj.get(key)
                if isinstance(value, list):
                    reports = [cls._normalize_report(x) for x in value if cls._normalize_report(x)]
                    if reports:
                        return reports[:3]

            normalized = cls._normalize_report(dart_obj)
            if normalized:
                return [normalized]

        return []

    @classmethod
    def _normalize_report(cls, obj: Any) -> Dict[str, Any]:
        if obj is None:
            return {}

        if isinstance(obj, str):
            text = clean_text(obj, max_len=180)
            if not text:
                return {}
            return {
                "report_name": "DART 공시",
                "rcept_date": "",
                "rcept_no": "",
                "summary": text,
            }

        if not isinstance(obj, dict):
            return {}

        report_name = clean_text(first_non_empty_from_dict(obj, cls.REPORT_NAME_KEYS), max_len=80)
        rcept_date = clean_text(first_non_empty_from_dict(obj, cls.REPORT_DATE_KEYS))
        rcept_no = clean_text(first_non_empty_from_dict(obj, cls.REPORT_NO_KEYS))
        summary = clean_text(first_non_empty_from_dict(obj, cls.SUMMARY_KEYS), max_len=180)

        if not report_name and not summary:
            return {}

        return {
            "report_name": report_name or "DART 공시",
            "rcept_date": rcept_date,
            "rcept_no": rcept_no,
            "summary": summary,
        }


class MarketSignalExtractor:
    MARKET_KEYS = {
        "market",
        "market_evidence",
        "market_context",
        "price_context",
        "price_volume_context",
    }
    BOARD_KEYS = {
        "board",
        "board_evidence",
        "board_context",
        "community",
        "community_evidence",
    }

    RETURN_KEYS = {
        "return_pct",
        "daily_return",
        "ret",
        "price_return",
        "pct_change",
        "change_pct",
    }
    RESIDUAL_Z_KEYS = {
        "residual_z",
        "abnormal_return_z",
        "return_z",
        "z_return",
    }
    VOLUME_RATIO_KEYS = {
        "volume_ratio",
        "volume_spike_ratio",
        "vol_ratio",
        "거래량배율",
    }
    VOLUME_Z_KEYS = {
        "volume_z",
        "vol_z",
        "turnover_z",
    }
    COMMENT_COUNT_KEYS = {
        "comment_count",
        "comments_count",
        "board_comment_count",
        "reply_count",
    }
    POST_COUNT_KEYS = {
        "post_count",
        "posts_count",
        "thread_count",
        "board_post_count",
    }
    ACTIVITY_Z_KEYS = {
        "board_activity_z",
        "activity_z",
        "comment_z",
        "post_z",
    }

    @classmethod
    def extract(cls, unit: Dict[str, Any]) -> MarketSignal:
        market_obj = recursive_find_first(unit, cls.MARKET_KEYS) or {}
        board_obj = recursive_find_first(unit, cls.BOARD_KEYS) or {}

        return_pct = cls._find_numeric(market_obj, cls.RETURN_KEYS)
        residual_z = cls._find_numeric(market_obj, cls.RESIDUAL_Z_KEYS)
        volume_ratio = cls._find_numeric(market_obj, cls.VOLUME_RATIO_KEYS)
        volume_z = cls._find_numeric(market_obj, cls.VOLUME_Z_KEYS)

        if return_pct is None:
            return_pct = cls._find_numeric(unit, cls.RETURN_KEYS)
        if residual_z is None:
            residual_z = cls._find_numeric(unit, cls.RESIDUAL_Z_KEYS)
        if volume_ratio is None:
            volume_ratio = cls._find_numeric(unit, cls.VOLUME_RATIO_KEYS)
        if volume_z is None:
            volume_z = cls._find_numeric(unit, cls.VOLUME_Z_KEYS)

        price_label = cls._price_label(return_pct, residual_z)
        volume_label = cls._volume_label(volume_ratio, volume_z)
        board_label = cls._board_label(unit, board_obj)

        parts = []
        if price_label != "특이 없음":
            parts.append(price_label)
        if volume_label != "특이 없음":
            parts.append(volume_label)
        if not parts:
            signal_summary = "가격·거래량 특이 없음"
        else:
            signal_summary = " / ".join(parts)

        return MarketSignal(
            price_label=price_label,
            volume_label=volume_label,
            board_label=board_label,
            signal_summary=signal_summary,
        )

    @classmethod
    def _find_numeric(cls, obj: Any, keys: Set[str]) -> Optional[float]:
        value = recursive_find_first(obj, keys, max_depth=4)
        return safe_float(value)

    @classmethod
    def _price_label(cls, return_pct: Optional[float], residual_z: Optional[float]) -> str:
        if residual_z is not None:
            if residual_z >= 2.0:
                return "급등"
            if residual_z <= -2.0:
                return "급락"
            if abs(residual_z) >= 1.3:
                return "변동성 확대"

        if return_pct is not None:
            if return_pct >= 7.0:
                return "급등"
            if return_pct <= -7.0:
                return "급락"
            if abs(return_pct) >= 3.0:
                return "변동성 확대"

        return "특이 없음"

    @classmethod
    def _volume_label(cls, volume_ratio: Optional[float], volume_z: Optional[float]) -> str:
        if volume_z is not None and volume_z >= 2.0:
            return "거래 관심 증가"
        if volume_ratio is not None and volume_ratio >= 2.0:
            return "거래 관심 증가"
        return "특이 없음"

    @classmethod
    def _board_label(cls, unit: Dict[str, Any], board_obj: Any) -> str:
        comment_count = cls._find_numeric(board_obj, cls.COMMENT_COUNT_KEYS)
        post_count = cls._find_numeric(board_obj, cls.POST_COUNT_KEYS)
        activity_z = cls._find_numeric(board_obj, cls.ACTIVITY_Z_KEYS)

        if comment_count is None:
            comment_count = cls._find_numeric(unit, cls.COMMENT_COUNT_KEYS)
        if post_count is None:
            post_count = cls._find_numeric(unit, cls.POST_COUNT_KEYS)
        if activity_z is None:
            activity_z = cls._find_numeric(unit, cls.ACTIVITY_Z_KEYS)

        if activity_z is not None and activity_z >= 2.0:
            return "반응 증가"
        if comment_count is not None and comment_count >= 20:
            return "반응 증가"
        if post_count is not None and post_count >= 5:
            return "반응 증가"

        return "특이 없음"


# =============================================================================
# Evidence / News Path 정책
# =============================================================================


@dataclass
class EvidenceDecision:
    evidence_level: str
    news_path: str
    reason: str


class EvidencePolicy:
    @classmethod
    def decide(
        cls,
        dart: DartEvidence,
        stock_event_contexts: List[ContextMatch],
        macro_contexts: List[ContextMatch],
        gdelt_contexts: List[ContextMatch],
        profile: StockProfile,
        market_signal: MarketSignal,
    ) -> EvidenceDecision:
        has_direct_stock_event = any(ctx.is_direct_stock_event for ctx in stock_event_contexts)

        if dart.exists:
            return EvidenceDecision(
                evidence_level="high",
                news_path="contextual_stock_news",
                reason="DART evidence exists",
            )

        if has_direct_stock_event:
            return EvidenceDecision(
                evidence_level="high",
                news_path="contextual_stock_news",
                reason="direct stock event exists",
            )

        has_background_context = bool(macro_contexts or gdelt_contexts or stock_event_contexts)

        if profile.exists and has_background_context:
            return EvidenceDecision(
                evidence_level="medium",
                news_path="contextual_stock_news",
                reason="profile sensitivity tags matched with macro/sector/GDELT background context",
            )

        if market_signal.has_price_or_volume_signal:
            return EvidenceDecision(
                evidence_level="low",
                news_path="market_price_alert",
                reason="price/volume signal only; insufficient contextual evidence for LLM stock news",
            )

        return EvidenceDecision(
            evidence_level="low",
            news_path="market_price_alert",
            reason="insufficient evidence",
        )


# =============================================================================
# Context Card Builder
# =============================================================================


class StockNewsContextCardBuilder:
    def __init__(
        self,
        profile_repo: StockProfileRepository,
        macro_repo: EventContextRepository,
        stock_event_repo: EventContextRepository,
        gdelt_repo: EventContextRepository,
        macro_window_days: int,
        stock_event_window_days: int,
        gdelt_window_days: int,
    ):
        self.profile_repo = profile_repo
        self.macro_repo = macro_repo
        self.stock_event_repo = stock_event_repo
        self.gdelt_repo = gdelt_repo
        self.macro_window_days = macro_window_days
        self.stock_event_window_days = stock_event_window_days
        self.gdelt_window_days = gdelt_window_days

    def build(self, unit: Dict[str, Any], idx: int) -> Dict[str, Any]:
        event_id = EventUnitExtractor.event_id(unit, fallback_idx=idx)
        event_date = EventUnitExtractor.event_date(unit)
        stock_code = EventUnitExtractor.stock_code(unit)
        stock_name = EventUnitExtractor.stock_name(unit)

        profile = self.profile_repo.get(stock_code, event_date, fallback_name=stock_name)
        stock_name = profile.stock_name or stock_name

        dart = DartEvidenceExtractor.extract(unit)
        market_signal = MarketSignalExtractor.extract(unit)

        macro_contexts = self.macro_repo.find_macro_like_contexts(
            target_date=event_date,
            profile=profile,
            window_days=self.macro_window_days,
            limit=3,
        )

        gdelt_contexts = self.gdelt_repo.find_macro_like_contexts(
            target_date=event_date,
            profile=profile,
            window_days=self.gdelt_window_days,
            limit=3,
        )

        stock_event_contexts = self.stock_event_repo.find_stock_event_contexts(
            target_date=event_date,
            stock_code=stock_code,
            stock_name=stock_name,
            profile=profile,
            window_days=self.stock_event_window_days,
            limit=3,
        )

        decision = EvidencePolicy.decide(
            dart=dart,
            stock_event_contexts=stock_event_contexts,
            macro_contexts=macro_contexts,
            gdelt_contexts=gdelt_contexts,
            profile=profile,
            market_signal=market_signal,
        )

        candidate_type = clean_text(
            recursive_find_first(
                unit,
                {
                    "event_generation_candidate_type",
                    "candidate_type",
                    "event_type",
                    "type",
                },
                max_depth=3,
            )
        )

        generation_permissions = recursive_find_first(
            unit,
            {"generation_permissions", "permissions"},
            max_depth=3,
        )

        card = {
            "event_id": event_id,
            "date": event_date.isoformat() if event_date else "",
            "news_path": decision.news_path,
            "evidence_level": decision.evidence_level,
            "stock": {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "sector": profile.sector,
                "business_summary": profile.business_summary,
                "sensitivity_tags": profile.sensitivity_tags,
                "asset_personality": profile.asset_personality,
                "profile_year": profile.business_year,
                "profile_exists": profile.exists,
            },
            "evidence": {
                "dart": {
                    "exists": dart.exists,
                    "cleaned_reports": dart.cleaned_reports,
                    "disclosure_dates": dart.disclosure_dates,
                },
                "macro_context": [ctx.to_payload() for ctx in macro_contexts],
                "gdelt_context": [ctx.to_payload() for ctx in gdelt_contexts],
                "stock_event_context": [ctx.to_payload() for ctx in stock_event_contexts],
                "market_signal": {
                    "price_label": market_signal.price_label,
                    "volume_label": market_signal.volume_label,
                    "signal_summary": market_signal.signal_summary,
                },
            },
            "generation_guardrail": {
                "allowed_news_path": decision.news_path,
                "causation_rule": "macro/sector/GDELT context is background only. Do not claim direct causation unless DART or direct stock event evidence exists.",
                "community_rule": "Do not generate community reaction, community_seed, or discussion-board style text in pr06.",
                "raw_numeric_rule": "Do not expose raw numeric market fields in LLM prompt.",
                "low_evidence_rule": "low evidence cards must not be sent to LLM stock news generation.",
            },
            "diagnostics": {
                "decision_reason": decision.reason,
                "candidate_type": candidate_type,
                "generation_permissions_present": generation_permissions is not None,
                "board_label_not_for_news_generation": market_signal.board_label,
                "removed_from_payload": [
                    "source_threads",
                    "comments",
                    "raw_comment_text",
                    "comment_count",
                    "post_count",
                    "return_pct",
                    "residual_z",
                    "volume_ratio",
                    "volume_z",
                    "board_activity_score",
                ],
            },
        }

        return card


# =============================================================================
# Writer / Report
# =============================================================================


class ContextCardWriter:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.jsonl_path = output_dir / "stock_news_context_cards.jsonl"
        self.preview_path = output_dir / "stock_news_context_cards_preview.csv"
        self.report_path = output_dir / "stock_news_context_cards_report.txt"

    def write_all(self, cards: List[Dict[str, Any]], report: str) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(self.jsonl_path, cards)
        self._write_preview(cards)
        self.report_path.write_text(report, encoding="utf-8")

    def _write_preview(self, cards: List[Dict[str, Any]]) -> None:
        rows = []
        for card in cards:
            stock = card.get("stock", {})
            evidence = card.get("evidence", {})
            dart = evidence.get("dart", {})
            market = evidence.get("market_signal", {})
            diagnostics = card.get("diagnostics", {})

            rows.append(
                {
                    "event_id": card.get("event_id", ""),
                    "date": card.get("date", ""),
                    "stock_code": stock.get("stock_code", ""),
                    "stock_name": stock.get("stock_name", ""),
                    "sector": stock.get("sector", ""),
                    "news_path": card.get("news_path", ""),
                    "evidence_level": card.get("evidence_level", ""),
                    "dart_exists": dart.get("exists", False),
                    "macro_context_count": len(evidence.get("macro_context", [])),
                    "gdelt_context_count": len(evidence.get("gdelt_context", [])),
                    "stock_event_context_count": len(evidence.get("stock_event_context", [])),
                    "price_label": market.get("price_label", ""),
                    "volume_label": market.get("volume_label", ""),
                    "board_label_not_for_news_generation": diagnostics.get(
                        "board_label_not_for_news_generation", ""
                    ),
                    "decision_reason": diagnostics.get("decision_reason", ""),
                    "business_summary": clean_text(stock.get("business_summary", ""), max_len=120),
                    "sensitivity_tags": "|".join(stock.get("sensitivity_tags", [])[:8]),
                }
            )

        pd.DataFrame(rows).to_csv(self.preview_path, index=False, encoding="utf-8-sig")


class ReportBuilder:
    def __init__(
        self,
        path_config: PathConfig,
        profile_repo: StockProfileRepository,
        macro_repo: EventContextRepository,
        stock_event_repo: EventContextRepository,
        gdelt_repo: EventContextRepository,
    ):
        self.path_config = path_config
        self.profile_repo = profile_repo
        self.macro_repo = macro_repo
        self.stock_event_repo = stock_event_repo
        self.gdelt_repo = gdelt_repo

    def build(self, cards: List[Dict[str, Any]]) -> str:
        total = len(cards)
        evidence_counter = Counter(card.get("evidence_level", "") for card in cards)
        path_counter = Counter(card.get("news_path", "") for card in cards)

        dart_count = sum(1 for card in cards if card["evidence"]["dart"]["exists"])
        direct_stock_event_count = 0
        macro_context_count = 0
        gdelt_context_count = 0
        stock_event_context_count = 0
        profile_exists_count = 0

        for card in cards:
            evidence = card.get("evidence", {})
            stock = card.get("stock", {})

            if stock.get("profile_exists"):
                profile_exists_count += 1

            macro_context_count += len(evidence.get("macro_context", []))
            gdelt_context_count += len(evidence.get("gdelt_context", []))
            stock_event_contexts = evidence.get("stock_event_context", [])
            stock_event_context_count += len(stock_event_contexts)

            for ctx in stock_event_contexts:
                if ctx.get("scope") == "stock_specific":
                    direct_stock_event_count += 1

        low_blocked = sum(
            1
            for card in cards
            if card.get("evidence_level") == "low"
            and card.get("news_path") == "market_price_alert"
        )

        warnings = []
        for repo in [self.profile_repo, self.macro_repo, self.stock_event_repo, self.gdelt_repo]:
            if getattr(repo, "load_warning", ""):
                warnings.append(repo.load_warning)

        contextual_examples = [
            card
            for card in cards
            if card.get("news_path") == "contextual_stock_news"
        ][:10]

        alert_examples = [
            card
            for card in cards
            if card.get("news_path") == "market_price_alert"
        ][:10]

        lines = []
        lines.append("# Stock News Context Cards Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- event_units_path: {self.path_config.event_units_path}")
        lines.append(f"- macro_event_csv: {self.path_config.macro_event_csv}")
        lines.append(f"- stock_event_csv: {self.path_config.stock_event_csv}")
        lines.append(f"- gdelt_context_path: {self.path_config.gdelt_context_path}")
        lines.append(f"- stock_profile_csv: {self.path_config.stock_profile_csv}")
        lines.append("")
        lines.append("## Output")
        lines.append(f"- output_dir: {self.path_config.output_dir}")
        lines.append(f"- stock_news_context_cards.jsonl")
        lines.append(f"- stock_news_context_cards_preview.csv")
        lines.append(f"- stock_news_context_cards_report.txt")
        lines.append("")
        lines.append("## Counts")
        lines.append(f"- total_cards: {total}")
        lines.append(f"- profile_exists_count: {profile_exists_count}")
        lines.append(f"- dart_evidence_count: {dart_count}")
        lines.append(f"- direct_stock_event_count: {direct_stock_event_count}")
        lines.append(f"- macro_context_total: {macro_context_count}")
        lines.append(f"- gdelt_context_total: {gdelt_context_count}")
        lines.append(f"- stock_event_context_total: {stock_event_context_count}")
        lines.append(f"- low_evidence_blocked_as_market_price_alert: {low_blocked}")
        lines.append("")
        lines.append("## Evidence Level")
        for key, value in sorted(evidence_counter.items()):
            lines.append(f"- {key}: {value}")
        lines.append("")
        lines.append("## News Path")
        for key, value in sorted(path_counter.items()):
            lines.append(f"- {key}: {value}")
        lines.append("")
        lines.append("## Warnings")
        if warnings:
            for warning in warnings:
                lines.append(f"- {warning}")
        else:
            lines.append("- none")
        lines.append("")
        lines.append("## Guardrail Check")
        lines.append("- source_threads/comments are not included in output cards.")
        lines.append("- raw numeric market fields are converted to coarse labels.")
        lines.append("- low evidence cards are assigned to market_price_alert.")
        lines.append("- macro/sector/GDELT context has usage_rule=background_only_no_direct_causation.")
        lines.append("")
        lines.append("## Contextual Stock News Examples")
        for card in contextual_examples:
            lines.append(
                f"- {card.get('date')} / {card['stock'].get('stock_code')} "
                f"{card['stock'].get('stock_name')} / {card.get('evidence_level')} / "
                f"{card['diagnostics'].get('decision_reason')}"
            )
        lines.append("")
        lines.append("## Market Price Alert Examples")
        for card in alert_examples:
            lines.append(
                f"- {card.get('date')} / {card['stock'].get('stock_code')} "
                f"{card['stock'].get('stock_name')} / {card.get('evidence_level')} / "
                f"{card['diagnostics'].get('decision_reason')}"
            )
        lines.append("")

        return "\n".join(lines)


# =============================================================================
# Pipeline
# =============================================================================


class StockNewsContextCardPipeline:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.path_config = PathConfig.from_args(args)

        self.profile_repo = StockProfileRepository(self.path_config.stock_profile_csv)
        self.macro_repo = EventContextRepository(self.path_config.macro_event_csv, "macro_event")
        self.stock_event_repo = EventContextRepository(
            self.path_config.stock_event_csv,
            "stock_event",
        )
        self.gdelt_repo = EventContextRepository(self.path_config.gdelt_context_path, "GDELT")

        self.builder = StockNewsContextCardBuilder(
            profile_repo=self.profile_repo,
            macro_repo=self.macro_repo,
            stock_event_repo=self.stock_event_repo,
            gdelt_repo=self.gdelt_repo,
            macro_window_days=args.macro_window_days,
            stock_event_window_days=args.stock_event_window_days,
            gdelt_window_days=args.gdelt_window_days,
        )

        self.writer = ContextCardWriter(self.path_config.output_dir)
        self.report_builder = ReportBuilder(
            path_config=self.path_config,
            profile_repo=self.profile_repo,
            macro_repo=self.macro_repo,
            stock_event_repo=self.stock_event_repo,
            gdelt_repo=self.gdelt_repo,
        )

    def run(self) -> None:
        if not self.path_config.event_units_path.exists():
            raise FileNotFoundError(f"event_units_path 없음: {self.path_config.event_units_path}")

        units = read_jsonl(self.path_config.event_units_path, limit=self.args.limit)
        cards = []

        for idx, unit in enumerate(units, start=1):
            card = self.builder.build(unit, idx)
            cards.append(card)

        report = self.report_builder.build(cards)
        self.writer.write_all(cards, report)

        print("=" * 100)
        print("[pr05a stock news context cards 생성 완료]")
        print(f"input_units: {len(units)}")
        print(f"output_cards: {len(cards)}")
        print(f"jsonl: {self.writer.jsonl_path}")
        print(f"preview: {self.writer.preview_path}")
        print(f"report: {self.writer.report_path}")
        print("=" * 100)


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build stock news context cards for pr06 from pr05 event_thread_units.jsonl"
    )

    parser.add_argument(
        "--project-root",
        type=str,
        default=".",
        help="프로젝트 루트 경로",
    )
    parser.add_argument(
        "--event-units-path",
        type=str,
        default=None,
        help="pr05 출력 event_thread_units.jsonl 경로",
    )
    parser.add_argument(
        "--macro-event-csv",
        type=str,
        default=None,
        help="거시 이벤트 CSV 경로. 없으면 자동 탐색 후 optional skip",
    )
    parser.add_argument(
        "--stock-event-csv",
        type=str,
        default=None,
        help="주식/업종 이벤트 CSV 경로. 없으면 자동 탐색 후 optional skip",
    )
    parser.add_argument(
        "--gdelt-context-path",
        type=str,
        default=None,
        help="GDELT 요약 context CSV/JSONL 경로. raw GDELT가 아니라 summary 권장",
    )
    parser.add_argument(
        "--stock-profile-csv",
        type=str,
        default=None,
        help="종목 프로필 CSV 경로. 없으면 자동 탐색 후 optional skip",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="출력 디렉토리",
    )
    parser.add_argument(
        "--macro-window-days",
        type=int,
        default=3,
        help="macro event를 event date 이전 며칠까지 볼지",
    )
    parser.add_argument(
        "--stock-event-window-days",
        type=int,
        default=7,
        help="stock event를 event date 이전 며칠까지 볼지",
    )
    parser.add_argument(
        "--gdelt-window-days",
        type=int,
        default=3,
        help="GDELT context를 event date 이전 며칠까지 볼지",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="디버깅용 처리 row 제한",
    )

    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    pipeline = StockNewsContextCardPipeline(args)
    pipeline.run()


if __name__ == "__main__":
    main()