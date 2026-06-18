from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


class MacroSignalConfig:
    START_DATE = "2013-01-01"
    END_DATE = "2023-12-31"

    PRICE_COLS = [
        "kospi",
        "kosdaq",
        "nasdaq",
        "sp500",
        "gold_price",
        "usdkrw",
        "wti_price",
        "dubai_oil_price",
    ]

    RATE_COLS = [
        "kr_policy_rate",
        "us_policy_rate",
        "ktb_3y_rate",
        "ktb_5y_rate",
        "ktb_10y_rate",
        "corp_aa_minus_3y_rate",
        "corp_bbb_minus_3y_rate",
        "cd_91d_rate",
        "us_treasury_2y_rate",
        "us_treasury_5y_rate",
        "us_treasury_10y_rate",
        "us_treasury_30y_rate",
    ]

    SPREAD_COLS = [
        "corp_aa_minus_spread",
        "corp_bbb_minus_spread",
        "ktb_10y_3y_spread",
        "us_10y_2y_spread",
        "us_30y_2y_spread",
    ]

    MONTHLY_COLS = [
        "cpi",
        "leading_index",
        "export_amount_usd_thousand",
        "import_amount_usd_thousand",
        "trade_balance_usd_thousand",
        "industrial_production_index",
        "mining_manufacturing_production_index",
        "retail_sales_index",
        "facility_investment_index",
    ]


