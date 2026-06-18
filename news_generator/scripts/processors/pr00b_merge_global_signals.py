# ============================================================
# pr00b_merge_global_signals.py
# 글로벌 지표(S&P500, 나스닥, 미국채, 미국 기준금리)를
# macro_signal_daily.csv에 병합한 후 저장
#
# 실행 순서:
#   1. python pr00b_merge_global_signals.py   ← 이 스크립트
#   2. python pr00_clean_macro_signal_daily.py ← z-score 등 정제
#   3. python pr04_build_macro_news_generation_input.py
#   4. python pr05_generate_macro_news_from_llm.py
# ============================================================

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 1. Config
# ============================================================

@dataclass
class GlobalMergeConfig:
    # 기존 macro_signal_daily.csv 경로
    base_path: Path

    # 글로벌 지표 raw 파일 경로들
    sp500_path: Path
    nasdaq_path: Path
    us_10y_path: Path
    us_2y_path: Path
    us_policy_rate_path: Path

    # 출력 경로 (기본: base_path 덮어쓰기)
    output_path: Optional[Path] = None

    # 리포트 경로
    report_path: Path = Path("data/processed/pr00b_merge_report.csv")

    date_col: str = "date"


# ============================================================
# 2. Source 정의
# ============================================================

@dataclass
class GlobalSource:
    """단일 글로벌 지표 파일 설명"""
    path: Path
    close_col: str        # 병합 후 컬럼명
    ret_col: str          # 수익률 컬럼명 (빈 문자열이면 생성 안 함)
    raw_price_col: str = "adj_close"   # 원본 파일의 가격 컬럼명


# ============================================================
# 3. Merger
# ============================================================

