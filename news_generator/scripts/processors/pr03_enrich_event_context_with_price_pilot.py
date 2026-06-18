from __future__ import annotations

import argparse
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class PriceEnrichConfig:
    year: int = 2018

    project_root: Path = Path(".")
    input_dir: Path = Path("data/raw")
    output_dir: Path = Path("data/raw")

    event_context_file_template: str = "event_context_daily_{year}.csv"

    stock_excel_path: Optional[Path] = None
    macro_signal_path: Path = Path("data/raw/macro_signal_daily.csv")

    output_event_file_template: str = "event_context_daily_{year}_price_enriched.csv"
    output_summary_file_template: str = "event_context_daily_{year}_price_enriched_summary.csv"
    output_stock_context_template: str = "stock_price_context_{year}.csv"
    output_excel_debug_template: str = "stock_excel_debug_{year}_top.csv"

    min_stock_count: int = 5
    asof_tolerance_days: int = 5

    def resolve_paths(self) -> "PriceEnrichConfig":
        self.project_root = self.project_root.resolve()

        if not self.input_dir.is_absolute():
            self.input_dir = self.project_root / self.input_dir

        if not self.output_dir.is_absolute():
            self.output_dir = self.project_root / self.output_dir

        if self.stock_excel_path is None:
            self.stock_excel_path = Path(f"../data/raw/stock/{self.year}stock.xlsx")

        if not self.stock_excel_path.is_absolute():
            self.stock_excel_path = self.project_root / self.stock_excel_path

        if not self.macro_signal_path.is_absolute():
            self.macro_signal_path = self.project_root / self.macro_signal_path

        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def event_context_path(self) -> Path:
        return self.input_dir / self.event_context_file_template.format(year=self.year)

    @property
    def output_event_path(self) -> Path:
        return self.output_dir / self.output_event_file_template.format(year=self.year)

    @property
    def output_summary_path(self) -> Path:
        return self.output_dir / self.output_summary_file_template.format(year=self.year)

    @property
    def output_stock_context_path(self) -> Path:
        return self.output_dir / self.output_stock_context_template.format(year=self.year)

    @property
    def output_excel_debug_path(self) -> Path:
        return self.output_dir / self.output_excel_debug_template.format(year=self.year)


class NumericCleaner:
    def clean(self, series: pd.Series) -> pd.Series:
        s = series.astype(str).str.strip()
        s = s.str.replace(",", "", regex=False)
        s = s.str.replace("%", "", regex=False)
        s = s.str.replace("원", "", regex=False)
        s = s.str.replace("주", "", regex=False)
        s = s.str.replace(" ", "", regex=False)
        s = s.replace(
            {
                "": np.nan,
                "-": np.nan,
                "nan": np.nan,
                "None": np.nan,
                "<NA>": np.nan,
                "NaT": np.nan,
            }
        )
        return pd.to_numeric(s, errors="coerce")


class TickerNormalizer:
    def normalize_one(self, value) -> str:
        if pd.isna(value):
            return ""

        text = str(value).strip()
        text = re.sub(r"\.0$", "", text)
        text = text.replace("'", "").replace('"', "")
        text = text.replace(" ", "")

        if text.upper().startswith("A") and len(text) >= 7:
            text = text[1:]

        if not text:
            return ""

        if text.lower() in {"nan", "none", "<na>", "nat"}:
            return ""

        if text.isdigit():
            text = text.zfill(6)

        return text

    def normalize(self, series: pd.Series) -> pd.Series:
        return series.apply(self.normalize_one)

    def is_date_like_code(self, code: str) -> bool:
        code = str(code).strip()

        if not re.fullmatch(r"\d{6}", code):
            return False

        year = int(code[:4])
        month = int(code[4:6])

        return 1900 <= year <= 2100 and 1 <= month <= 12


class CompanyKeyBuilder:
    def build(self, value) -> str:
        if pd.isna(value):
            return ""

        text = str(value).strip()
        text = re.sub(r"\s+", "", text)
        text = text.replace("(주)", "")
        text = text.replace("㈜", "")
        text = text.replace("주식회사", "")
        text = text.replace("보통주", "")
        text = text.replace("우선주", "")

        return text


