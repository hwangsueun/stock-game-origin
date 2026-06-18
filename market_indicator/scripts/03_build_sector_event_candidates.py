from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


class SectorEventConfig:
    START_DATE = "2013-01-01"
    END_DATE = "2023-12-31"

    SECTOR_LONG_PATH = "data/raw/kr_sector_indices_long_20130101_20231231.csv"
    MACRO_SIGNAL_PATH = "data/processed/macro_signal_daily.csv"

    OUTPUT_DIR = "data/processed"

    FORBIDDEN_TERMS = (
        "장중 급등|장 초반 강세|시초가|고가|저가|고점 대비 하락|저점 반등|"
        "갭상승|갭하락|장 마감 직전 매수세"
    )


class SectorContextBuilder:
    def __init__(self, config: SectorEventConfig):
        self.config = config
        self.output_dir = Path(config.OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        sector_df = self._load_sector_long()
        macro_df = self._load_macro_signal()

        context_df = self._build_sector_context(sector_df, macro_df)
        rank_df = self._build_daily_rank(context_df)
        event_df = self._build_sector_events(context_df, rank_df)

        self._save(context_df, rank_df, event_df)

    def _load_sector_long(self) -> pd.DataFrame:
        path = Path(self.config.SECTOR_LONG_PATH)

        if not path.exists():
            raise FileNotFoundError(path)

        df = pd.read_csv(path)
        required_cols = [
            "date",
            "market",
            "index_code",
            "index_name",
            "index_slug",
            "close",
        ]

        missing = [col for col in required_cols if col not in df.columns]
        if missing:
            raise ValueError(f"업종지수 파일에 필수 컬럼 없음: {missing} / 현재 컬럼={list(df.columns)}")

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        df = df[
            (df["date"] >= pd.Timestamp(self.config.START_DATE)) &
            (df["date"] <= pd.Timestamp(self.config.END_DATE))
        ].copy()

        numeric_cols = ["close", "volume", "trading_value", "market_cap"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = self._to_numeric(df[col])

        df["market"] = df["market"].astype(str).str.upper().str.strip()
        df["index_code"] = df["index_code"].astype(str).str.strip()
        df["index_name"] = df["index_name"].astype(str).str.strip()
        df["index_slug"] = df["index_slug"].astype(str).str.strip()

        df = df.sort_values(["market", "index_code", "date"]).reset_index(drop=True)

        return df

    def _load_macro_signal(self) -> pd.DataFrame:
        path = Path(self.config.MACRO_SIGNAL_PATH)

        if not path.exists():
            raise FileNotFoundError(
                f"{path} 없음. 먼저 scripts/02_build_macro_event_candidates.py 실행 필요"
            )

        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        keep_cols = [
            "date",
            "kospi_ret_1d",
            "kospi_ret_5d",
            "kospi_ret_20d",
            "kosdaq_ret_1d",
            "kosdaq_ret_5d",
            "kosdaq_ret_20d",
        ]

        existing = [col for col in keep_cols if col in df.columns]
        df = df[existing].copy()

        for col in df.columns:
            if col != "date":
                df[col] = self._to_numeric(df[col])

        return df

    def _build_sector_context(
        self,
        sector_df: pd.DataFrame,
        macro_df: pd.DataFrame,
    ) -> pd.DataFrame:
        df = sector_df.copy()

        group_cols = ["market", "index_code"]

        df["sector_return_1d"] = (
            df.groupby(group_cols)["close"].pct_change(1) * 100
        )
        df["sector_return_5d"] = (
            df.groupby(group_cols)["close"].pct_change(5) * 100
        )
        df["sector_return_20d"] = (
            df.groupby(group_cols)["close"].pct_change(20) * 100
        )

        df = df.merge(macro_df, on="date", how="left")

        df["market_return_1d"] = np.where(
            df["market"].eq("KOSPI"),
            df.get("kospi_ret_1d"),
            df.get("kosdaq_ret_1d"),
        )
        df["market_return_5d"] = np.where(
            df["market"].eq("KOSPI"),
            df.get("kospi_ret_5d"),
            df.get("kosdaq_ret_5d"),
        )
        df["market_return_20d"] = np.where(
            df["market"].eq("KOSPI"),
            df.get("kospi_ret_20d"),
            df.get("kosdaq_ret_20d"),
        )

        df["relative_return_vs_market_1d"] = (
            df["sector_return_1d"] - df["market_return_1d"]
        )
        df["relative_return_vs_market_5d"] = (
            df["sector_return_5d"] - df["market_return_5d"]
        )
        df["relative_return_vs_market_20d"] = (
            df["sector_return_20d"] - df["market_return_20d"]
        )

        keep_cols = [
            "date",
            "market",
            "index_code",
            "index_name",
            "index_slug",
            "close",
            "volume",
            "trading_value",
            "market_cap",
            "sector_return_1d",
            "sector_return_5d",
            "sector_return_20d",
            "market_return_1d",
            "market_return_5d",
            "market_return_20d",
            "relative_return_vs_market_1d",
            "relative_return_vs_market_5d",
            "relative_return_vs_market_20d",
        ]

        existing = [col for col in keep_cols if col in df.columns]
        df = df[existing].copy()

        df = df.replace([np.inf, -np.inf], np.nan)
        df = df.sort_values(["date", "market", "index_name"]).reset_index(drop=True)

        return df

    def _build_daily_rank(self, context_df: pd.DataFrame) -> pd.DataFrame:
        df = context_df.copy()

        df["rank_return_1d_desc"] = (
            df.groupby(["date", "market"])["sector_return_1d"]
            .rank(method="first", ascending=False)
        )
        df["rank_return_1d_asc"] = (
            df.groupby(["date", "market"])["sector_return_1d"]
            .rank(method="first", ascending=True)
        )
        df["rank_relative_1d_desc"] = (
            df.groupby(["date", "market"])["relative_return_vs_market_1d"]
            .rank(method="first", ascending=False)
        )
        df["rank_relative_1d_asc"] = (
            df.groupby(["date", "market"])["relative_return_vs_market_1d"]
            .rank(method="first", ascending=True)
        )

        df["sector_count_in_market"] = (
            df.groupby(["date", "market"])["index_code"].transform("count")
        )

        rank_cols = [
            "date",
            "market",
            "index_code",
            "index_name",
            "index_slug",
            "sector_return_1d",
            "sector_return_5d",
            "market_return_1d",
            "relative_return_vs_market_1d",
            "rank_return_1d_desc",
            "rank_return_1d_asc",
            "rank_relative_1d_desc",
            "rank_relative_1d_asc",
            "sector_count_in_market",
        ]

        return df[rank_cols].copy()

    def _build_sector_events(
        self,
        context_df: pd.DataFrame,
        rank_df: pd.DataFrame,
    ) -> pd.DataFrame:
        df = context_df.merge(
            rank_df[
                [
                    "date",
                    "market",
                    "index_code",
                    "rank_return_1d_desc",
                    "rank_return_1d_asc",
                    "rank_relative_1d_desc",
                    "rank_relative_1d_asc",
                    "sector_count_in_market",
                ]
            ],
            on=["date", "market", "index_code"],
            how="left",
        )

        events: List[Dict] = []

        for _, row in df.iterrows():
            events.extend(self._make_events_for_row(row))

        event_df = pd.DataFrame(events)

        if event_df.empty:
            return event_df

        event_df = event_df.sort_values(
            ["date", "strength"],
            ascending=[True, False],
        ).reset_index(drop=True)

        return event_df

    def _make_events_for_row(self, row: pd.Series) -> List[Dict]:
        events = []

        date = row["date"]
        market = self._safe_str(row.get("market"))
        index_name = self._safe_str(row.get("index_name"))
        index_code = self._safe_str(row.get("index_code"))
        asset_id = f"{market}_{index_name}_{index_code}"

        ret_1d = self._get(row, "sector_return_1d")
        ret_5d = self._get(row, "sector_return_5d")
        ret_20d = self._get(row, "sector_return_20d")
        market_ret_1d = self._get(row, "market_return_1d")
        rel_1d = self._get(row, "relative_return_vs_market_1d")
        rel_5d = self._get(row, "relative_return_vs_market_5d")

        rank_up = self._get(row, "rank_return_1d_desc")
        rank_down = self._get(row, "rank_return_1d_asc")
        rel_rank_up = self._get(row, "rank_relative_1d_desc")
        rel_rank_down = self._get(row, "rank_relative_1d_asc")

        if ret_1d is not None and abs(ret_1d) >= 2.0:
            direction = "positive" if ret_1d > 0 else "negative"
            frame = (
                f"{market} {index_name} 업종지수가 종가 기준 {ret_1d:.2f}% 변동하며 "
                f"업종 전반의 방향성을 보여줌"
            )

            events.append(
                self._event(
                    date=date,
                    asset_class="국내 업종",
                    asset_id=asset_id,
                    market=market,
                    sector=index_name,
                    event_type="sector_close_move",
                    event_frame=frame,
                    direction=direction,
                    strength=self._strength(abs(ret_1d), [2.0, 3.5, 5.0]),
                    evidence_1=f"업종 1일 수익률 {ret_1d:.2f}%",
                    evidence_2=self._fmt("시장 1일 수익률", market_ret_1d, "%"),
                    evidence_3=self._fmt("시장 대비 초과수익률", rel_1d, "%"),
                    news_style="sector_summary",
                )
            )

        if ret_5d is not None and abs(ret_5d) >= 5.0:
            direction = "positive" if ret_5d > 0 else "negative"
            frame = (
                f"{market} {index_name} 업종지수가 최근 5거래일 기준 {ret_5d:.2f}% 변동하며 "
                f"단기 업종 추세가 부각됨"
            )

            events.append(
                self._event(
                    date=date,
                    asset_class="국내 업종",
                    asset_id=asset_id,
                    market=market,
                    sector=index_name,
                    event_type="sector_5d_trend",
                    event_frame=frame,
                    direction=direction,
                    strength=self._strength(abs(ret_5d), [5.0, 8.0, 12.0]),
                    evidence_1=f"업종 5거래일 수익률 {ret_5d:.2f}%",
                    evidence_2=self._fmt("업종 20거래일 수익률", ret_20d, "%"),
                    evidence_3=self._fmt("시장 대비 5거래일 초과수익률", rel_5d, "%"),
                    news_style="sector_trend",
                )
            )

        if rel_1d is not None and abs(rel_1d) >= 1.5 and ret_1d is not None:
            direction = "positive" if rel_1d > 0 else "negative"
            frame = (
                f"{market} {index_name} 업종은 종가 기준 시장 대비 {rel_1d:.2f}%p "
                f"{'강세' if rel_1d > 0 else '부진'}을 보임"
            )

            events.append(
                self._event(
                    date=date,
                    asset_class="국내 업종",
                    asset_id=asset_id,
                    market=market,
                    sector=index_name,
                    event_type="sector_relative_move",
                    event_frame=frame,
                    direction=direction,
                    strength=self._strength(abs(rel_1d), [1.5, 2.5, 4.0]),
                    evidence_1=f"업종 1일 수익률 {ret_1d:.2f}%",
                    evidence_2=self._fmt("시장 1일 수익률", market_ret_1d, "%"),
                    evidence_3=f"시장 대비 초과수익률 {rel_1d:.2f}%p",
                    news_style="sector_relative_summary",
                )
            )

        if rank_up == 1 and rel_rank_up == 1 and rel_1d is not None and rel_1d >= 0.8:
            frame = (
                f"{market} {index_name} 업종이 해당 시장 내 수익률과 시장 대비 성과에서 "
                f"상위권을 기록함"
            )

            events.append(
                self._event(
                    date=date,
                    asset_class="국내 업종",
                    asset_id=asset_id,
                    market=market,
                    sector=index_name,
                    event_type="sector_leader",
                    event_frame=frame,
                    direction="positive",
                    strength=self._strength(abs(rel_1d), [0.8, 1.5, 3.0]),
                    evidence_1=f"시장 내 1일 수익률 순위 {int(rank_up)}위",
                    evidence_2=f"시장 대비 성과 순위 {int(rel_rank_up)}위",
                    evidence_3=f"시장 대비 초과수익률 {rel_1d:.2f}%p",
                    news_style="sector_leader",
                )
            )

        if rank_down == 1 and rel_rank_down == 1 and rel_1d is not None and rel_1d <= -0.8:
            frame = (
                f"{market} {index_name} 업종이 해당 시장 내 수익률과 시장 대비 성과에서 "
                f"하위권을 기록함"
            )

            events.append(
                self._event(
                    date=date,
                    asset_class="국내 업종",
                    asset_id=asset_id,
                    market=market,
                    sector=index_name,
                    event_type="sector_laggard",
                    event_frame=frame,
                    direction="negative",
                    strength=self._strength(abs(rel_1d), [0.8, 1.5, 3.0]),
                    evidence_1=f"시장 내 1일 수익률 하위 1위",
                    evidence_2=f"시장 대비 성과 하위 1위",
                    evidence_3=f"시장 대비 초과수익률 {rel_1d:.2f}%p",
                    news_style="sector_laggard",
                )
            )

        return events

    def _event(
        self,
        date: pd.Timestamp,
        asset_class: str,
        asset_id: str,
        market: str,
        sector: str,
        event_type: str,
        event_frame: str,
        direction: str,
        strength: int,
        evidence_1: Optional[str],
        evidence_2: Optional[str],
        evidence_3: Optional[str],
        news_style: str,
    ) -> Dict:
        return {
            "date": date.strftime("%Y-%m-%d"),
            "asset_class": asset_class,
            "asset_id": asset_id,
            "market": market,
            "sector": sector,
            "event_type": event_type,
            "event_frame": event_frame,
            "direction": direction,
            "strength": strength,
            "evidence_1": evidence_1,
            "evidence_2": evidence_2,
            "evidence_3": evidence_3,
            "news_style": news_style,
            "forbidden_terms": self.config.FORBIDDEN_TERMS,
        }

    def _save(
        self,
        context_df: pd.DataFrame,
        rank_df: pd.DataFrame,
        event_df: pd.DataFrame,
    ) -> None:
        context_path = self.output_dir / "sector_context_daily.csv"
        rank_path = self.output_dir / "sector_daily_rank.csv"
        event_path = self.output_dir / "sector_event_candidates_daily.csv"

        context_df.to_csv(context_path, index=False, encoding="utf-8-sig")
        rank_df.to_csv(rank_path, index=False, encoding="utf-8-sig")
        event_df.to_csv(event_path, index=False, encoding="utf-8-sig")

        print("=" * 80)
        print(f"[저장 완료] {context_path}")
        print(f"[저장 완료] {rank_path}")
        print(f"[저장 완료] {event_path}")
        print(f"[sector context rows] {len(context_df):,}")
        print(f"[sector event rows] {len(event_df):,}")
        print("=" * 80)

        if not event_df.empty:
            print("[이벤트 샘플]")
            print(event_df.head(30))

    def _to_numeric(self, series: pd.Series) -> pd.Series:
        s = series.astype(str).str.strip()
        s = s.str.replace(",", "", regex=False)
        s = s.str.replace("%", "", regex=False)
        s = s.replace({"": None, "-": None, "nan": None, "None": None})
        return pd.to_numeric(s, errors="coerce")

    def _get(self, row: pd.Series, col: str) -> Optional[float]:
        if col not in row.index:
            return None

        value = row[col]

        if pd.isna(value):
            return None

        try:
            return float(value)
        except Exception:
            return None

    def _safe_str(self, value) -> str:
        if pd.isna(value):
            return ""
        return str(value).strip()

    def _fmt(self, label: str, value: Optional[float], suffix: str) -> Optional[str]:
        if value is None:
            return None
        return f"{label} {value:.2f}{suffix}"

    def _strength(self, value: float, thresholds: List[float]) -> int:
        if value >= thresholds[2]:
            return 5
        if value >= thresholds[1]:
            return 4
        if value >= thresholds[0]:
            return 3
        return 2


if __name__ == "__main__":
    config = SectorEventConfig()
    builder = SectorContextBuilder(config)
    builder.run()
    