class MacroSignalBuilder:
    def __init__(
        self,
        input_path: str = "data/processed/macro_context_daily.csv",
        output_dir: str = "data/processed",
    ):
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.config = MacroSignalConfig()

    def run(self) -> None:
        df = self._load()
        signal_df = self._build_signals(df)
        event_df = self._build_events(signal_df)

        signal_path = self.output_dir / "macro_signal_daily.csv"
        event_path = self.output_dir / "macro_event_candidates_daily.csv"

        signal_df.to_csv(signal_path, index=False, encoding="utf-8-sig")
        event_df.to_csv(event_path, index=False, encoding="utf-8-sig")

        print("=" * 80)
        print(f"[저장 완료] {signal_path}")
        print(f"[저장 완료] {event_path}")
        print(f"[signal rows] {len(signal_df):,}")
        print(f"[event rows] {len(event_df):,}")
        print("=" * 80)

        print("[이벤트 샘플]")
        print(event_df.head(30))

    def _load(self) -> pd.DataFrame:
        if not self.input_path.exists():
            raise FileNotFoundError(self.input_path)

        df = pd.read_csv(self.input_path)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.sort_values("date").reset_index(drop=True)

        df = df[
            (df["date"] >= pd.Timestamp(self.config.START_DATE)) &
            (df["date"] <= pd.Timestamp(self.config.END_DATE))
        ].copy()

        return df

    def _build_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        for col in self.config.PRICE_COLS:
            if col in out.columns:
                out[f"{col}_ret_1d"] = out[col].pct_change(1) * 100
                out[f"{col}_ret_5d"] = out[col].pct_change(5) * 100
                out[f"{col}_ret_20d"] = out[col].pct_change(20) * 100

        for col in self.config.RATE_COLS:
            if col in out.columns:
                out[f"{col}_chg_1d_bp"] = out[col].diff(1) * 100
                out[f"{col}_chg_5d_bp"] = out[col].diff(5) * 100
                out[f"{col}_chg_20d_bp"] = out[col].diff(20) * 100

        for col in self.config.SPREAD_COLS:
            if col in out.columns:
                out[f"{col}_chg_1d_bp"] = out[col].diff(1) * 100
                out[f"{col}_chg_20d_bp"] = out[col].diff(20) * 100

        # 월별 지표는 forward-fill되어 있으므로 값이 바뀐 날만 발표/갱신일처럼 취급
        for col in self.config.MONTHLY_COLS:
            if col in out.columns:
                out[f"{col}_mom"] = out[col].diff(1)
                out[f"{col}_changed_flag"] = out[col].diff(1).abs().fillna(0) > 0

        out = out.replace([np.inf, -np.inf], np.nan)

        return out

    def _build_events(self, df: pd.DataFrame) -> pd.DataFrame:
        events: List[Dict] = []

        for _, row in df.iterrows():
            date = row["date"]

            events.extend(self._market_price_events(row, date))
            events.extend(self._fx_commodity_events(row, date))
            events.extend(self._rate_events(row, date))
            events.extend(self._spread_events(row, date))
            events.extend(self._monthly_macro_events(row, date))

        event_df = pd.DataFrame(events)

        if event_df.empty:
            return event_df

        event_df = event_df.sort_values(
            ["date", "strength"],
            ascending=[True, False],
        ).reset_index(drop=True)

        return event_df

    def _market_price_events(self, row: pd.Series, date: pd.Timestamp) -> List[Dict]:
        events = []

        market_specs = [
            ("kospi", "국내 주식시장", "KOSPI"),
            ("kosdaq", "국내 성장주 시장", "KOSDAQ"),
            ("nasdaq", "미국 기술주 시장", "NASDAQ"),
            ("sp500", "미국 대형주 시장", "S&P500"),
        ]

        for col, asset_class, label in market_specs:
            ret_1d = self._get(row, f"{col}_ret_1d")
            ret_5d = self._get(row, f"{col}_ret_5d")

            if ret_1d is None:
                continue

            if abs(ret_1d) >= 1.5:
                direction = "positive" if ret_1d > 0 else "negative"
                frame = (
                    f"{label}가 종가 기준 {ret_1d:.2f}% 변동하며 "
                    f"시장 방향성에 영향을 줌"
                )

                events.append(
                    self._event(
                        date=date,
                        asset_class=asset_class,
                        asset_id=label,
                        event_type="market_close_move",
                        event_frame=frame,
                        direction=direction,
                        strength=self._strength_from_abs(abs(ret_1d), [1.5, 2.5, 4.0]),
                        evidence_1=f"1일 수익률 {ret_1d:.2f}%",
                        evidence_2=self._fmt_optional("5거래일 수익률", ret_5d, "%"),
                        evidence_3="장중 표현 없이 종가 기준으로만 해석",
                        news_style="market_summary",
                    )
                )

        return events

    def _fx_commodity_events(self, row: pd.Series, date: pd.Timestamp) -> List[Dict]:
        events = []

        specs = [
            ("usdkrw", "환율", "원/달러 환율", 0.8, "fx_move"),
            ("wti_price", "원자재", "WTI 유가", 2.5, "oil_move"),
            ("dubai_oil_price", "원자재", "Dubai 유가", 2.5, "oil_move"),
            ("gold_price", "원자재", "금 가격", 1.5, "safe_asset_move"),
        ]

        for col, asset_class, label, threshold, event_type in specs:
            ret_1d = self._get(row, f"{col}_ret_1d")
            ret_5d = self._get(row, f"{col}_ret_5d")

            if ret_1d is None:
                continue

            if abs(ret_1d) >= threshold:
                direction = "positive" if ret_1d > 0 else "negative"

                if col == "usdkrw":
                    frame = (
                        f"{label}이 전일 대비 {ret_1d:.2f}% 변동하며 "
                        f"수출주와 외화부채 부담 해석에 영향을 줌"
                    )
                elif "oil" in col or col == "wti_price":
                    frame = (
                        f"{label}가 전일 대비 {ret_1d:.2f}% 변동하며 "
                        f"정유·항공·운송 업종 해석에 영향을 줌"
                    )
                else:
                    frame = (
                        f"{label}이 전일 대비 {ret_1d:.2f}% 변동하며 "
                        f"안전자산 선호 흐름을 반영"
                    )

                events.append(
                    self._event(
                        date=date,
                        asset_class=asset_class,
                        asset_id=label,
                        event_type=event_type,
                        event_frame=frame,
                        direction=direction,
                        strength=self._strength_from_abs(abs(ret_1d), [threshold, threshold * 1.8, threshold * 3.0]),
                        evidence_1=f"1일 변화율 {ret_1d:.2f}%",
                        evidence_2=self._fmt_optional("5거래일 변화율", ret_5d, "%"),
                        evidence_3="가격 데이터 기반",
                        news_style="macro_to_sector",
                    )
                )

        return events

    def _rate_events(self, row: pd.Series, date: pd.Timestamp) -> List[Dict]:
        events = []

        specs = [
            ("kr_policy_rate", "한국 기준금리"),
            ("us_policy_rate", "미국 정책금리"),
            ("ktb_3y_rate", "국고채 3년 금리"),
            ("ktb_10y_rate", "국고채 10년 금리"),
            ("us_treasury_2y_rate", "미국 국채 2년 금리"),
            ("us_treasury_10y_rate", "미국 국채 10년 금리"),
        ]

        for col, label in specs:
            chg_1d = self._get(row, f"{col}_chg_1d_bp")
            chg_20d = self._get(row, f"{col}_chg_20d_bp")
            level = self._get(row, col)

            if chg_1d is None:
                continue

            if abs(chg_1d) >= 5:
                direction = "negative" if chg_1d > 0 else "positive"
                frame = (
                    f"{label}가 전일 대비 {chg_1d:.1f}bp 변동하며 "
                    f"채권형 자산과 성장주 할인율 부담에 영향을 줌"
                )

                events.append(
                    self._event(
                        date=date,
                        asset_class="금리",
                        asset_id=label,
                        event_type="rate_move",
                        event_frame=frame,
                        direction=direction,
                        strength=self._strength_from_abs(abs(chg_1d), [5, 10, 20]),
                        evidence_1=f"금리 수준 {level:.3f}%" if level is not None else None,
                        evidence_2=f"1일 변화 {chg_1d:.1f}bp",
                        evidence_3=self._fmt_optional("20거래일 변화", chg_20d, "bp"),
                        news_style="rate_market_summary",
                    )
                )

        return events

    def _spread_events(self, row: pd.Series, date: pd.Timestamp) -> List[Dict]:
        events = []

        specs = [
            ("corp_aa_minus_spread", "회사채 AA- 신용스프레드"),
            ("corp_bbb_minus_spread", "회사채 BBB- 신용스프레드"),
            ("ktb_10y_3y_spread", "국고채 10년-3년 스프레드"),
            ("us_10y_2y_spread", "미국 10년-2년 스프레드"),
        ]

        for col, label in specs:
            chg_20d = self._get(row, f"{col}_chg_20d_bp")
            level = self._get(row, col)

            if chg_20d is None:
                continue

            if abs(chg_20d) >= 10:
                direction = "negative" if chg_20d > 0 and "신용" in label else "neutral"

                frame = (
                    f"{label}가 최근 20거래일 기준 {chg_20d:.1f}bp 변동하며 "
                    f"채권시장 위험선호와 경기 해석에 영향을 줌"
                )

                events.append(
                    self._event(
                        date=date,
                        asset_class="채권",
                        asset_id=label,
                        event_type="spread_move",
                        event_frame=frame,
                        direction=direction,
                        strength=self._strength_from_abs(abs(chg_20d), [10, 20, 40]),
                        evidence_1=f"스프레드 수준 {level:.3f}%p" if level is not None else None,
                        evidence_2=f"20거래일 변화 {chg_20d:.1f}bp",
                        evidence_3="스프레드 확대는 위험 프리미엄 상승으로 해석 가능",
                        news_style="bond_summary",
                    )
                )

        return events

    def _monthly_macro_events(self, row: pd.Series, date: pd.Timestamp) -> List[Dict]:
        events = []

        specs = [
            ("cpi", "소비자물가", "inflation_update"),
            ("leading_index", "경기선행지수", "leading_index_update"),
            ("export_amount_usd_thousand", "수출금액", "trade_update"),
            ("import_amount_usd_thousand", "수입금액", "trade_update"),
            ("trade_balance_usd_thousand", "무역수지", "trade_balance_update"),
            ("industrial_production_index", "산업생산지수", "real_activity_update"),
            ("mining_manufacturing_production_index", "광공업생산지수", "real_activity_update"),
            ("retail_sales_index", "소매판매지수", "consumption_update"),
            ("facility_investment_index", "설비투자지수", "investment_update"),
        ]

        for col, label, event_type in specs:
            changed = bool(row.get(f"{col}_changed_flag", False))
            mom = self._get(row, f"{col}_mom")
            level = self._get(row, col)

            if not changed or mom is None:
                continue

            direction = "positive" if mom > 0 else "negative"

            frame = (
                f"{label}가 전월 대비 {mom:.2f} 변동하며 "
                f"경기 흐름 해석에 반영됨"
            )

            events.append(
                self._event(
                    date=date,
                    asset_class="거시지표",
                    asset_id=label,
                    event_type=event_type,
                    event_frame=frame,
                    direction=direction,
                    strength=self._strength_from_abs(abs(mom), [0.5, 1.5, 3.0]),
                    evidence_1=f"현재 값 {level:.2f}" if level is not None else None,
                    evidence_2=f"전월 대비 변화 {mom:.2f}",
                    evidence_3="월별 지표 갱신일 기준 이벤트",
                    news_style="macro_indicator_update",
                )
            )

        return events

    def _event(
        self,
        date: pd.Timestamp,
        asset_class: str,
        asset_id: str,
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
            "event_type": event_type,
            "event_frame": event_frame,
            "direction": direction,
            "strength": strength,
            "evidence_1": evidence_1,
            "evidence_2": evidence_2,
            "evidence_3": evidence_3,
            "news_style": news_style,
            "forbidden_terms": "장중 급등|장 초반 강세|시초가|고가|저가|고점 대비 하락|저점 반등|갭상승|갭하락|장 마감 직전 매수세",
        }

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

    def _strength_from_abs(self, value: float, thresholds: List[float]) -> int:
        if value >= thresholds[2]:
            return 5
        if value >= thresholds[1]:
            return 4
        if value >= thresholds[0]:
            return 3
        return 2

    def _fmt_optional(self, label: str, value: Optional[float], suffix: str) -> Optional[str]:
        if value is None:
            return None
        return f"{label} {value:.2f}{suffix}"


if __name__ == "__main__":
    builder = MacroSignalBuilder(
        input_path="data/processed/macro_context_daily.csv",
        output_dir="data/processed",
    )
    builder.run()