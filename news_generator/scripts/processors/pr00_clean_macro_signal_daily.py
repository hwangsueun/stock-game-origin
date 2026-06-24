# ============================================================
# pr00_clean_macro_signal_daily.py
# 뉴스 생성용 거시 시그널 데이터 정제 코드
# ============================================================

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 1. Config
# ============================================================

@dataclass
class MacroCleanConfig:
    input_path: Path
    output_path: Path
    report_path: Path

    date_col_candidates: Tuple[str, ...] = (
        "date",
        "Date",
        "날짜",
        "base_date",
        "ref_date",
        "published_at",
        "SQLDATE",
    )

    required_date_col: str = "date"

    rolling_window: int = 60
    min_periods: int = 20

    fill_daily_method: str = "ffill"

    price_like_cols: Tuple[str, ...] = (
        "kospi_close",
        "kosdaq_close",
        "usdkrw",
        "kr_3y_yield",
        "kr_10y_yield",
        "wti",
        "dubai_oil",
        "gold",
    )

    ret_pairs: Dict[str, str] = field(default_factory=lambda: {
        "kospi_close": "kospi_ret_1d",
        "kosdaq_close": "kosdaq_ret_1d",
        "usdkrw": "usdkrw_ret_1d",
        "wti": "wti_ret_1d",
        "dubai_oil": "dubai_oil_ret_1d",
        "gold": "gold_ret_1d",
    })

    protected_cols: Tuple[str, ...] = (
        "date",
    )


# ============================================================
# 2. Cleaner
# ============================================================