class SafeDateParser:
    def parse_single(self, value) -> Optional[pd.Timestamp]:
        if pd.isna(value):
            return None

        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
            try:
                return pd.Timestamp(value)
            except Exception:
                return None

        text = str(value).strip()
        text = re.sub(r"\.0$", "", text)

        if re.fullmatch(r"\d{8}", text):
            parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
            return parsed if pd.notna(parsed) else None

        for fmt, pattern in [
            ("%Y-%m-%d", r"\d{4}-\d{1,2}-\d{1,2}"),
            ("%Y/%m/%d", r"\d{4}/\d{1,2}/\d{1,2}"),
            ("%Y.%m.%d", r"\d{4}\.\d{1,2}\.\d{1,2}"),
        ]:
            if re.fullmatch(pattern, text):
                parsed = pd.to_datetime(text, format=fmt, errors="coerce")
                return parsed if pd.notna(parsed) else None

        num = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]

        if pd.notna(num) and 35000 <= float(num) <= 50000:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    parsed = pd.to_datetime(
                        num,
                        unit="D",
                        origin="1899-12-30",
                        errors="coerce",
                    )
                    return parsed if pd.notna(parsed) else None
                except Exception:
                    return None

        return None

    def parse_series(self, series: pd.Series) -> pd.Series:
        return pd.Series(
            [self.parse_single(v) for v in series],
            index=series.index,
            dtype="datetime64[ns]",
        )


