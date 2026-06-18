#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pr05c_build_stock_news_generation_inputs.py

Purpose
-------
Build stock-date level evidence bundles for stock-specific news generation.

This script merges:
1. judged GDELT stock context cards
2. structured stock event calendar
3. structured macro event calendar
4. DART disclosure results JSON
5. stock price-volume Excel data

It does NOT call LLM.
It does NOT generate final news.
It builds compact evidence bundles and evaluates whether a stock-date can be used
as a candidate for stock-specific news generation.

Recommended command
-------------------
python scripts/processors/pr05c_build_stock_news_generation_inputs.py \
  --judged-context-csv "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/gdelt_context/gdelt_stock_context_cards_judged_v3_all_fixed.csv" \
  --stock-event-csv "/Users/hgs/Desktop/IISE CD/data/raw/market_event/stock_event_calendar_2013_2023.csv" \
  --macro-event-csv "/Users/hgs/Desktop/IISE CD/data/raw/market_event/macro_event_calendar_2013_2023.csv" \
  --dart-json "/Users/hgs/Desktop/IISE CD/news_generator/dart_collector/dart_results_2013_2024.json" \
  --price-volume-xlsx "/Users/hgs/Desktop/IISE CD/data/raw/stock/stock_price-volume_npq.xlsx" \
  --output-jsonl "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/stock_news_generation_inputs/stock_news_candidate_bundles_v1.jsonl" \
  --report-txt "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/stock_news_generation_inputs/stock_news_candidate_bundles_v1_report.txt" \
  --start-date 2018-07-01 \
  --end-date 2018-07-31
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


# =============================================================================
# Constants
# =============================================================================

DIRECT_GDELT_DECISIONS = {"accept_direct", "accept_rule_direct"}
INDIRECT_GDELT_DECISIONS = {"accept_indirect"}
BACKGROUND_GDELT_DECISIONS = {"background_only"}
REJECT_GDELT_DECISIONS = {"reject", "do_not_use", "missing_llm_result"}
DO_NOT_USE_ALLOWED_USAGE = {"do_not_use"}

DEFAULT_GDELT_CONTEXT_FIELDS = [
    "context_id",
    "theme",
    "context_theme",
    "summary",
    "context_summary",
    "evidence_class",
    "final_decision",
    "final_allowed_usage",
    "rule_decision",
    "llm_decision",
    "attach_score",
    "gdelt_signal_strength",
    "context_stock_link_score",
    "llm_confidence",
    "raw_count",
    "weighted_count",
    "avg_tone",
    "tone_score",
    "matched_stock_tags",
    "matched_profile_terms",
    "llm_reason_ko",
    "llm_reason",
    "source_event_ids",
    "source_urls",
    "domain",
    "source_name",
]

NUMERIC_SORT_COLUMNS = [
    "attach_score",
    "gdelt_signal_strength",
    "context_stock_link_score",
    "llm_confidence",
    "raw_count",
    "weighted_count",
    "avg_tone",
    "tone_score",
]

PRICE_VOLUME_NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "return_1d",
    "return_z",
    "volume_z",
    "turnover",
    "amount",
]

DATE_COLUMN_CANDIDATES = [
    "date",
    "ref_date",
    "event_date",
    "base_date",
    "trading_date",
    "published_at",
    "rcept_date",
    "source_rcept_date",
    "business_summary_asof",
]

STOCK_CODE_COLUMN_CANDIDATES = [
    "stock_code",
    "종목코드",
    "code",
    "ticker",
    "symbol",
]

STOCK_NAME_COLUMN_CANDIDATES = [
    "stock_name",
    "종목명",
    "name",
    "corp_name",
    "corp",
]

EVENT_TYPE_COLUMN_CANDIDATES = [
    "event_type",
    "type",
    "category",
    "event_category",
    "source_type",
]

EVENT_NAME_COLUMN_CANDIDATES = [
    "event_name",
    "event_title",
    "title",
    "headline",
    "name",
    "source_event_name",
]

SUMMARY_COLUMN_CANDIDATES = [
    "summary",
    "detail",
    "description",
    "content",
    "event_summary",
    "detail_news",
    "body",
]

DART_REPORT_NAME_CANDIDATES = [
    "report_name",
    "source_report_name",
    "rpt_nm",
    "report_nm",
]

DART_RCEPT_NO_CANDIDATES = [
    "rcept_no",
    "source_rcept_no",
]

MACRO_KEEP_FIELDS = [
    "date",
    "event_name",
    "event_type",
    "direction",
    "severity",
    "summary",
    "related_assets",
    "asset_class",
    "source_event_id",
    "source",
]

STOCK_EVENT_KEEP_FIELDS = [
    "date",
    "stock_code",
    "stock_name",
    "event_name",
    "event_type",
    "direction",
    "severity",
    "summary",
    "source_event_id",
    "source",
]

DART_KEEP_FIELDS = [
    "date",
    "stock_code",
    "stock_name",
    "corp_code",
    "corp_name",
    "report_name",
    "rcept_no",
    "rcept_date",
    "event_type",
    "summary",
    "source",
]


# =============================================================================
# Utility
# =============================================================================

class JsonSafeConverter:
    """Convert pandas/numpy values into JSON-safe Python values."""

    @staticmethod
    def value(value: Any) -> Any:
        if value is None:
            return None

        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass

        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None

        if isinstance(value, Path):
            return str(value)

        return value

    @classmethod
    def dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        for key, value in data.items():
            clean_value = cls.value(value)
            if clean_value is not None:
                result[key] = clean_value

        return result


class ColumnHelper:
    """Resolve optional columns safely."""

    @staticmethod
    def first_existing(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
        columns = set(df.columns)

        for candidate in candidates:
            if candidate in columns:
                return candidate

        lower_map = {str(col).lower(): col for col in df.columns}
        for candidate in candidates:
            key = candidate.lower()
            if key in lower_map:
                return lower_map[key]

        return None

    @staticmethod
    def ensure_column(df: pd.DataFrame, column: str, default: Any = None) -> pd.DataFrame:
        result = df.copy()
        if column not in result.columns:
            result[column] = default
        return result

    @staticmethod
    def require(df: pd.DataFrame, required: Sequence[str], name: str) -> None:
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"{name} missing required columns: {missing}")


class DateNormalizer:
    """Normalize date-like values into YYYY-MM-DD strings."""

    @staticmethod
    def normalize_series(series: pd.Series) -> pd.Series:
        s = series.copy()

        # Handle yyyymmdd numeric/string.
        s_str = s.astype("string").str.strip()
        yyyymmdd_mask = s_str.str.match(r"^\d{8}$", na=False)

        result = pd.Series(index=s.index, dtype="string")

        if yyyymmdd_mask.any():
            result.loc[yyyymmdd_mask] = pd.to_datetime(
                s_str.loc[yyyymmdd_mask],
                format="%Y%m%d",
                errors="coerce",
            ).dt.strftime("%Y-%m-%d")

        if (~yyyymmdd_mask).any():
            result.loc[~yyyymmdd_mask] = pd.to_datetime(
                s.loc[~yyyymmdd_mask],
                errors="coerce",
            ).dt.strftime("%Y-%m-%d")

        return result

    @staticmethod
    def normalize_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        series = pd.Series([value])
        normalized = DateNormalizer.normalize_series(series).iloc[0]
        if pd.isna(normalized):
            return None
        return str(normalized)


class StockCodeNormalizer:
    """Normalize stock codes to 6-digit strings."""

    @staticmethod
    def normalize_series(series: pd.Series) -> pd.Series:
        s = series.astype("string").str.strip()

        # Remove decimal suffix from Excel numeric-like code: 005930.0
        s = s.str.replace(r"\.0$", "", regex=True)

        # Remove non-alphanumeric spaces but keep digits.
        s = s.str.replace(r"\s+", "", regex=True)

        # Extract last 6 digits if a code has prefixes.
        extracted = s.str.extract(r"(\d{6})", expand=False)
        s = extracted.fillna(s)

        return s.str.zfill(6)

    @staticmethod
    def normalize_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        series = pd.Series([value])
        normalized = StockCodeNormalizer.normalize_series(series).iloc[0]
        if pd.isna(normalized):
            return None
        return str(normalized)