class MacroSignalCleaner:
    def __init__(self, config: MacroCleanConfig):
        self.config = config

    def run(self) -> pd.DataFrame:
        df = self._load_csv(self.config.input_path)

        original_shape = df.shape
        original_cols = list(df.columns)

        df = self._standardize_columns(df)
        df = self._standardize_date(df)
        df = self._sort_and_deduplicate(df)
        df = self._coerce_numeric_columns(df)
        df = self._reindex_daily(df)
        df = self._create_return_columns(df)
        df = self._create_spread_columns(df)
        df = self._fill_missing_values(df)
        df = self._create_rolling_zscores(df)

        report = self._build_quality_report(
            df=df,
            original_shape=original_shape,
            original_cols=original_cols,
        )

        self._save_outputs(df, report)

        return df

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------

    def _load_csv(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"입력 파일이 없습니다: {path}")

        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(path, encoding="cp949")

    # --------------------------------------------------------
    # Column
    # --------------------------------------------------------

    def _standardize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        rename_map = {}
        for col in df.columns:
            clean_col = (
                str(col)
                .strip()
                .replace(" ", "_")
                .replace("-", "_")
                .replace("/", "_")
                .replace(".", "_")
                .lower()
            )
            rename_map[col] = clean_col

        df = df.rename(columns=rename_map)

        return df

    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    def _standardize_date(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        normalized_candidates = [
            c.strip().replace(" ", "_").replace("-", "_").replace("/", "_").replace(".", "_").lower()
            for c in self.config.date_col_candidates
        ]

        date_col = None
        for candidate in normalized_candidates:
            if candidate in df.columns:
                date_col = candidate
                break

        if date_col is None:
            raise ValueError(
                f"날짜 컬럼을 찾지 못했습니다. 후보: {self.config.date_col_candidates}, 실제 컬럼: {list(df.columns)}"
            )

        df = df.rename(columns={date_col: self.config.required_date_col})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        df = df.dropna(subset=["date"])
        df["date"] = df["date"].dt.normalize()

        return df

    def _sort_and_deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df = df.sort_values("date")
        df = df.drop_duplicates(subset=["date"], keep="last")
        df = df.reset_index(drop=True)
        return df

    # --------------------------------------------------------
    # Numeric cleaning
    # --------------------------------------------------------

    def _coerce_numeric_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in df.columns:
            if col in self.config.protected_cols:
                continue

            df[col] = df[col].apply(self._parse_numeric_value)

        return df

    def _parse_numeric_value(self, value):
        if pd.isna(value):
            return np.nan

        if isinstance(value, (int, float, np.integer, np.floating)):
            return value

        text = str(value).strip()

        if text in ("", "-", "N/A", "NA", "nan", "None", "null"):
            return np.nan

        text = text.replace(",", "")
        text = text.replace("%", "")

        text = re.sub(r"[^0-9.\-+eE]", "", text)

        if text in ("", "-", "+", "."):
            return np.nan

        try:
            return float(text)
        except ValueError:
            return np.nan

    # --------------------------------------------------------
    # Daily index
    # --------------------------------------------------------

    def _reindex_daily(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df = df.set_index("date").sort_index()

        full_index = pd.date_range(
            start=df.index.min(),
            end=df.index.max(),
            freq="D",
        )

        df = df.reindex(full_index)
        df.index.name = "date"
        df = df.reset_index()

        return df

    # --------------------------------------------------------
    # Feature creation
    # --------------------------------------------------------

    def _create_return_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for price_col, ret_col in self.config.ret_pairs.items():
            if price_col in df.columns and ret_col not in df.columns:
                df[ret_col] = df[price_col].pct_change() * 100

        return df

    def _create_spread_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        if "kr_10y_yield" in df.columns and "kr_3y_yield" in df.columns:
            if "term_spread_10y_3y" not in df.columns:
                df["term_spread_10y_3y"] = df["kr_10y_yield"] - df["kr_3y_yield"]

        return df

    # --------------------------------------------------------
    # Missing
    # --------------------------------------------------------

    def _fill_missing_values(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        numeric_cols = self._get_numeric_cols(df)

        if self.config.fill_daily_method == "ffill":
            df[numeric_cols] = df[numeric_cols].ffill()

        df[numeric_cols] = df[numeric_cols].bfill()

        return df

    # --------------------------------------------------------
    # Z-score
    # --------------------------------------------------------

    def _create_rolling_zscores(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        numeric_cols = self._get_numeric_cols(df)

        base_cols = [
            col for col in numeric_cols
            if not col.endswith("_z")
            and not col.endswith("_rolling_mean")
            and not col.endswith("_rolling_std")
        ]

        for col in base_cols:
            rolling_mean = df[col].rolling(
                window=self.config.rolling_window,
                min_periods=self.config.min_periods,
            ).mean()

            rolling_std = df[col].rolling(
                window=self.config.rolling_window,
                min_periods=self.config.min_periods,
            ).std()

            z_col = f"{col}_z"
            df[z_col] = (df[col] - rolling_mean) / rolling_std.replace(0, np.nan)

        return df

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    def _build_quality_report(
        self,
        df: pd.DataFrame,
        original_shape: Tuple[int, int],
        original_cols: List[str],
    ) -> pd.DataFrame:
        rows = []

        for col in df.columns:
            missing_count = int(df[col].isna().sum())
            missing_ratio = float(df[col].isna().mean())

            rows.append({
                "column": col,
                "dtype": str(df[col].dtype),
                "missing_count": missing_count,
                "missing_ratio": round(missing_ratio, 6),
                "unique_count": int(df[col].nunique(dropna=True)),
                "original_rows": original_shape[0],
                "original_cols": original_shape[1],
                "cleaned_rows": df.shape[0],
                "cleaned_cols": df.shape[1],
            })

        report = pd.DataFrame(rows)

        meta = {
            "original_shape": original_shape,
            "cleaned_shape": df.shape,
            "original_columns": original_cols,
            "cleaned_columns": list(df.columns),
            "date_min": str(df["date"].min().date()) if "date" in df.columns else None,
            "date_max": str(df["date"].max().date()) if "date" in df.columns else None,
        }

        report.attrs["meta"] = meta

        return report

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    def _save_outputs(self, df: pd.DataFrame, report: pd.DataFrame) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.report_path.parent.mkdir(parents=True, exist_ok=True)

        df.to_csv(self.config.output_path, index=False, encoding="utf-8-sig")
        report.to_csv(self.config.report_path, index=False, encoding="utf-8-sig")

        meta_path = self.config.report_path.with_suffix(".meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(report.attrs.get("meta", {}), f, ensure_ascii=False, indent=2)

        print("=" * 80)
        print("[정제 완료]")
        print(f"cleaned_csv : {self.config.output_path}")
        print(f"report_csv  : {self.config.report_path}")
        print(f"meta_json   : {meta_path}")
        print(f"shape       : {df.shape}")
        print("=" * 80)

    # --------------------------------------------------------
    # Utils
    # --------------------------------------------------------

    def _get_numeric_cols(self, df: pd.DataFrame) -> List[str]:
        numeric_cols = []

        for col in df.columns:
            if col in self.config.protected_cols:
                continue

            # bool 컬럼 제외
            if pd.api.types.is_bool_dtype(df[col]):
                continue

            # datetime 컬럼 제외
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                continue

            # 숫자형 컬럼만 사용
            if pd.api.types.is_numeric_dtype(df[col]):
                numeric_cols.append(col)

        return numeric_cols


# ============================================================
# 3. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input-path",
        type=str,
        default="data/raw/macro_signal_daily.csv",
    )

    parser.add_argument(
        "--output-path",
        type=str,
        default="data/processed/macro_signal_daily_cleaned.csv",
    )

    parser.add_argument(
        "--report-path",
        type=str,
        default="data/processed/macro_signal_daily_quality_report.csv",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = MacroCleanConfig(
        input_path=Path(args.input_path),
        output_path=Path(args.output_path),
        report_path=Path(args.report_path),
    )

    cleaner = MacroSignalCleaner(config)
    cleaner.run()


if __name__ == "__main__":
    main()