class GlobalSignalMerger:
    def __init__(self, config: GlobalMergeConfig):
        self.config = config
        self.output_path = config.output_path or config.base_path

        self.sources: List[GlobalSource] = [
            GlobalSource(
                path=config.sp500_path,
                close_col="sp500_close",
                ret_col="sp500_ret_1d",
            ),
            GlobalSource(
                path=config.nasdaq_path,
                close_col="nasdaq_close",
                ret_col="nasdaq_ret_1d",
            ),
            GlobalSource(
                path=config.us_10y_path,
                close_col="us_10y_yield",
                ret_col="",   # 금리는 수익률 대신 스프레드만 사용
            ),
            GlobalSource(
                path=config.us_2y_path,
                close_col="us_2y_yield",
                ret_col="",
            ),
            GlobalSource(
                path=config.us_policy_rate_path,
                close_col="us_policy_rate",
                ret_col="",
            ),
        ]

    def run(self) -> None:
        base = self._load_base()
        print(f"[base] {base.shape}, 날짜 범위: {base['date'].min().date()} ~ {base['date'].max().date()}")

        for source in self.sources:
            base = self._merge_source(base, source)

        base = self._create_derived_columns(base)
        base = self._forward_fill_global_cols(base)

        self._save(base)
        self._print_summary(base)

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------

    def _load_base(self) -> pd.DataFrame:
        if not self.config.base_path.exists():
            raise FileNotFoundError(f"base 파일 없음: {self.config.base_path}")

        try:
            df = pd.read_csv(self.config.base_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(self.config.base_path, encoding="cp949")

        df[self.config.date_col] = pd.to_datetime(df[self.config.date_col], errors="coerce")
        df = df.dropna(subset=[self.config.date_col])
        df = df.sort_values(self.config.date_col).reset_index(drop=True)

        return df

    def _load_source(self, source: GlobalSource) -> pd.DataFrame:
        if not source.path.exists():
            raise FileNotFoundError(f"글로벌 지표 파일 없음: {source.path}")

        try:
            df = pd.read_csv(source.path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(source.path, encoding="cp949")

        # 컬럼명 소문자 정규화
        df.columns = [c.strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]

        # date 컬럼 파싱
        date_candidates = ["date", "날짜", "base_date"]
        date_col = next((c for c in date_candidates if c in df.columns), None)
        if date_col is None:
            raise ValueError(f"{source.path} 에서 date 컬럼을 찾을 수 없음. 컬럼: {list(df.columns)}")

        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.sort_values("date").reset_index(drop=True)

        # adj_close 컬럼 존재 확인
        price_col = source.raw_price_col
        if price_col not in df.columns:
            # volume 컬럼을 제외하고 숫자형 컬럼 중 첫 번째를 시도
            numeric_cols = [
                c for c in df.columns
                if c not in ("date",) and "volume" not in c
                and pd.api.types.is_numeric_dtype(df[c])
            ]
            if not numeric_cols:
                raise ValueError(f"{source.path} 에서 가격 컬럼을 찾을 수 없음.")
            price_col = numeric_cols[0]
            print(f"  [경고] adj_close 없음 → {price_col} 사용: {source.path.name}")

        df = df[["date", price_col]].rename(columns={price_col: source.close_col})

        return df

    # --------------------------------------------------------
    # Merge
    # --------------------------------------------------------

    def _merge_source(self, base: pd.DataFrame, source: GlobalSource) -> pd.DataFrame:
        # 이미 컬럼이 있으면 덮어쓰지 않고 스킵
        if source.close_col in base.columns:
            print(f"  [스킵] {source.close_col} 이미 존재")
            return base

        src_df = self._load_source(source)
        print(f"  [로드] {source.path.name} → {source.close_col} ({len(src_df)}행)")

        base = base.merge(src_df, on="date", how="left")

        # 수익률 컬럼 생성
        if source.ret_col:
            base[source.ret_col] = base[source.close_col].pct_change() * 100

        return base

    # --------------------------------------------------------
    # Derived columns
    # --------------------------------------------------------

    def _create_derived_columns(self, base: pd.DataFrame) -> pd.DataFrame:
        base = base.copy()

        # 미국 장단기 금리 스프레드 (경기 선행 지표)
        if "us_10y_yield" in base.columns and "us_2y_yield" in base.columns:
            if "us_term_spread_10y_2y" not in base.columns:
                base["us_term_spread_10y_2y"] = base["us_10y_yield"] - base["us_2y_yield"]
                print("  [파생] us_term_spread_10y_2y 생성")

        return base

    # --------------------------------------------------------
    # Forward fill (글로벌 휴장일 처리)
    # --------------------------------------------------------

    def _forward_fill_global_cols(self, base: pd.DataFrame) -> pd.DataFrame:
        """
        미국 시장은 한국과 휴장일이 달라 left join 후 NaN이 생김.
        글로벌 지표 컬럼만 ffill 적용.
        수익률 컬럼은 ffill 대신 0.0으로 채움 (휴장일 수익률 = 0).
        """
        base = base.copy()

        global_close_cols = [
            "sp500_close", "nasdaq_close",
            "us_10y_yield", "us_2y_yield",
            "us_policy_rate", "us_term_spread_10y_2y",
        ]
        global_ret_cols = [
            "sp500_ret_1d", "nasdaq_ret_1d",
        ]

        for col in global_close_cols:
            if col in base.columns:
                base[col] = base[col].ffill()

        for col in global_ret_cols:
            if col in base.columns:
                # ffill 후 여전히 NaN이면 0 (휴장일)
                base[col] = base[col].ffill()
                base[col] = base[col].fillna(0.0)

        return base

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    def _save(self, df: pd.DataFrame) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.output_path, index=False, encoding="utf-8-sig")

    def _print_summary(self, df: pd.DataFrame) -> None:
        added_cols = [
            "sp500_close", "sp500_ret_1d",
            "nasdaq_close", "nasdaq_ret_1d",
            "us_10y_yield", "us_2y_yield",
            "us_policy_rate", "us_term_spread_10y_2y",
        ]

        print("=" * 80)
        print("[pr00b 완료] 글로벌 지표 병합")
        print(f"output     : {self.output_path}")
        print(f"shape      : {df.shape}")
        print(f"날짜 범위  : {df['date'].min().date()} ~ {df['date'].max().date()}")
        print()
        print("[추가된 컬럼 결측률]")
        for col in added_cols:
            if col in df.columns:
                missing = df[col].isna().mean()
                print(f"  {col:<30} {missing:.1%}")
        print("=" * 80)
        print()
        print("다음 단계:")
        print("  python scripts/processors/pr00_clean_macro_signal_daily.py")


# ============================================================
# 4. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="글로벌 지표를 macro_signal_daily.csv에 병합"
    )

    parser.add_argument(
        "--base-path",
        type=str,
        default="data/raw/macro_signal_daily.csv",
    )
    parser.add_argument(
        "--sp500-path",
        type=str,
        default="data/raw/sp500_20130101_20231231.csv",
    )
    parser.add_argument(
        "--nasdaq-path",
        type=str,
        default="data/raw/nasdaq_20130101_20231231.csv",
    )
    parser.add_argument(
        "--us-10y-path",
        type=str,
        default="data/raw/us_treasury_10y_20130101_20231231.csv",
    )
    parser.add_argument(
        "--us-2y-path",
        type=str,
        default="data/raw/us_treasury_2y_20130101_20231231.csv",
    )
    parser.add_argument(
        "--us-policy-rate-path",
        type=str,
        default="data/raw/us_policy_rate_20130101_20231231.csv",
    )
    parser.add_argument(
        "--output-path",
        type=str,
        default=None,
        help="미지정 시 base-path 덮어쓰기",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default="data/processed/pr00b_merge_report.csv",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = GlobalMergeConfig(
        base_path=Path(args.base_path),
        sp500_path=Path(args.sp500_path),
        nasdaq_path=Path(args.nasdaq_path),
        us_10y_path=Path(args.us_10y_path),
        us_2y_path=Path(args.us_2y_path),
        us_policy_rate_path=Path(args.us_policy_rate_path),
        output_path=Path(args.output_path) if args.output_path else None,
        report_path=Path(args.report_path),
    )

    merger = GlobalSignalMerger(config)
    merger.run()


if __name__ == "__main__":
    main()