class CsvReader:
    """Read CSV with common Korean encodings fallback."""

    @staticmethod
    def read(path: Path) -> pd.DataFrame:
        encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]

        last_error: Optional[Exception] = None

        for encoding in encodings:
            try:
                return pd.read_csv(path, encoding=encoding)
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"Failed to read CSV: {path}. Last error: {last_error}")


class JsonlWriter:
    """Write UTF-8 JSONL."""

    @staticmethod
    def write(path: Path, records: Sequence[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# =============================================================================
# Config
# =============================================================================

@dataclass(frozen=True)
class BundleBuildConfig:
    judged_context_csv: Path
    stock_event_csv: Optional[Path]
    macro_event_csv: Optional[Path]
    dart_json: Optional[Path]
    price_volume_xlsx: Optional[Path]

    output_jsonl: Path
    report_txt: Path

    start_date: Optional[str] = None
    end_date: Optional[str] = None

    max_gdelt_direct_contexts: int = 3
    max_gdelt_indirect_contexts: int = 3
    max_gdelt_background_contexts: int = 2
    max_macro_events: int = 3
    max_stock_events: int = 3
    max_dart_events: int = 3

    price_return_z_threshold: float = 2.0
    volume_z_threshold: float = 2.0
    min_abs_return_for_price_move: float = 0.03

    include_ineligible_bundles: bool = True
    build_from_event_only_rows: bool = True


# =============================================================================
# Base normalizer
# =============================================================================

class EventDataFrameNormalizer:
    """Normalize arbitrary event dataframe into canonical columns."""

    def __init__(
        self,
        source_name: str,
        require_stock_code: bool,
    ):
        self.source_name = source_name
        self.require_stock_code = require_stock_code

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        date_col = ColumnHelper.first_existing(result, DATE_COLUMN_CANDIDATES)
        if date_col is None:
            raise ValueError(f"{self.source_name}: no date-like column found.")

        result["date"] = DateNormalizer.normalize_series(result[date_col])

        stock_code_col = ColumnHelper.first_existing(result, STOCK_CODE_COLUMN_CANDIDATES)
        stock_name_col = ColumnHelper.first_existing(result, STOCK_NAME_COLUMN_CANDIDATES)
        event_type_col = ColumnHelper.first_existing(result, EVENT_TYPE_COLUMN_CANDIDATES)
        event_name_col = ColumnHelper.first_existing(result, EVENT_NAME_COLUMN_CANDIDATES)
        summary_col = ColumnHelper.first_existing(result, SUMMARY_COLUMN_CANDIDATES)

        if self.require_stock_code:
            if stock_code_col is None:
                raise ValueError(f"{self.source_name}: no stock_code-like column found.")
            result["stock_code"] = StockCodeNormalizer.normalize_series(result[stock_code_col])
        else:
            if stock_code_col is not None:
                result["stock_code"] = StockCodeNormalizer.normalize_series(result[stock_code_col])

        if stock_name_col is not None:
            result["stock_name"] = result[stock_name_col].astype("string").fillna("")
        elif "stock_name" not in result.columns:
            result["stock_name"] = ""

        if event_type_col is not None:
            result["event_type"] = result[event_type_col].astype("string").fillna("")
        elif "event_type" not in result.columns:
            result["event_type"] = ""

        if event_name_col is not None:
            result["event_name"] = result[event_name_col].astype("string").fillna("")
        elif "event_name" not in result.columns:
            result["event_name"] = ""

        if summary_col is not None:
            result["summary"] = result[summary_col].astype("string").fillna("")
        elif "summary" not in result.columns:
            result["summary"] = ""

        if "direction" not in result.columns:
            result["direction"] = ""

        if "severity" not in result.columns:
            result["severity"] = ""

        if "related_assets" not in result.columns:
            result["related_assets"] = ""

        if "asset_class" not in result.columns:
            result["asset_class"] = ""

        if "source_event_id" not in result.columns:
            result["source_event_id"] = ""

        result["source"] = self.source_name

        result = result[result["date"].notna()].copy()

        if self.require_stock_code:
            result = result[result["stock_code"].notna()].copy()

        return result


# =============================================================================
# GDELT loader
# =============================================================================

class GdeltJudgedContextLoader:
    """Load judged GDELT stock context cards."""

    def __init__(self, config: BundleBuildConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.judged_context_csv

        if not path.exists():
            raise FileNotFoundError(f"GDELT judged context CSV not found: {path}")

        df = CsvReader.read(path)

        ColumnHelper.require(
            df,
            required=["date", "stock_code", "final_decision", "final_allowed_usage"],
            name="GDELT judged context",
        )

        result = df.copy()
        result["date"] = DateNormalizer.normalize_series(result["date"])
        result["stock_code"] = StockCodeNormalizer.normalize_series(result["stock_code"])

        if "stock_name" not in result.columns:
            result["stock_name"] = ""

        if "business_year" not in result.columns:
            result["business_year"] = ""

        result["final_decision"] = (
            result["final_decision"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        result["final_allowed_usage"] = (
            result["final_allowed_usage"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        for col in NUMERIC_SORT_COLUMNS:
            if col not in result.columns:
                result[col] = 0.0
            result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0.0)

        result = result[result["date"].notna()].copy()
        result = self._filter_date_range(result)

        result["gdelt_context_type"] = result.apply(self._classify_context_type, axis=1)

        return result

    def _filter_date_range(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        if self.config.start_date:
            result = result[result["date"] >= self.config.start_date].copy()

        if self.config.end_date:
            result = result[result["date"] <= self.config.end_date].copy()

        return result

    @staticmethod
    def _classify_context_type(row: pd.Series) -> str:
        final_decision = str(row.get("final_decision", "")).strip().lower()
        final_allowed_usage = str(row.get("final_allowed_usage", "")).strip().lower()

        if final_allowed_usage in DO_NOT_USE_ALLOWED_USAGE:
            return "excluded"

        if final_decision in DIRECT_GDELT_DECISIONS:
            return "direct"

        if final_decision in INDIRECT_GDELT_DECISIONS:
            return "indirect"

        if final_decision in BACKGROUND_GDELT_DECISIONS:
            return "background"

        if final_decision in REJECT_GDELT_DECISIONS:
            return "excluded"

        return "excluded"


# =============================================================================
# Stock event loader
# =============================================================================

class StockEventLoader:
    """Load stock-specific event calendar."""

    def __init__(self, config: BundleBuildConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.stock_event_csv

        if path is None or not path.exists():
            return self._empty()

        df = CsvReader.read(path)
        normalizer = EventDataFrameNormalizer(
            source_name="stock_event",
            require_stock_code=True,
        )
        result = normalizer.normalize(df)
        result = self._filter_date_range(result)

        return result

    def _filter_date_range(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        if self.config.start_date:
            result = result[result["date"] >= self.config.start_date].copy()

        if self.config.end_date:
            result = result[result["date"] <= self.config.end_date].copy()

        return result

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(columns=STOCK_EVENT_KEEP_FIELDS)


# =============================================================================
# Macro event loader
# =============================================================================

class MacroEventLoader:
    """Load macro event calendar."""

    def __init__(self, config: BundleBuildConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.macro_event_csv

        if path is None or not path.exists():
            return self._empty()

        df = CsvReader.read(path)
        normalizer = EventDataFrameNormalizer(
            source_name="macro_event",
            require_stock_code=False,
        )
        result = normalizer.normalize(df)
        result = self._filter_date_range(result)

        return result

    def _filter_date_range(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        if self.config.start_date:
            result = result[result["date"] >= self.config.start_date].copy()

        if self.config.end_date:
            result = result[result["date"] <= self.config.end_date].copy()

        return result

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(columns=MACRO_KEEP_FIELDS)


# =============================================================================
# DART loader
# =============================================================================

class DartJsonLoader:
    """Load DART disclosure JSON and normalize to stock-date events."""

    def __init__(self, config: BundleBuildConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.dart_json

        if path is None or not path.exists():
            return self._empty()

        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        records = self._flatten_json(raw)

        if not records:
            return self._empty()

        df = pd.DataFrame(records)
        result = self._normalize(df)
        result = self._filter_date_range(result)

        return result

    def _flatten_json(self, raw: Any) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    records.append(item)
            return records

        if isinstance(raw, dict):
            # Common shape: {"results": [...]} or {"data": [...]}.
            for key in ["results", "data", "items", "list", "records"]:
                value = raw.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            records.append(item)
                    return records

            # Shape: {"005930": [..], "000660": [..]}
            for key, value in raw.items():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            row = dict(item)
                            if "stock_code" not in row:
                                row["stock_code"] = key
                            records.append(row)

            if records:
                return records

            # Single record dict.
            records.append(raw)

        return records

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        date_col = ColumnHelper.first_existing(result, DATE_COLUMN_CANDIDATES)
        stock_code_col = ColumnHelper.first_existing(result, STOCK_CODE_COLUMN_CANDIDATES)
        stock_name_col = ColumnHelper.first_existing(result, STOCK_NAME_COLUMN_CANDIDATES)
        report_name_col = ColumnHelper.first_existing(result, DART_REPORT_NAME_CANDIDATES)
        rcept_no_col = ColumnHelper.first_existing(result, DART_RCEPT_NO_CANDIDATES)
        summary_col = ColumnHelper.first_existing(result, SUMMARY_COLUMN_CANDIDATES)

        if date_col is None:
            raise ValueError("DART JSON: no date-like column found.")

        if stock_code_col is None:
            raise ValueError("DART JSON: no stock_code-like column found.")

        result["date"] = DateNormalizer.normalize_series(result[date_col])
        result["rcept_date"] = result["date"]
        result["stock_code"] = StockCodeNormalizer.normalize_series(result[stock_code_col])

        if stock_name_col is not None:
            result["stock_name"] = result[stock_name_col].astype("string").fillna("")
        elif "stock_name" not in result.columns:
            result["stock_name"] = ""

        if "corp_name" not in result.columns:
            if stock_name_col is not None:
                result["corp_name"] = result[stock_name_col].astype("string").fillna("")
            else:
                result["corp_name"] = ""

        if "corp_code" not in result.columns:
            result["corp_code"] = ""

        if report_name_col is not None:
            result["report_name"] = result[report_name_col].astype("string").fillna("")
        elif "report_name" not in result.columns:
            result["report_name"] = ""

        if rcept_no_col is not None:
            result["rcept_no"] = result[rcept_no_col].astype("string").fillna("")
        elif "rcept_no" not in result.columns:
            result["rcept_no"] = ""

        if summary_col is not None:
            result["summary"] = result[summary_col].astype("string").fillna("")
        elif "summary" not in result.columns:
            result["summary"] = result["report_name"]

        result["event_type"] = result["report_name"].apply(self._infer_event_type)
        result["source"] = "dart"

        result = result[result["date"].notna()].copy()
        result = result[result["stock_code"].notna()].copy()

        return result

    @staticmethod
    def _infer_event_type(report_name: Any) -> str:
        text = str(report_name)

        rules = [
            ("contract", ["계약", "판매", "공급"]),
            ("earnings", ["잠정실적", "영업실적", "매출액", "손익구조"]),
            ("financing", ["유상증자", "무상증자", "전환사채", "신주인수권", "차입", "사채"]),
            ("shareholder", ["주주총회", "배당", "주식분할", "자기주식"]),
            ("investment", ["타법인", "출자", "취득", "처분", "투자"]),
            ("management", ["대표이사", "임원", "최대주주", "경영"]),
            ("risk", ["소송", "벌금", "제재", "횡령", "배임"]),
        ]

        for event_type, keywords in rules:
            if any(keyword in text for keyword in keywords):
                return event_type

        return "disclosure"

    def _filter_date_range(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        if self.config.start_date:
            result = result[result["date"] >= self.config.start_date].copy()

        if self.config.end_date:
            result = result[result["date"] <= self.config.end_date].copy()

        return result

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(columns=DART_KEEP_FIELDS)


# =============================================================================
# Price-volume loader
# =============================================================================

class PriceVolumeExcelLoader:
    """
    Load stock price-volume Excel data.

    This loader tries to support both:
    1. long format:
       date, stock_code, stock_name, close, volume, ...
    2. wide format:
       one date column and many stock-related columns.

    If the wide format cannot be interpreted, the script still runs, but
    price-volume context will be empty. The report will expose this.
    """

    def __init__(self, config: BundleBuildConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.price_volume_xlsx

        if path is None or not path.exists():
            return self._empty()

        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")

        normalized_frames: List[pd.DataFrame] = []

        for sheet_name, sheet_df in sheets.items():
            if sheet_df.empty:
                continue

            df = self._clean_excel_frame(sheet_df)

            if df.empty:
                continue

            normalized = self._try_normalize_sheet(df, sheet_name=sheet_name)

            if not normalized.empty:
                normalized_frames.append(normalized)

        if not normalized_frames:
            return self._empty()

        result = pd.concat(normalized_frames, ignore_index=True)
        result = self._postprocess(result)
        result = self._filter_date_range(result)

        return result

    def _clean_excel_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result = result.dropna(axis=0, how="all").dropna(axis=1, how="all")
        result.columns = [str(col).strip() for col in result.columns]
        return result

    def _try_normalize_sheet(self, df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
        long_df = self._try_long_format(df, sheet_name=sheet_name)
        if not long_df.empty:
            return long_df

        wide_df = self._try_wide_format(df, sheet_name=sheet_name)
        if not wide_df.empty:
            return wide_df

        return self._empty()

    def _try_long_format(self, df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
        date_col = ColumnHelper.first_existing(df, DATE_COLUMN_CANDIDATES)
        stock_code_col = ColumnHelper.first_existing(df, STOCK_CODE_COLUMN_CANDIDATES)

        if date_col is None or stock_code_col is None:
            return self._empty()

        result = df.copy()
        result["date"] = DateNormalizer.normalize_series(result[date_col])
        result["stock_code"] = StockCodeNormalizer.normalize_series(result[stock_code_col])

        stock_name_col = ColumnHelper.first_existing(result, STOCK_NAME_COLUMN_CANDIDATES)
        if stock_name_col is not None:
            result["stock_name"] = result[stock_name_col].astype("string").fillna("")
        elif "stock_name" not in result.columns:
            result["stock_name"] = ""

        result = self._standardize_price_volume_columns(result)
        result["source"] = f"price_volume:{sheet_name}"

        result = result[result["date"].notna()].copy()
        result = result[result["stock_code"].notna()].copy()

        keep_cols = [
            "date",
            "stock_code",
            "stock_name",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover",
            "source",
        ]

        for col in keep_cols:
            if col not in result.columns:
                result[col] = None

        return result[keep_cols].copy()

    def _try_wide_format(self, df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
        date_col = ColumnHelper.first_existing(df, DATE_COLUMN_CANDIDATES)

        if date_col is None:
            # Try first column as date if it parses well.
            candidate = df.columns[0]
            parsed = DateNormalizer.normalize_series(df[candidate])
            parse_ratio = parsed.notna().mean()
            if parse_ratio >= 0.7:
                date_col = candidate
            else:
                return self._empty()

        result = df.copy()
        result["date"] = DateNormalizer.normalize_series(result[date_col])
        result = result[result["date"].notna()].copy()

        value_cols = [col for col in result.columns if col != date_col and col != "date"]

        records: List[Dict[str, Any]] = []

        for col in value_cols:
            stock_code = self._extract_stock_code_from_column(col)
            if stock_code is None:
                continue

            metric = self._infer_metric_from_column(col, sheet_name=sheet_name)
            if metric is None:
                continue

            temp = pd.DataFrame(
                {
                    "date": result["date"],
                    "stock_code": stock_code,
                    metric: result[col],
                    "source": f"price_volume:{sheet_name}",
                }
            )
            records.extend(temp.to_dict(orient="records"))

        if not records:
            return self._empty()

        long_metrics = pd.DataFrame(records)
        value_metric_cols = [
            col for col in ["open", "high", "low", "close", "volume", "amount", "turnover"]
            if col in long_metrics.columns
        ]

        if not value_metric_cols:
            return self._empty()

        grouped = (
            long_metrics
            .groupby(["date", "stock_code"], as_index=False)
            .agg({col: "first" for col in value_metric_cols + ["source"]})
        )

        grouped["stock_name"] = ""

        return grouped

    def _extract_stock_code_from_column(self, col: Any) -> Optional[str]:
        text = str(col)
        match = re.search(r"(\d{6})", text)
        if not match:
            return None
        return match.group(1)

    def _infer_metric_from_column(self, col: Any, sheet_name: str) -> Optional[str]:
        text = f"{sheet_name} {col}".lower()

        if any(key in text for key in ["volume", "거래량", "vol"]):
            return "volume"

        if any(key in text for key in ["amount", "거래대금", "value"]):
            return "amount"

        if any(key in text for key in ["turnover", "회전율"]):
            return "turnover"

        if any(key in text for key in ["open", "시가"]):
            return "open"

        if any(key in text for key in ["high", "고가"]):
            return "high"

        if any(key in text for key in ["low", "저가"]):
            return "low"

        if any(key in text for key in ["close", "종가", "price", "adj close", "수정종가"]):
            return "close"

        # If sheet name is price-like and column has stock code only, treat value as close.
        if any(key in text for key in ["price", "close", "종가", "stock_price"]):
            return "close"

        return None

    def _standardize_price_volume_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        mapping_candidates = {
            "open": ["open", "시가"],
            "high": ["high", "고가"],
            "low": ["low", "저가"],
            "close": ["close", "종가", "adj close", "수정종가", "price"],
            "volume": ["volume", "거래량", "vol"],
            "amount": ["amount", "거래대금"],
            "turnover": ["turnover", "회전율"],
        }

        lower_map = {str(col).lower(): col for col in result.columns}

        for target, candidates in mapping_candidates.items():
            if target in result.columns:
                continue

            for candidate in candidates:
                if candidate in result.columns:
                    result[target] = result[candidate]
                    break

                lower_candidate = candidate.lower()
                if lower_candidate in lower_map:
                    result[target] = result[lower_map[lower_candidate]]
                    break

        return result

    def _postprocess(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        result["date"] = DateNormalizer.normalize_series(result["date"])
        result["stock_code"] = StockCodeNormalizer.normalize_series(result["stock_code"])

        if "stock_name" not in result.columns:
            result["stock_name"] = ""

        for col in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
            if col not in result.columns:
                result[col] = None
            result[col] = pd.to_numeric(result[col], errors="coerce")

        result = result.sort_values(["stock_code", "date"], kind="mergesort").copy()

        if "close" in result.columns:
            result["return_1d"] = (
                result.groupby("stock_code")["close"]
                .pct_change()
                .replace([float("inf"), float("-inf")], pd.NA)
            )
        else:
            result["return_1d"] = pd.NA

        result["return_z"] = self._rolling_zscore(
            result,
            group_col="stock_code",
            value_col="return_1d",
            window=60,
        )

        if "volume" in result.columns:
            result["volume_z"] = self._rolling_zscore(
                result,
                group_col="stock_code",
                value_col="volume",
                window=60,
            )
        else:
            result["volume_z"] = pd.NA

        result["is_price_move_significant"] = (
            (result["return_z"].abs() >= self.config.price_return_z_threshold)
            | (result["return_1d"].abs() >= self.config.min_abs_return_for_price_move)
        ).fillna(False)

        result["is_volume_spike"] = (
            result["volume_z"] >= self.config.volume_z_threshold
        ).fillna(False)

        result["has_abnormal_price_volume"] = (
            result["is_price_move_significant"] | result["is_volume_spike"]
        ).fillna(False)

        result["market_reaction_label"] = result.apply(self._reaction_label, axis=1)

        keep_cols = [
            "date",
            "stock_code",
            "stock_name",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover",
            "return_1d",
            "return_z",
            "volume_z",
            "is_price_move_significant",
            "is_volume_spike",
            "has_abnormal_price_volume",
            "market_reaction_label",
            "source",
        ]

        for col in keep_cols:
            if col not in result.columns:
                result[col] = None

        return result[keep_cols].drop_duplicates(["date", "stock_code"], keep="last")

    def _rolling_zscore(
        self,
        df: pd.DataFrame,
        group_col: str,
        value_col: str,
        window: int,
    ) -> pd.Series:
        values = pd.to_numeric(df[value_col], errors="coerce")

        rolling_mean = (
            values
            .groupby(df[group_col])
            .transform(lambda s: s.rolling(window=window, min_periods=20).mean())
        )

        rolling_std = (
            values
            .groupby(df[group_col])
            .transform(lambda s: s.rolling(window=window, min_periods=20).std())
        )

        z = (values - rolling_mean) / rolling_std.replace(0, pd.NA)
        return z.replace([float("inf"), float("-inf")], pd.NA)

    @staticmethod
    def _reaction_label(row: pd.Series) -> str:
        ret = row.get("return_1d")
        price_sig = bool(row.get("is_price_move_significant", False))
        vol_sig = bool(row.get("is_volume_spike", False))

        if not price_sig and not vol_sig:
            return "normal"

        if pd.isna(ret):
            if vol_sig:
                return "volume_spike"
            return "abnormal"

        if ret > 0 and vol_sig:
            return "price_up_volume_spike"

        if ret < 0 and vol_sig:
            return "price_down_volume_spike"

        if ret > 0:
            return "price_up"

        if ret < 0:
            return "price_down"

        if vol_sig:
            return "volume_spike"

        return "abnormal"

    def _filter_date_range(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()

        if self.config.start_date:
            result = result[result["date"] >= self.config.start_date].copy()

        if self.config.end_date:
            result = result[result["date"] <= self.config.end_date].copy()

        return result

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "date",
                "stock_code",
                "stock_name",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "turnover",
                "return_1d",
                "return_z",
                "volume_z",
                "is_price_move_significant",
                "is_volume_spike",
                "has_abnormal_price_volume",
                "market_reaction_label",
                "source",
            ]
        )


# =============================================================================
# Context item builders
# =============================================================================

class GdeltContextItemBuilder:
    """Build compact GDELT context item."""

    def build(self, row: pd.Series) -> Dict[str, Any]:
        item: Dict[str, Any] = {"source": "gdelt"}

        for field in DEFAULT_GDELT_CONTEXT_FIELDS:
            if field in row.index:
                value = JsonSafeConverter.value(row[field])
                if value is not None:
                    item[field] = value

        if "theme" not in item and "context_theme" in item:
            item["theme"] = item["context_theme"]

        if "summary" not in item and "context_summary" in item:
            item["summary"] = item["context_summary"]

        return JsonSafeConverter.dict(item)


class EventContextItemBuilder:
    """Build compact event context items."""

    def build_macro(self, row: pd.Series) -> Dict[str, Any]:
        return self._build(row, keep_fields=MACRO_KEEP_FIELDS, source="macro_event")

    def build_stock_event(self, row: pd.Series) -> Dict[str, Any]:
        return self._build(row, keep_fields=STOCK_EVENT_KEEP_FIELDS, source="stock_event")

    def build_dart(self, row: pd.Series) -> Dict[str, Any]:
        return self._build(row, keep_fields=DART_KEEP_FIELDS, source="dart")

    def build_price_volume(self, row: Optional[pd.Series]) -> Dict[str, Any]:
        if row is None:
            return {}

        keep_fields = [
            "date",
            "stock_code",
            "stock_name",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover",
            "return_1d",
            "return_z",
            "volume_z",
            "is_price_move_significant",
            "is_volume_spike",
            "has_abnormal_price_volume",
            "market_reaction_label",
            "source",
        ]

        item = self._build(row, keep_fields=keep_fields, source="price_volume")
        return item

    def _build(self, row: pd.Series, keep_fields: Sequence[str], source: str) -> Dict[str, Any]:
        item: Dict[str, Any] = {"source": source}

        for field in keep_fields:
            if field in row.index:
                value = JsonSafeConverter.value(row[field])
                if value is not None:
                    item[field] = value

        return JsonSafeConverter.dict(item)


# =============================================================================
# Sorting
# =============================================================================

class GdeltContextSorter:
    """Sort GDELT contexts by type-specific priority."""

    @staticmethod
    def direct(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        result = df.copy()
        result["direct_priority"] = result["final_decision"].map(
            {
                "accept_rule_direct": 2,
                "accept_direct": 1,
            }
        ).fillna(0)

        return result.sort_values(
            by=[
                "direct_priority",
                "attach_score",
                "gdelt_signal_strength",
                "context_stock_link_score",
                "raw_count",
            ],
            ascending=[False, False, False, False, False],
            kind="mergesort",
        )

    @staticmethod
    def indirect(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        return df.sort_values(
            by=[
                "attach_score",
                "gdelt_signal_strength",
                "llm_confidence",
                "context_stock_link_score",
                "raw_count",
            ],
            ascending=[False, False, False, False, False],
            kind="mergesort",
        )

    @staticmethod
    def background(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        return df.sort_values(
            by=[
                "gdelt_signal_strength",
                "raw_count",
                "weighted_count",
                "attach_score",
            ],
            ascending=[False, False, False, False],
            kind="mergesort",
        )


class EventSorter:
    """Sort structured events."""

    @staticmethod
    def stock_events(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        result = df.copy()

        if "severity" not in result.columns:
            result["severity"] = 0

        result["severity_num"] = pd.to_numeric(result["severity"], errors="coerce").fillna(0)

        return result.sort_values(
            by=["severity_num", "event_type", "event_name"],
            ascending=[False, True, True],
            kind="mergesort",
        )

    @staticmethod
    def macro_events(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        result = df.copy()

        if "severity" not in result.columns:
            result["severity"] = 0

        result["severity_num"] = pd.to_numeric(result["severity"], errors="coerce").fillna(0)

        return result.sort_values(
            by=["severity_num", "event_type", "event_name"],
            ascending=[False, True, True],
            kind="mergesort",
        )

    @staticmethod
    def dart_events(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        result = df.copy()
        priority_map = {
            "risk": 7,
            "earnings": 6,
            "contract": 5,
            "financing": 4,
            "investment": 3,
            "shareholder": 2,
            "management": 1,
            "disclosure": 0,
        }

        result["dart_priority"] = result["event_type"].map(priority_map).fillna(0)

        return result.sort_values(
            by=["dart_priority", "report_name"],
            ascending=[False, True],
            kind="mergesort",
        )


# =============================================================================
# Eligibility
# =============================================================================

class GenerationEligibilityEvaluator:
    """
    Evaluate whether the bundle can be used for stock-specific news generation.

    Conservative rules:
    - stock_event or DART is strong stock-specific evidence.
    - direct GDELT can generate.
    - indirect GDELT requires corroboration.
    - macro-only, background-only, price-volume-only cannot generate.
    """

    @staticmethod
    def evaluate(
        gdelt_direct_count: int,
        gdelt_indirect_count: int,
        gdelt_background_count: int,
        stock_event_count: int,
        dart_event_count: int,
        macro_event_count: int,
        has_price_volume: bool,
        has_abnormal_price_volume: bool,
    ) -> Dict[str, Any]:
        if stock_event_count >= 1:
            return {
                "can_generate_stock_news": True,
                "reason": "has_stock_event",
                "evidence_level": "stock_specific_event",
                "requires_corroboration": False,
            }

        if dart_event_count >= 1:
            return {
                "can_generate_stock_news": True,
                "reason": "has_dart_context",
                "evidence_level": "stock_specific_disclosure",
                "requires_corroboration": False,
            }

        if gdelt_direct_count >= 1:
            return {
                "can_generate_stock_news": True,
                "reason": "has_direct_gdelt_context",
                "evidence_level": "direct_sector_theme",
                "requires_corroboration": False,
            }

        if gdelt_indirect_count >= 1 and has_abnormal_price_volume:
            return {
                "can_generate_stock_news": True,
                "reason": "indirect_gdelt_with_price_volume_corroboration",
                "evidence_level": "corroborated_indirect",
                "requires_corroboration": False,
            }

        if gdelt_indirect_count >= 1 and macro_event_count >= 1:
            return {
                "can_generate_stock_news": False,
                "reason": "macro_and_indirect_only_requires_stock_specific_evidence",
                "evidence_level": "weak_context",
                "requires_corroboration": True,
            }

        if gdelt_indirect_count >= 1:
            return {
                "can_generate_stock_news": False,
                "reason": "only_indirect_gdelt_requires_corroboration",
                "evidence_level": "weak_context",
                "requires_corroboration": True,
            }

        if gdelt_background_count >= 1:
            return {
                "can_generate_stock_news": False,
                "reason": "only_background_context",
                "evidence_level": "background_only",
                "requires_corroboration": True,
            }

        if has_abnormal_price_volume:
            return {
                "can_generate_stock_news": False,
                "reason": "only_price_volume_reaction",
                "evidence_level": "market_reaction_only",
                "requires_corroboration": True,
            }

        if macro_event_count >= 1:
            return {
                "can_generate_stock_news": False,
                "reason": "only_macro_event",
                "evidence_level": "macro_background_only",
                "requires_corroboration": True,
            }

        return {
            "can_generate_stock_news": False,
            "reason": "no_usable_evidence",
            "evidence_level": "none",
            "requires_corroboration": True,
        }


# =============================================================================
# Bundle Builder
# =============================================================================

class EvidenceBundleBuilder:
    """Build stock-date evidence bundles."""

    def __init__(
        self,
        config: BundleBuildConfig,
        gdelt_df: pd.DataFrame,
        stock_event_df: pd.DataFrame,
        macro_event_df: pd.DataFrame,
        dart_df: pd.DataFrame,
        price_volume_df: pd.DataFrame,
    ):
        self.config = config
        self.gdelt_df = gdelt_df
        self.stock_event_df = stock_event_df
        self.macro_event_df = macro_event_df
        self.dart_df = dart_df
        self.price_volume_df = price_volume_df

        self.gdelt_item_builder = GdeltContextItemBuilder()
        self.event_item_builder = EventContextItemBuilder()

    def build(self) -> List[Dict[str, Any]]:
        keys = self._build_stock_date_keys()
        bundles: List[Dict[str, Any]] = []

        gdelt_grouped = self._group_by_stock_date(self.gdelt_df)
        stock_event_grouped = self._group_by_stock_date(self.stock_event_df)
        dart_grouped = self._group_by_stock_date(self.dart_df)
        price_volume_grouped = self._group_by_stock_date(self.price_volume_df)
        macro_grouped = self._group_by_date(self.macro_event_df)

        for date, stock_code in sorted(keys):
            gdelt_group = gdelt_grouped.get((date, stock_code), self._empty_gdelt())
            stock_group = stock_event_grouped.get((date, stock_code), self._empty_stock_event())
            dart_group = dart_grouped.get((date, stock_code), self._empty_dart())
            price_volume_group = price_volume_grouped.get((date, stock_code), self._empty_price_volume())
            macro_group = macro_grouped.get(date, self._empty_macro())

            bundle = self._build_one_bundle(
                date=date,
                stock_code=stock_code,
                gdelt_group=gdelt_group,
                stock_group=stock_group,
                dart_group=dart_group,
                price_volume_group=price_volume_group,
                macro_group=macro_group,
            )

            if not self.config.include_ineligible_bundles:
                if not bundle["generation_eligibility"]["can_generate_stock_news"]:
                    continue

            if bundle["evidence_counts"]["total_usable_evidence"] <= 0:
                continue

            bundles.append(bundle)

        return bundles

    def _build_stock_date_keys(self) -> set[Tuple[str, str]]:
        keys: set[Tuple[str, str]] = set()

        for df in [self.gdelt_df, self.stock_event_df, self.dart_df]:
            if df.empty:
                continue

            if "date" not in df.columns or "stock_code" not in df.columns:
                continue

            for row in df[["date", "stock_code"]].dropna().itertuples(index=False):
                keys.add((str(row.date), str(row.stock_code)))

        if self.config.build_from_event_only_rows and not self.price_volume_df.empty:
            abnormal = self.price_volume_df[
                self.price_volume_df["has_abnormal_price_volume"].fillna(False)
            ].copy()

            if not abnormal.empty:
                for row in abnormal[["date", "stock_code"]].dropna().itertuples(index=False):
                    keys.add((str(row.date), str(row.stock_code)))

        return keys

    def _build_one_bundle(
        self,
        date: str,
        stock_code: str,
        gdelt_group: pd.DataFrame,
        stock_group: pd.DataFrame,
        dart_group: pd.DataFrame,
        price_volume_group: pd.DataFrame,
        macro_group: pd.DataFrame,
    ) -> Dict[str, Any]:
        gdelt_direct_df = gdelt_group[gdelt_group["gdelt_context_type"] == "direct"].copy()
        gdelt_indirect_df = gdelt_group[gdelt_group["gdelt_context_type"] == "indirect"].copy()
        gdelt_background_df = gdelt_group[gdelt_group["gdelt_context_type"] == "background"].copy()
        gdelt_excluded_df = gdelt_group[gdelt_group["gdelt_context_type"] == "excluded"].copy()

        gdelt_direct_df = GdeltContextSorter.direct(gdelt_direct_df)
        gdelt_indirect_df = GdeltContextSorter.indirect(gdelt_indirect_df)
        gdelt_background_df = GdeltContextSorter.background(gdelt_background_df)

        stock_group = EventSorter.stock_events(stock_group)
        macro_group = EventSorter.macro_events(macro_group)
        dart_group = EventSorter.dart_events(dart_group)

        price_volume_row = self._select_price_volume_row(price_volume_group)

        gdelt_direct_contexts = self._build_gdelt_items(
            gdelt_direct_df.head(self.config.max_gdelt_direct_contexts)
        )
        gdelt_indirect_contexts = self._build_gdelt_items(
            gdelt_indirect_df.head(self.config.max_gdelt_indirect_contexts)
        )
        gdelt_background_contexts = self._build_gdelt_items(
            gdelt_background_df.head(self.config.max_gdelt_background_contexts)
        )

        stock_event_contexts = self._build_stock_event_items(
            stock_group.head(self.config.max_stock_events)
        )
        macro_event_contexts = self._build_macro_items(
            macro_group.head(self.config.max_macro_events)
        )
        dart_contexts = self._build_dart_items(
            dart_group.head(self.config.max_dart_events)
        )
        price_volume_context = self.event_item_builder.build_price_volume(price_volume_row)

        has_price_volume = bool(price_volume_context)
        has_abnormal_price_volume = bool(
            price_volume_context.get("has_abnormal_price_volume", False)
        )

        gdelt_direct_count = len(gdelt_direct_df)
        gdelt_indirect_count = len(gdelt_indirect_df)
        gdelt_background_count = len(gdelt_background_df)
        stock_event_count = len(stock_group)
        dart_event_count = len(dart_group)
        macro_event_count = len(macro_group)

        eligibility = GenerationEligibilityEvaluator.evaluate(
            gdelt_direct_count=gdelt_direct_count,
            gdelt_indirect_count=gdelt_indirect_count,
            gdelt_background_count=gdelt_background_count,
            stock_event_count=stock_event_count,
            dart_event_count=dart_event_count,
            macro_event_count=macro_event_count,
            has_price_volume=has_price_volume,
            has_abnormal_price_volume=has_abnormal_price_volume,
        )

        stock_name = self._resolve_stock_name(
            gdelt_group=gdelt_group,
            stock_group=stock_group,
            dart_group=dart_group,
            price_volume_group=price_volume_group,
        )

        business_year = self._first_non_null(gdelt_group, "business_year", default="")

        evidence_counts = {
            "gdelt_direct": gdelt_direct_count,
            "gdelt_indirect": gdelt_indirect_count,
            "gdelt_background": gdelt_background_count,
            "gdelt_excluded": len(gdelt_excluded_df),
            "stock_events": stock_event_count,
            "macro_events": macro_event_count,
            "dart_events": dart_event_count,
            "has_price_volume": has_price_volume,
            "has_abnormal_price_volume": has_abnormal_price_volume,
            "total_usable_evidence": (
                gdelt_direct_count
                + gdelt_indirect_count
                + gdelt_background_count
                + stock_event_count
                + macro_event_count
                + dart_event_count
                + int(has_price_volume)
            ),
        }

        bundle = {
            "date": date,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "business_year": business_year,
            "source_scope": "gdelt_stock_event_macro_event_dart_price_volume",
            "gdelt_contexts": {
                "direct_contexts": gdelt_direct_contexts,
                "indirect_contexts": gdelt_indirect_contexts,
                "background_contexts": gdelt_background_contexts,
            },
            "stock_event_contexts": stock_event_contexts,
            "macro_event_contexts": macro_event_contexts,
            "dart_contexts": dart_contexts,
            "price_volume_context": price_volume_context,
            "evidence_counts": evidence_counts,
            "generation_eligibility": eligibility,
        }

        return JsonSafeConverter.dict(bundle)

    def _select_price_volume_row(self, group: pd.DataFrame) -> Optional[pd.Series]:
        if group.empty:
            return None

        result = group.copy()

        result["abnormal_priority"] = result["has_abnormal_price_volume"].fillna(False).astype(int)
        result["volume_priority"] = result["is_volume_spike"].fillna(False).astype(int)
        result["price_priority"] = result["is_price_move_significant"].fillna(False).astype(int)
        result["abs_return_z"] = pd.to_numeric(result["return_z"], errors="coerce").abs().fillna(0)
        result["volume_z_safe"] = pd.to_numeric(result["volume_z"], errors="coerce").fillna(0)

        result = result.sort_values(
            by=[
                "abnormal_priority",
                "price_priority",
                "volume_priority",
                "abs_return_z",
                "volume_z_safe",
            ],
            ascending=[False, False, False, False, False],
            kind="mergesort",
        )

        return result.iloc[0]

    def _build_gdelt_items(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        return [self.gdelt_item_builder.build(row) for _, row in df.iterrows()]

    def _build_stock_event_items(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        return [self.event_item_builder.build_stock_event(row) for _, row in df.iterrows()]

    def _build_macro_items(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        return [self.event_item_builder.build_macro(row) for _, row in df.iterrows()]

    def _build_dart_items(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        return [self.event_item_builder.build_dart(row) for _, row in df.iterrows()]

    def _resolve_stock_name(
        self,
        gdelt_group: pd.DataFrame,
        stock_group: pd.DataFrame,
        dart_group: pd.DataFrame,
        price_volume_group: pd.DataFrame,
    ) -> str:
        for df in [stock_group, dart_group, gdelt_group, price_volume_group]:
            value = self._first_non_null(df, "stock_name", default="")
            if value:
                return str(value)
        return ""

    @staticmethod
    def _first_non_null(df: pd.DataFrame, col: str, default: Any = None) -> Any:
        if df.empty or col not in df.columns:
            return default

        values = df[col].dropna()
        if values.empty:
            return default

        value = values.iloc[0]
        value = JsonSafeConverter.value(value)

        if value is None:
            return default

        return value

    @staticmethod
    def _group_by_stock_date(df: pd.DataFrame) -> Dict[Tuple[str, str], pd.DataFrame]:
        if df.empty or "date" not in df.columns or "stock_code" not in df.columns:
            return {}

        groups: Dict[Tuple[str, str], pd.DataFrame] = {}

        for key, group in df.groupby(["date", "stock_code"], dropna=False, sort=False):
            groups[(str(key[0]), str(key[1]))] = group.copy()

        return groups

    @staticmethod
    def _group_by_date(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        if df.empty or "date" not in df.columns:
            return {}

        groups: Dict[str, pd.DataFrame] = {}

        for date, group in df.groupby("date", dropna=False, sort=False):
            groups[str(date)] = group.copy()

        return groups

    @staticmethod
    def _empty_gdelt() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "date",
                "stock_code",
                "stock_name",
                "business_year",
                "gdelt_context_type",
                "final_decision",
                "final_allowed_usage",
            ]
            + NUMERIC_SORT_COLUMNS
        )

    @staticmethod
    def _empty_stock_event() -> pd.DataFrame:
        return pd.DataFrame(columns=STOCK_EVENT_KEEP_FIELDS)

    @staticmethod
    def _empty_macro() -> pd.DataFrame:
        return pd.DataFrame(columns=MACRO_KEEP_FIELDS)

    @staticmethod
    def _empty_dart() -> pd.DataFrame:
        return pd.DataFrame(columns=DART_KEEP_FIELDS)

    @staticmethod
    def _empty_price_volume() -> pd.DataFrame:
        return PriceVolumeExcelLoader._empty()


# =============================================================================
# Report
# =============================================================================

class ReportBuilder:
    """Build report text."""

    def __init__(
        self,
        config: BundleBuildConfig,
        gdelt_df: pd.DataFrame,
        stock_event_df: pd.DataFrame,
        macro_event_df: pd.DataFrame,
        dart_df: pd.DataFrame,
        price_volume_df: pd.DataFrame,
        bundles: Sequence[Dict[str, Any]],
    ):
        self.config = config
        self.gdelt_df = gdelt_df
        self.stock_event_df = stock_event_df
        self.macro_event_df = macro_event_df
        self.dart_df = dart_df
        self.price_volume_df = price_volume_df
        self.bundles = list(bundles)

    def write(self) -> None:
        self.config.report_txt.parent.mkdir(parents=True, exist_ok=True)
        self.config.report_txt.write_text(self.build_text(), encoding="utf-8")

    def build_text(self) -> str:
        lines: List[str] = []

        lines.append("# pr05c Stock News Evidence Bundle Report")
        lines.append("")
        lines.append("## Inputs")
        lines.append(f"- judged_context_csv: {self.config.judged_context_csv}")
        lines.append(f"- stock_event_csv: {self.config.stock_event_csv}")
        lines.append(f"- macro_event_csv: {self.config.macro_event_csv}")
        lines.append(f"- dart_json: {self.config.dart_json}")
        lines.append(f"- price_volume_xlsx: {self.config.price_volume_xlsx}")
        lines.append("")
        lines.append("## Outputs")
        lines.append(f"- output_jsonl: {self.config.output_jsonl}")
        lines.append(f"- report_txt: {self.config.report_txt}")
        lines.append("")
        lines.append("## Date Filter")
        lines.append(f"- start_date: {self.config.start_date}")
        lines.append(f"- end_date: {self.config.end_date}")
        lines.append("")
        lines.append("## Loaded Rows")
        lines.append(f"- gdelt_rows: {len(self.gdelt_df)}")
        lines.append(f"- stock_event_rows: {len(self.stock_event_df)}")
        lines.append(f"- macro_event_rows: {len(self.macro_event_df)}")
        lines.append(f"- dart_rows: {len(self.dart_df)}")
        lines.append(f"- price_volume_rows: {len(self.price_volume_df)}")
        lines.append("")

        self._append_gdelt_distribution(lines)
        self._append_price_volume_summary(lines)
        self._append_bundle_summary(lines)
        self._append_top_tables(lines)
        self._append_samples(lines)

        return "\n".join(lines).rstrip() + "\n"

    def _append_gdelt_distribution(self, lines: List[str]) -> None:
        lines.append("## GDELT Context Type Distribution")

        if self.gdelt_df.empty:
            lines.append("- no gdelt rows")
            lines.append("")
            return

        for key, value in self.gdelt_df["gdelt_context_type"].value_counts(dropna=False).items():
            lines.append(f"- {key}: {value}")

        lines.append("")
        lines.append("## GDELT final_decision Distribution")
        for key, value in self.gdelt_df["final_decision"].value_counts(dropna=False).items():
            lines.append(f"- {key}: {value}")

        lines.append("")

    def _append_price_volume_summary(self, lines: List[str]) -> None:
        lines.append("## Price-Volume Summary")

        if self.price_volume_df.empty:
            lines.append("- no price-volume rows parsed")
            lines.append("")
            return

        abnormal_count = int(self.price_volume_df["has_abnormal_price_volume"].fillna(False).sum())
        price_move_count = int(self.price_volume_df["is_price_move_significant"].fillna(False).sum())
        volume_spike_count = int(self.price_volume_df["is_volume_spike"].fillna(False).sum())

        lines.append(f"- rows: {len(self.price_volume_df)}")
        lines.append(f"- abnormal_price_volume_rows: {abnormal_count}")
        lines.append(f"- price_move_rows: {price_move_count}")
        lines.append(f"- volume_spike_rows: {volume_spike_count}")

        lines.append("")
        lines.append("### market_reaction_label Distribution")
        for key, value in self.price_volume_df["market_reaction_label"].value_counts(dropna=False).items():
            lines.append(f"- {key}: {value}")

        lines.append("")

    def _append_bundle_summary(self, lines: List[str]) -> None:
        bundle_df = self._bundle_df()

        lines.append("## Bundle Summary")
        lines.append(f"- bundle_count: {len(bundle_df)}")

        if bundle_df.empty:
            lines.append("")
            return

        lines.append(f"- can_generate_true: {int(bundle_df['can_generate_stock_news'].sum())}")
        lines.append(f"- can_generate_false: {int((~bundle_df['can_generate_stock_news']).sum())}")
        lines.append(f"- unique_dates: {bundle_df['date'].nunique()}")
        lines.append(f"- unique_stocks: {bundle_df['stock_code'].nunique()}")
        lines.append("")

        lines.append("## Eligibility Reason Distribution")
        for key, value in bundle_df["eligibility_reason"].value_counts(dropna=False).items():
            lines.append(f"- {key}: {value}")

        lines.append("")
        lines.append("## Evidence Count Summary")
        evidence_cols = [
            "gdelt_direct",
            "gdelt_indirect",
            "gdelt_background",
            "stock_events",
            "macro_events",
            "dart_events",
            "has_price_volume",
            "has_abnormal_price_volume",
            "total_usable_evidence",
        ]

        for col in evidence_cols:
            if col in bundle_df.columns:
                lines.append(
                    f"- {col}: "
                    f"sum={bundle_df[col].sum()}, "
                    f"mean={bundle_df[col].mean():.3f}, "
                    f"max={bundle_df[col].max()}"
                )

        lines.append("")

    def _append_top_tables(self, lines: List[str]) -> None:
        bundle_df = self._bundle_df()

        if bundle_df.empty:
            return

        lines.append("## Top Stocks By Bundle Count")
        stock_count = (
            bundle_df
            .groupby(["stock_code", "stock_name"], dropna=False)
            .size()
            .reset_index(name="bundle_count")
            .sort_values("bundle_count", ascending=False)
            .head(20)
        )
        lines.extend(self._records_as_bullets(stock_count))
        lines.append("")

        lines.append("## Top Dates By Bundle Count")
        date_count = (
            bundle_df
            .groupby("date", dropna=False)
            .size()
            .reset_index(name="bundle_count")
            .sort_values("bundle_count", ascending=False)
            .head(20)
        )
        lines.extend(self._records_as_bullets(date_count))
        lines.append("")

        lines.append("## Top Stocks By Generatable Bundle Count")
        gen = bundle_df[bundle_df["can_generate_stock_news"]].copy()

        if gen.empty:
            lines.append("- no generatable bundles")
        else:
            gen_count = (
                gen
                .groupby(["stock_code", "stock_name"], dropna=False)
                .size()
                .reset_index(name="generatable_bundle_count")
                .sort_values("generatable_bundle_count", ascending=False)
                .head(20)
            )
            lines.extend(self._records_as_bullets(gen_count))

        lines.append("")

    def _append_samples(self, lines: List[str]) -> None:
        lines.append("## Sample Bundles")
        lines.append("")

        sample_reasons = [
            "has_stock_event",
            "has_dart_context",
            "has_direct_gdelt_context",
            "indirect_gdelt_with_price_volume_corroboration",
            "only_indirect_gdelt_requires_corroboration",
            "only_price_volume_reaction",
        ]

        for reason in sample_reasons:
            lines.append(f"### {reason}")
            samples = [
                bundle for bundle in self.bundles
                if bundle["generation_eligibility"]["reason"] == reason
            ][:3]

            if not samples:
                lines.append("- no sample")
                lines.append("")
                continue

            for bundle in samples:
                compact = self._compact_bundle(bundle)
                lines.append("```json")
                lines.append(json.dumps(compact, ensure_ascii=False, indent=2, default=str))
                lines.append("```")

            lines.append("")

    def _bundle_df(self) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []

        for bundle in self.bundles:
            eligibility = bundle.get("generation_eligibility", {})
            counts = bundle.get("evidence_counts", {})

            row = {
                "date": bundle.get("date"),
                "stock_code": bundle.get("stock_code"),
                "stock_name": bundle.get("stock_name"),
                "can_generate_stock_news": bool(eligibility.get("can_generate_stock_news", False)),
                "eligibility_reason": eligibility.get("reason"),
                "evidence_level": eligibility.get("evidence_level"),
            }

            row.update(counts)
            rows.append(row)

        return pd.DataFrame(rows)

    def _compact_bundle(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        gdelt_contexts = bundle.get("gdelt_contexts", {})

        return {
            "date": bundle.get("date"),
            "stock_code": bundle.get("stock_code"),
            "stock_name": bundle.get("stock_name"),
            "generation_eligibility": bundle.get("generation_eligibility"),
            "evidence_counts": bundle.get("evidence_counts"),
            "gdelt_direct_themes": self._extract_gdelt_themes(
                gdelt_contexts.get("direct_contexts", [])
            ),
            "gdelt_indirect_themes": self._extract_gdelt_themes(
                gdelt_contexts.get("indirect_contexts", [])
            ),
            "stock_event_names": self._extract_event_names(
                bundle.get("stock_event_contexts", [])
            ),
            "dart_report_names": [
                item.get("report_name")
                for item in bundle.get("dart_contexts", [])
                if item.get("report_name")
            ],
            "price_volume_context": bundle.get("price_volume_context", {}),
            "macro_event_names": self._extract_event_names(
                bundle.get("macro_event_contexts", [])
            ),
        }

    @staticmethod
    def _extract_gdelt_themes(contexts: Sequence[Dict[str, Any]]) -> List[str]:
        values: List[str] = []
        for item in contexts:
            value = item.get("theme") or item.get("summary") or item.get("context_theme")
            if value:
                values.append(str(value))
        return values[:5]

    @staticmethod
    def _extract_event_names(contexts: Sequence[Dict[str, Any]]) -> List[str]:
        values: List[str] = []
        for item in contexts:
            value = item.get("event_name") or item.get("summary")
            if value:
                values.append(str(value))
        return values[:5]

    @staticmethod
    def _records_as_bullets(df: pd.DataFrame) -> List[str]:
        if df.empty:
            return ["- no records"]

        lines: List[str] = []
        for record in df.to_dict(orient="records"):
            text = ", ".join(f"{k}={v}" for k, v in record.items())
            lines.append(f"- {text}")
        return lines


# =============================================================================
# Pipeline
# =============================================================================

class Pr05cPipeline:
    """End-to-end pr05c pipeline."""

    def __init__(self, config: BundleBuildConfig):
        self.config = config

    def run(self) -> None:
        gdelt_df = GdeltJudgedContextLoader(self.config).load()
        stock_event_df = StockEventLoader(self.config).load()
        macro_event_df = MacroEventLoader(self.config).load()
        dart_df = DartJsonLoader(self.config).load()
        price_volume_df = PriceVolumeExcelLoader(self.config).load()

        builder = EvidenceBundleBuilder(
            config=self.config,
            gdelt_df=gdelt_df,
            stock_event_df=stock_event_df,
            macro_event_df=macro_event_df,
            dart_df=dart_df,
            price_volume_df=price_volume_df,
        )
        bundles = builder.build()

        JsonlWriter.write(self.config.output_jsonl, bundles)

        report = ReportBuilder(
            config=self.config,
            gdelt_df=gdelt_df,
            stock_event_df=stock_event_df,
            macro_event_df=macro_event_df,
            dart_df=dart_df,
            price_volume_df=price_volume_df,
            bundles=bundles,
        )
        report.write()

        self._print_summary(
            gdelt_df=gdelt_df,
            stock_event_df=stock_event_df,
            macro_event_df=macro_event_df,
            dart_df=dart_df,
            price_volume_df=price_volume_df,
            bundles=bundles,
        )

    def _print_summary(
        self,
        gdelt_df: pd.DataFrame,
        stock_event_df: pd.DataFrame,
        macro_event_df: pd.DataFrame,
        dart_df: pd.DataFrame,
        price_volume_df: pd.DataFrame,
        bundles: Sequence[Dict[str, Any]],
    ) -> None:
        generatable_count = sum(
            1 for bundle in bundles
            if bundle["generation_eligibility"]["can_generate_stock_news"]
        )

        reason_counts: Dict[str, int] = {}
        for bundle in bundles:
            reason = bundle["generation_eligibility"]["reason"]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        print("=" * 100)
        print("[pr05c 완료]")
        print("")
        print("[input rows]")
        print(f"gdelt_rows: {len(gdelt_df)}")
        print(f"stock_event_rows: {len(stock_event_df)}")
        print(f"macro_event_rows: {len(macro_event_df)}")
        print(f"dart_rows: {len(dart_df)}")
        print(f"price_volume_rows: {len(price_volume_df)}")
        print("")
        print("[output]")
        print(f"output_jsonl: {self.config.output_jsonl}")
        print(f"report_txt: {self.config.report_txt}")
        print(f"bundle_count: {len(bundles)}")
        print(f"can_generate_stock_news=True: {generatable_count}")
        print(f"can_generate_stock_news=False: {len(bundles) - generatable_count}")
        print("")
        print("[eligibility reasons]")
        for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"{reason}: {count}")
        print("=" * 100)


# =============================================================================
# CLI
# =============================================================================

class CliParser:
    """CLI parser factory."""

    @staticmethod
    def build() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Build stock-date evidence bundles for stock news generation."
        )

        parser.add_argument(
            "--judged-context-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/gdelt_context/"
                "gdelt_stock_context_cards_judged_v3_all_fixed.csv"
            ),
        )

        parser.add_argument(
            "--stock-event-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/market_event/"
                "stock_event_calendar_2013_2023.csv"
            ),
        )

        parser.add_argument(
            "--macro-event-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/market_event/"
                "macro_event_calendar_2013_2023.csv"
            ),
        )

        parser.add_argument(
            "--dart-json",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/news_generator/dart_collector/"
                "dart_results_2013_2024.json"
            ),
        )

        parser.add_argument(
            "--price-volume-xlsx",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/stock/"
                "stock_price-volume_npq.xlsx"
            ),
        )

        parser.add_argument(
            "--output-jsonl",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/"
                "stock_news_generation_inputs/stock_news_candidate_bundles_v1.jsonl"
            ),
        )

        parser.add_argument(
            "--report-txt",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/"
                "stock_news_generation_inputs/stock_news_candidate_bundles_v1_report.txt"
            ),
        )

        parser.add_argument("--start-date", type=str, default=None)
        parser.add_argument("--end-date", type=str, default=None)

        parser.add_argument("--max-gdelt-direct-contexts", type=int, default=3)
        parser.add_argument("--max-gdelt-indirect-contexts", type=int, default=3)
        parser.add_argument("--max-gdelt-background-contexts", type=int, default=2)
        parser.add_argument("--max-macro-events", type=int, default=3)
        parser.add_argument("--max-stock-events", type=int, default=3)
        parser.add_argument("--max-dart-events", type=int, default=3)

        parser.add_argument("--price-return-z-threshold", type=float, default=2.0)
        parser.add_argument("--volume-z-threshold", type=float, default=2.0)
        parser.add_argument("--min-abs-return-for-price-move", type=float, default=0.03)

        parser.add_argument(
            "--exclude-ineligible-bundles",
            action="store_true",
            help="Write only bundles with can_generate_stock_news=True.",
        )

        parser.add_argument(
            "--no-event-only-rows",
            action="store_true",
            help="Do not create bundles from price-volume abnormal rows alone.",
        )

        return parser


def build_config(args: argparse.Namespace) -> BundleBuildConfig:
    return BundleBuildConfig(
        judged_context_csv=args.judged_context_csv,
        stock_event_csv=args.stock_event_csv,
        macro_event_csv=args.macro_event_csv,
        dart_json=args.dart_json,
        price_volume_xlsx=args.price_volume_xlsx,
        output_jsonl=args.output_jsonl,
        report_txt=args.report_txt,
        start_date=args.start_date,
        end_date=args.end_date,
        max_gdelt_direct_contexts=args.max_gdelt_direct_contexts,
        max_gdelt_indirect_contexts=args.max_gdelt_indirect_contexts,
        max_gdelt_background_contexts=args.max_gdelt_background_contexts,
        max_macro_events=args.max_macro_events,
        max_stock_events=args.max_stock_events,
        max_dart_events=args.max_dart_events,
        price_return_z_threshold=args.price_return_z_threshold,
        volume_z_threshold=args.volume_z_threshold,
        min_abs_return_for_price_move=args.min_abs_return_for_price_move,
        include_ineligible_bundles=not args.exclude_ineligible_bundles,
        build_from_event_only_rows=not args.no_event_only_rows,
    )


def main() -> None:
    parser = CliParser.build()
    args = parser.parse_args()

    config = build_config(args)

    pipeline = Pr05cPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()