class StockExcelParser:
    def __init__(self, config: PriceEnrichConfig):
        self.config = config
        self.numeric_cleaner = NumericCleaner()
        self.ticker_normalizer = TickerNormalizer()
        self.date_parser = SafeDateParser()

    def load(self) -> pd.DataFrame:
        if self.config.stock_excel_path is None or not self.config.stock_excel_path.exists():
            raise FileNotFoundError(f"주식 가격 엑셀 없음: {self.config.stock_excel_path}")

        print(f"[주식 가격 엑셀 로드] {self.config.stock_excel_path}")

        sheets = self._read_excel_sheets(self.config.stock_excel_path)

        print(f"[시트 수] {len(sheets)}")
        print(f"[시트명] {list(sheets.keys())}")

        parts = []

        for sheet_name, raw_df in sheets.items():
            if raw_df.empty:
                continue

            print("=" * 100)
            print(f"[시트 검사] {sheet_name} rows={len(raw_df):,} cols={len(raw_df.columns):,}")

            try:
                parsed = self._parse_sheet(raw_df, sheet_name)

                if not parsed.empty:
                    parts.append(parsed)
                    print(f"[시트 변환 성공] {sheet_name} rows={len(parsed):,}")
                else:
                    print(f"[시트 변환 결과 없음] {sheet_name}")

            except Exception as e:
                print(f"[시트 변환 실패] {sheet_name} / reason={e}")
                self._save_debug(raw_df)

        if not parts:
            raise ValueError(
                "주가 엑셀에서 주가 데이터를 추출하지 못함. "
                f"debug 파일 확인 필요: {self.config.output_excel_debug_path}"
            )

        df = pd.concat(parts, ignore_index=True)

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df[df["date"].dt.year.eq(self.config.year)].copy()

        df["ticker"] = self.ticker_normalizer.normalize(df["ticker"])
        df = df[df["ticker"].ne("")].copy()
        df = df[~df["ticker"].apply(self.ticker_normalizer.is_date_like_code)].copy()
        df = df.dropna(subset=["close"])

        df = (
            df.groupby(["date", "ticker"], as_index=False)
            .agg(
                company_name=("company_name", "last"),
                close=("close", "last"),
                volume=("volume", "last"),
            )
        )

        df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

        stock_count = df["ticker"].nunique()

        print("=" * 100)
        print(f"[주식 가격 정규화 완료] rows={len(df):,}")
        print(f"[종목 수] {stock_count:,}")

        if not df.empty:
            print(f"[기간] {df['date'].min().date()} ~ {df['date'].max().date()}")
            print("[샘플]")
            print(df.head(30).to_string(index=False))

        if stock_count < self.config.min_stock_count:
            raise ValueError(
                f"추출된 종목 수가 너무 적음: {stock_count}. "
                f"엑셀 구조 또는 ticker 추론 확인 필요."
            )

        return df

    def _read_excel_sheets(self, path: Path) -> Dict[str, pd.DataFrame]:
        try:
            return pd.read_excel(
                path,
                sheet_name=None,
                header=None,
                dtype=object,
                engine="openpyxl",
            )
        except Exception:
            return pd.read_excel(
                path,
                sheet_name=None,
                header=None,
                dtype=object,
            )

    def _parse_sheet(self, raw_df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
        fixed = self._try_npq_price_volume_format(raw_df, sheet_name)

        if not fixed.empty:
            print("[고정형 NPQ 주가/거래량 포맷 감지]")
            return fixed

        raise ValueError(
            "지원하는 주가 엑셀 구조가 아님. "
            "현재 코드는 WiseFn/QuantiWise NPQ price-volume 구조를 우선 지원함."
        )

    def _try_npq_price_volume_format(self, raw_df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
        """
        WiseFn/QuantiWise 계열 stock_price-volume_npq.xlsx 구조 전용 파서.

        실제 확인된 구조:
        - row 9  : 코드       A000660, A000660, A058470, ...
        - row 10 : 코드명     SK하이닉스, SK하이닉스, 리노공업, ...
        - row 13 : 아이템명   종가(원), 거래량(주), 종가(원), 거래량(주), ...
        - row 15~: 날짜 + 값
        - col 0  : 날짜
        - col 1~ : 종목별 종가/거래량 반복
        """
        if raw_df.shape[0] < 20 or raw_df.shape[1] < 3:
            return pd.DataFrame()

        code_row = self._find_row_by_first_col(raw_df, ["코드", "code", "symbol"])
        name_row = self._find_row_by_first_col(raw_df, ["코드명", "종목명", "회사명", "name", "symbol name"])
        item_row = self._find_row_by_first_col(raw_df, ["아이템명", "item", "item name"])

        if code_row is None or name_row is None or item_row is None:
            return pd.DataFrame()

        data_start = self._find_data_start_row(raw_df, start_row=item_row + 1)

        if data_start is None:
            return pd.DataFrame()

        print(
            f"[npq 포맷 후보] sheet={sheet_name}, "
            f"code_row={code_row}, name_row={name_row}, item_row={item_row}, data_start={data_start}"
        )

        date_values = self.date_parser.parse_series(raw_df.iloc[data_start:, 0])

        parts = []
        meta_rows = []

        for col_idx in range(1, raw_df.shape[1]):
            raw_code = raw_df.iloc[code_row, col_idx]
            raw_name = raw_df.iloc[name_row, col_idx]
            raw_item = raw_df.iloc[item_row, col_idx]

            ticker = self.ticker_normalizer.normalize_one(raw_code)

            if not ticker:
                continue

            if self.ticker_normalizer.is_date_like_code(ticker):
                continue

            company_name = "" if pd.isna(raw_name) else str(raw_name).strip()
            item_name = "" if pd.isna(raw_item) else str(raw_item).strip()

            field = self._infer_npq_field(item_name)

            if field is None:
                continue

            values = self.numeric_cleaner.clean(raw_df.iloc[data_start:, col_idx])

            part = pd.DataFrame(
                {
                    "date": date_values.values,
                    "ticker": ticker,
                    "company_name": company_name,
                    "field": field,
                    "value": values.values,
                }
            )

            part = part.dropna(subset=["date", "value"])

            if part.empty:
                continue

            parts.append(part)

            meta_rows.append(
                {
                    "col_idx": col_idx,
                    "ticker": ticker,
                    "company_name": company_name,
                    "field": field,
                    "item_name": item_name,
                    "valid_values": int(values.notna().sum()),
                }
            )

        if not parts:
            return pd.DataFrame()

        meta_df = pd.DataFrame(meta_rows)

        print("[npq 추론 컬럼 샘플]")
        print(meta_df.head(40).to_string(index=False))

        field_df = pd.concat(parts, ignore_index=True)

        pivot = (
            field_df.pivot_table(
                index=["date", "ticker", "company_name"],
                columns="field",
                values="value",
                aggfunc="last",
            )
            .reset_index()
        )

        pivot.columns.name = None

        if "close" not in pivot.columns:
            return pd.DataFrame()

        if "volume" not in pivot.columns:
            pivot["volume"] = np.nan

        out = pivot[["date", "ticker", "company_name", "close", "volume"]].copy()

        print(
            f"[npq 변환 완료] rows={len(out):,}, "
            f"tickers={out['ticker'].nunique():,}, "
            f"date={out['date'].min().date()}~{out['date'].max().date()}"
        )

        return out

    def _find_row_by_first_col(self, raw_df: pd.DataFrame, candidates: List[str]) -> Optional[int]:
        target = [self._normalize_label(x) for x in candidates]

        for idx in range(len(raw_df)):
            value = raw_df.iloc[idx, 0]

            if pd.isna(value):
                continue

            label = self._normalize_label(value)

            if label in target:
                return idx

        return None

    def _find_data_start_row(self, raw_df: pd.DataFrame, start_row: int) -> Optional[int]:
        for row_idx in range(start_row, len(raw_df)):
            parsed = self.date_parser.parse_single(raw_df.iloc[row_idx, 0])

            if parsed is not None and pd.notna(parsed):
                return row_idx

        return None

    def _infer_npq_field(self, item_name: str) -> Optional[str]:
        text = str(item_name).lower().replace(" ", "")

        if "거래량" in text or "volume" in text:
            return "volume"

        if "종가" in text or "close" in text or "price" in text:
            return "close"

        return None

    def _normalize_label(self, value) -> str:
        text = str(value).strip().lower()
        text = re.sub(r"[\s_\-./(){}\[\]·ㆍ]", "", text)
        return text

    def _save_debug(self, raw_df: pd.DataFrame) -> None:
        debug = raw_df.iloc[:80, :min(120, raw_df.shape[1])].copy()
        debug.to_csv(
            self.config.output_excel_debug_path,
            index=False,
            header=False,
            encoding="utf-8-sig",
        )
        print(f"[엑셀 디버그 저장] {self.config.output_excel_debug_path}")


class StockPriceFeatureBuilder:
    def build(self, stock_df: pd.DataFrame) -> pd.DataFrame:
        df = stock_df.copy()
        df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

        df["stock_return_1d"] = df.groupby("ticker")["close"].pct_change(1) * 100
        df["stock_return_5d"] = df.groupby("ticker")["close"].pct_change(5) * 100
        df["stock_return_20d"] = df.groupby("ticker")["close"].pct_change(20) * 100

        df["volume_5d_avg"] = (
            df.groupby("ticker")["volume"]
            .transform(lambda s: s.rolling(5, min_periods=3).mean())
        )
        df["volume_20d_avg"] = (
            df.groupby("ticker")["volume"]
            .transform(lambda s: s.rolling(20, min_periods=5).mean())
        )
        df["volume_ratio_20d"] = df["volume"] / df["volume_20d_avg"].replace(0, np.nan)

        df = df.replace([np.inf, -np.inf], np.nan)

        return df


class MarketReturnLoader:
    def __init__(self, config: PriceEnrichConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        candidates = [
            self.config.macro_signal_path,
            self.config.project_root / "../market_indicator/data/processed/macro_signal_daily.csv",
        ]

        for path in candidates:
            path = path.resolve()

            if path.exists():
                print(f"[시장 수익률 로드] {path}")
                return self._load_from_path(path)

        print("[시장 수익률 파일 없음] market_return은 비워둠")
        return pd.DataFrame(columns=["date", "kospi_ret_1d", "kosdaq_ret_1d"])

    def _load_from_path(self, path: Path) -> pd.DataFrame:
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        keep_cols = ["date"]

        for col in [
            "kospi_ret_1d",
            "kospi_ret_5d",
            "kospi_ret_20d",
            "kosdaq_ret_1d",
            "kosdaq_ret_5d",
            "kosdaq_ret_20d",
        ]:
            if col in df.columns:
                keep_cols.append(col)

        df = df[keep_cols].copy()

        for col in df.columns:
            if col != "date":
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df


class EventContextLoader:
    TEXT_COLUMNS = [
        "asset_id",
        "company_name",
        "signal_group",
        "event_type",
        "event_frame",
        "evidence_1",
        "evidence_2",
        "evidence_3",
        "market",
        "sector",
        "ticker",
    ]

    def __init__(self, config: PriceEnrichConfig):
        self.config = config
        self.ticker_normalizer = TickerNormalizer()

    def load(self) -> pd.DataFrame:
        path = self.config.event_context_path

        if not path.exists():
            raise FileNotFoundError(f"이벤트 컨텍스트 파일 없음: {path}")

        df = pd.read_csv(path)
        print(f"[이벤트 컨텍스트 로드] {path} rows={len(df):,}")

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df[df["date"].dt.year.eq(self.config.year)].copy()

        for col in self.TEXT_COLUMNS:
            if col not in df.columns:
                df[col] = ""

            df[col] = df[col].fillna("").astype(str)

        df["ticker"] = self.ticker_normalizer.normalize(df["ticker"])

        if "event_id" not in df.columns:
            df["event_id"] = [
                f"event_{self.config.year}_{i:06d}"
                for i in range(len(df))
            ]

        if "priority_score" not in df.columns:
            df["priority_score"] = 0.0

        if "selection_rank" not in df.columns:
            df["selection_rank"] = 9999

        return df.reset_index(drop=True)


class PriceEvidenceBuilder:
    def build_price_evidence(self, row: pd.Series) -> Tuple[str, str, str, str]:
        ticker = self._safe_str(row.get("ticker"))
        company_name = self._safe_str(row.get("company_name"))
        price_date = self._safe_str(row.get("price_date"))

        close = self._safe_float(row.get("stock_close"))
        ret_1d = self._safe_float(row.get("stock_return_1d"))
        ret_5d = self._safe_float(row.get("stock_return_5d"))
        volume_ratio = self._safe_float(row.get("volume_ratio_20d"))
        market_ret = self._safe_float(row.get("market_return_1d"))
        rel_ret = self._safe_float(row.get("relative_return_vs_market_1d"))

        if not ticker or close is None:
            return "", "", "", ""

        evidence_1 = (
            f"가격 기준일 {price_date} / 종가 {close:,.0f}원"
            if price_date
            else f"종가 {close:,.0f}원"
        )

        evidence_2 = (
            f"종가 기준 1일 수익률 {ret_1d:.2f}%"
            if ret_1d is not None
            else ""
        )

        detail_parts = []

        if ret_5d is not None:
            detail_parts.append(f"5거래일 수익률 {ret_5d:.2f}%")

        if rel_ret is not None:
            detail_parts.append(f"시장 대비 초과수익률 {rel_ret:.2f}%p")
        elif market_ret is not None:
            detail_parts.append(f"시장 1일 수익률 {market_ret:.2f}%")

        if volume_ratio is not None:
            detail_parts.append(f"20일 평균 대비 거래량 {volume_ratio:.2f}배")

        evidence_3 = " / ".join(detail_parts)

        price_frame = self._build_price_frame(
            company_name=company_name,
            ret_1d=ret_1d,
            rel_ret=rel_ret,
            volume_ratio=volume_ratio,
        )

        return evidence_1, evidence_2, evidence_3, price_frame

    def _build_price_frame(
        self,
        company_name: str,
        ret_1d: Optional[float],
        rel_ret: Optional[float],
        volume_ratio: Optional[float],
    ) -> str:
        name = company_name if company_name else "해당 종목"

        if ret_1d is None:
            return f"{name}의 종가 기준 가격 반응은 확인되지 않음"

        if abs(ret_1d) < 1.0:
            return f"{name}은 종가 기준 주가 반응이 제한적이었음"

        direction_word = "상승" if ret_1d > 0 else "하락"

        if rel_ret is not None:
            if rel_ret >= 1.0:
                relative_text = "시장보다 강했음"
            elif rel_ret <= -1.0:
                relative_text = "시장보다 부진했음"
            else:
                relative_text = "시장과 비슷한 흐름이었음"
        else:
            relative_text = "가격 변화가 관측됨"

        volume_text = ""

        if volume_ratio is not None and volume_ratio >= 1.5:
            volume_text = " 거래량도 최근 평균보다 많았음"

        return (
            f"{name}은 종가 기준 전일 대비 {ret_1d:.2f}% {direction_word}했고 "
            f"{relative_text}.{volume_text}"
        )

    def _safe_str(self, value) -> str:
        if pd.isna(value):
            return ""

        text = str(value).strip()

        if text in {"nan", "None", "<NA>", "NaT"}:
            return ""

        return text

    def _safe_float(self, value) -> Optional[float]:
        try:
            if pd.isna(value):
                return None
            return float(value)
        except Exception:
            return None


class PriceEnricher:
    def __init__(self, config: PriceEnrichConfig):
        self.config = config
        self.company_key_builder = CompanyKeyBuilder()
        self.evidence_builder = PriceEvidenceBuilder()

    def enrich(
        self,
        event_df: pd.DataFrame,
        stock_context: pd.DataFrame,
        market_returns: pd.DataFrame,
    ) -> pd.DataFrame:
        df = event_df.copy()
        df["_row_id"] = np.arange(len(df))

        stock = stock_context.copy()
        stock["price_date"] = stock["date"]

        stock = stock.rename(
            columns={
                "company_name": "price_company_name",
                "close": "stock_close",
                "volume": "stock_volume",
            }
        )

        stock_cols = [
            "date",
            "price_date",
            "ticker",
            "price_company_name",
            "stock_close",
            "stock_volume",
            "stock_return_1d",
            "stock_return_5d",
            "stock_return_20d",
            "volume_5d_avg",
            "volume_20d_avg",
            "volume_ratio_20d",
        ]

        stock = stock[stock_cols].copy()

        # 1차: 동일 날짜 + ticker 매칭
        df = df.merge(
            stock,
            on=["date", "ticker"],
            how="left",
        )

        # 2차: 비거래일 이벤트는 ticker별 다음 거래일 가격으로 매칭
        missing = (
            df["stock_close"].isna()
            & df["ticker"].fillna("").astype(str).str.strip().ne("")
        )

        if missing.any():
            asof_result = self._asof_match_by_ticker(
                events=df.loc[missing, ["_row_id", "date", "ticker"]],
                stock=stock,
            )

            if not asof_result.empty:
                df = df.merge(
                    asof_result,
                    on="_row_id",
                    how="left",
                    suffixes=("", "_asof"),
                )

                fill_cols = [
                    "price_date",
                    "price_company_name",
                    "stock_close",
                    "stock_volume",
                    "stock_return_1d",
                    "stock_return_5d",
                    "stock_return_20d",
                    "volume_5d_avg",
                    "volume_20d_avg",
                    "volume_ratio_20d",
                ]

                for col in fill_cols:
                    asof_col = f"{col}_asof"

                    if asof_col in df.columns:
                        fill_mask = df[col].isna() & df[asof_col].notna()
                        df.loc[fill_mask, col] = df.loc[fill_mask, asof_col]

                df = df.drop(columns=[c for c in df.columns if c.endswith("_asof")], errors="ignore")

        # 3차: company_name fallback
        missing = (
            df["stock_close"].isna()
            & df["company_name"].fillna("").astype(str).str.strip().ne("")
        )

        if missing.any():
            df = self._fallback_match_by_company(df, stock)

        if not market_returns.empty:
            df = df.merge(market_returns, on="date", how="left")

        for col in ["kospi_ret_1d", "kosdaq_ret_1d", "kospi_ret_5d", "kosdaq_ret_5d"]:
            if col not in df.columns:
                df[col] = np.nan

        market_upper = df["market"].fillna("").astype(str).str.upper()

        df["market_return_1d"] = np.where(
            market_upper.eq("KOSDAQ"),
            df["kosdaq_ret_1d"],
            df["kospi_ret_1d"],
        )
        df["market_return_5d"] = np.where(
            market_upper.eq("KOSDAQ"),
            df["kosdaq_ret_5d"],
            df["kospi_ret_5d"],
        )

        df["relative_return_vs_market_1d"] = df["stock_return_1d"] - df["market_return_1d"]
        df["relative_return_vs_market_5d"] = df["stock_return_5d"] - df["market_return_5d"]

        df["has_price_context"] = df["stock_close"].notna()

        evidence = df.apply(
            lambda row: self.evidence_builder.build_price_evidence(row),
            axis=1,
            result_type="expand",
        )

        evidence.columns = [
            "price_evidence_1",
            "price_evidence_2",
            "price_evidence_3",
            "price_frame",
        ]

        df = pd.concat([df, evidence], axis=1)
        df = self._append_price_context(df)

        df = df.drop(columns=["_row_id"], errors="ignore")

        return df

    def _asof_match_by_ticker(
        self,
        events: pd.DataFrame,
        stock: pd.DataFrame,
    ) -> pd.DataFrame:
        results = []
        tolerance = pd.Timedelta(days=self.config.asof_tolerance_days)

        stock_sorted = stock.sort_values(["ticker", "price_date"]).copy()

        for ticker, ev in events.groupby("ticker"):
            st = stock_sorted[stock_sorted["ticker"].eq(ticker)].copy()

            if st.empty:
                continue

            ev = ev.sort_values("date").copy()
            st = st.sort_values("price_date").copy()

            matched = pd.merge_asof(
                ev,
                st.drop(columns=["date"], errors="ignore"),
                left_on="date",
                right_on="price_date",
                by="ticker",
                direction="forward",
                tolerance=tolerance,
            )

            results.append(matched)

        if not results:
            return pd.DataFrame(columns=["_row_id"])

        out = pd.concat(results, ignore_index=True)
        out = out.drop(columns=["date", "ticker"], errors="ignore")

        return out

    def _fallback_match_by_company(
        self,
        df: pd.DataFrame,
        stock: pd.DataFrame,
    ) -> pd.DataFrame:
        out = df.copy()

        stock2 = stock.copy()
        stock2["company_key"] = stock2["price_company_name"].apply(self.company_key_builder.build)
        stock2 = stock2[stock2["company_key"].ne("")].copy()

        target = out[out["stock_close"].isna()].copy()
        target["company_key"] = target["company_name"].apply(self.company_key_builder.build)
        target = target[target["company_key"].ne("")].copy()

        if target.empty or stock2.empty:
            return out

        matches = []
        tolerance = pd.Timedelta(days=self.config.asof_tolerance_days)

        for key, ev in target.groupby("company_key"):
            st = stock2[stock2["company_key"].eq(key)].copy()

            if st.empty:
                continue

            ev = ev.sort_values("date").copy()
            st = st.sort_values("price_date").copy()

            matched = pd.merge_asof(
                ev[["_row_id", "date", "company_key"]],
                st.drop(columns=["date"], errors="ignore"),
                left_on="date",
                right_on="price_date",
                by="company_key",
                direction="forward",
                tolerance=tolerance,
            )

            matches.append(matched)

        if not matches:
            return out

        m = pd.concat(matches, ignore_index=True)
        m = m.dropna(subset=["stock_close"])

        if m.empty:
            return out

        m = m.drop_duplicates("_row_id", keep="first")

        fill_cols = [
            "ticker",
            "price_company_name",
            "price_date",
            "stock_close",
            "stock_volume",
            "stock_return_1d",
            "stock_return_5d",
            "stock_return_20d",
            "volume_5d_avg",
            "volume_20d_avg",
            "volume_ratio_20d",
        ]

        out = out.merge(
            m[["_row_id"] + fill_cols],
            on="_row_id",
            how="left",
            suffixes=("", "_fb"),
        )

        for col in fill_cols:
            fb = f"{col}_fb"

            if fb not in out.columns:
                continue

            if col not in out.columns:
                out[col] = np.nan

            if col == "ticker":
                mask = (
                    out["stock_close"].isna()
                    & out[fb].notna()
                    & out[col].fillna("").astype(str).str.strip().eq("")
                )
            else:
                mask = out[col].isna() & out[fb].notna()

            out.loc[mask, col] = out.loc[mask, fb]

        out = out.drop(columns=[c for c in out.columns if c.endswith("_fb")], errors="ignore")
        out = out.drop(columns=["company_key"], errors="ignore")

        return out

    def _append_price_context(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        for col in ["event_frame", "evidence_1", "evidence_2", "evidence_3"]:
            if col not in out.columns:
                out[col] = ""
            out[col] = out[col].fillna("").astype(str)

        mask = out["has_price_context"].fillna(False)

        out.loc[mask, "event_frame"] = (
            out.loc[mask, "event_frame"].str.rstrip()
            + " "
            + out.loc[mask, "price_frame"].fillna("").astype(str)
        )

        out.loc[mask, "evidence_1"] = out.loc[mask].apply(
            lambda r: self._join_evidence(r["evidence_1"], r["price_evidence_1"]),
            axis=1,
        )
        out.loc[mask, "evidence_2"] = out.loc[mask].apply(
            lambda r: self._join_evidence(r["evidence_2"], r["price_evidence_2"]),
            axis=1,
        )
        out.loc[mask, "evidence_3"] = out.loc[mask].apply(
            lambda r: self._join_evidence(r["evidence_3"], r["price_evidence_3"]),
            axis=1,
        )

        return out

    def _join_evidence(self, old: str, new: str) -> str:
        old = "" if pd.isna(old) else str(old).strip()
        new = "" if pd.isna(new) else str(new).strip()

        if old and new:
            return f"{old} | {new}"

        return old or new


class PriceEnrichPipeline:
    def __init__(self, config: PriceEnrichConfig):
        self.config = config

    def run(self) -> None:
        event_df = EventContextLoader(self.config).load()

        stock_raw = StockExcelParser(self.config).load()
        stock_context = StockPriceFeatureBuilder().build(stock_raw)

        market_returns = MarketReturnLoader(self.config).load()

        enriched = PriceEnricher(self.config).enrich(
            event_df=event_df,
            stock_context=stock_context,
            market_returns=market_returns,
        )

        summary = self._build_summary(event_df, enriched, stock_context)
        self._save(enriched, summary, stock_context)

    def _build_summary(
        self,
        event_df: pd.DataFrame,
        enriched: pd.DataFrame,
        stock_context: pd.DataFrame,
    ) -> pd.DataFrame:
        stock_event_count = int(
            event_df["ticker"].fillna("").astype(str).str.strip().ne("").sum()
        )

        matched_count = int(enriched["has_price_context"].fillna(False).sum())

        disclosure_count = (
            int(event_df["signal_group"].eq("disclosure").sum())
            if "signal_group" in event_df.columns
            else 0
        )

        disclosure_matched = (
            int(
                enriched[enriched["signal_group"].eq("disclosure")]["has_price_context"]
                .fillna(False)
                .sum()
            )
            if "signal_group" in enriched.columns
            else 0
        )

        return pd.DataFrame(
            [
                {
                    "year": self.config.year,
                    "event_rows": len(event_df),
                    "stock_event_rows": stock_event_count,
                    "price_matched_rows": matched_count,
                    "price_match_ratio_all": matched_count / len(event_df) if len(event_df) else np.nan,
                    "disclosure_rows": disclosure_count,
                    "disclosure_price_matched_rows": disclosure_matched,
                    "disclosure_price_match_ratio": (
                        disclosure_matched / disclosure_count
                        if disclosure_count
                        else np.nan
                    ),
                    "stock_price_rows": len(stock_context),
                    "stock_count": stock_context["ticker"].nunique(),
                    "price_date_min": (
                        stock_context["date"].min().strftime("%Y-%m-%d")
                        if not stock_context.empty
                        else ""
                    ),
                    "price_date_max": (
                        stock_context["date"].max().strftime("%Y-%m-%d")
                        if not stock_context.empty
                        else ""
                    ),
                }
            ]
        )

    def _save(
        self,
        enriched: pd.DataFrame,
        summary: pd.DataFrame,
        stock_context: pd.DataFrame,
    ) -> None:
        enriched_out = enriched.copy()
        stock_out = stock_context.copy()

        for df in [enriched_out, stock_out]:
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

            if "price_date" in df.columns:
                df["price_date"] = pd.to_datetime(df["price_date"], errors="coerce").dt.strftime("%Y-%m-%d")

        enriched_out.to_csv(self.config.output_event_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.config.output_summary_path, index=False, encoding="utf-8-sig")
        stock_out.to_csv(self.config.output_stock_context_path, index=False, encoding="utf-8-sig")

        print("=" * 100)
        print(f"[저장 완료] {self.config.output_event_path}")
        print(f"[저장 완료] {self.config.output_summary_path}")
        print(f"[저장 완료] {self.config.output_stock_context_path}")
        print("=" * 100)

        print("[요약]")
        print(summary.to_string(index=False))

        print("=" * 100)
        print("[signal_group별 가격 결합률]")
        if "signal_group" in enriched_out.columns:
            print(
                enriched_out.groupby("signal_group")["has_price_context"]
                .agg(["count", "sum", "mean"])
                .sort_values("sum", ascending=False)
            )

        print("=" * 100)
        print("[가격 결합 샘플]")
        sample_cols = [
            "date",
            "price_date",
            "signal_group",
            "ticker",
            "company_name",
            "event_type",
            "stock_close",
            "stock_return_1d",
            "market_return_1d",
            "relative_return_vs_market_1d",
            "price_evidence_1",
            "price_evidence_2",
            "price_evidence_3",
            "has_price_context",
        ]
        sample_cols = [c for c in sample_cols if c in enriched_out.columns]

        matched = enriched_out[enriched_out["has_price_context"].fillna(False)]

        if not matched.empty:
            print(matched[sample_cols].head(40).to_string(index=False))
        else:
            print("가격 결합된 행 없음")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument("--year", type=int, default=2018)
    parser.add_argument("--project-root", type=str, default=".")
    parser.add_argument("--input-dir", type=str, default="data/raw")
    parser.add_argument("--output-dir", type=str, default="data/raw")
    parser.add_argument("--stock-excel", type=str, default=None)
    parser.add_argument("--macro-signal", type=str, default="data/raw/macro_signal_daily.csv")
    parser.add_argument("--min-stock-count", type=int, default=5)
    parser.add_argument("--asof-tolerance-days", type=int, default=5)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    stock_excel_path = Path(args.stock_excel) if args.stock_excel else None

    config = PriceEnrichConfig(
        year=args.year,
        project_root=Path(args.project_root),
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        stock_excel_path=stock_excel_path,
        macro_signal_path=Path(args.macro_signal),
        min_stock_count=args.min_stock_count,
        asof_tolerance_days=args.asof_tolerance_days,
    ).resolve_paths()

    print("=" * 100)
    print("[pr03 주식 가격 데이터 결합 시작 - NPQ 고정형 파서]")
    print(f"project_root: {config.project_root}")
    print(f"year: {config.year}")
    print(f"event_context: {config.event_context_path}")
    print(f"stock_excel: {config.stock_excel_path}")
    print(f"macro_signal: {config.macro_signal_path}")
    print(f"output_event: {config.output_event_path}")
    print("=" * 100)

    pipeline = PriceEnrichPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()