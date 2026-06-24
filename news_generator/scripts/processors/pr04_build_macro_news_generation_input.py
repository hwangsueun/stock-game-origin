# ============================================================
# pr04_build_macro_news_generation_input.py
# 정제된 거시 시그널 → LLM 뉴스 생성용 JSONL 생성
#
# 목적:
# - 2013~2023년 일별 거시뉴스 생성 입력 생성
# - 하루 10개 뉴스 생성을 전제로 macro_events 최소 10개 보장
# - 월별/저빈도 지표는 반복 뉴스 방지를 위해 값 변경일에만 핵심 이벤트 생성
# ============================================================

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 1. Config
# ============================================================

@dataclass
class MacroNewsInputConfig:
    input_path: Path
    output_path: Path
    report_path: Path
    macro_event_calendar_path: Optional[Path] = None
    official_release_calendar_path: Optional[Path] = None
    context_overlay_path: Optional[Path] = None

    year: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    # 하루 10개 뉴스 필수
    news_per_day: int = 10

    # 핵심 이벤트 판단 기준
    min_event_abs_z: float = 1.0
    strong_event_abs_z: float = 1.8

    # 보충 이벤트까지 포함할 때 최소 z 기준
    weak_event_abs_z: float = 0.3

    detail_length_min: int = 180
    detail_length_max: int = 230

    date_col: str = "date"

    allowed_angles: Tuple[str, ...] = (
        "macro_regime",
        "risk_sentiment",
        "money_flow",
        "external_pressure",
        "policy_inflation",
        "market_breadth",
    )




# ============================================================
# 2. Column Registry
# ============================================================

@dataclass
class MacroColumnRegistry:
    index_cols: Tuple[str, ...] = (
        "kospi_close",
        "kospi_ret_1d",
        "kospi_ret_5d",
        "kosdaq_close",
        "kosdaq_ret_1d",
        "kosdaq_ret_5d",
    )

    rate_cols: Tuple[str, ...] = (
        "kr_3y_yield",
        "kr_10y_yield",
        "term_spread_10y_3y",
    )

    fx_cols: Tuple[str, ...] = (
        "usdkrw",
        "usdkrw_ret_1d",
    )

    commodity_cols: Tuple[str, ...] = (
        "wti",
        "wti_ret_1d",
        "dubai_oil",
        "dubai_oil_ret_1d",
        "gold",
        "gold_ret_1d",
    )

    real_activity_cols: Tuple[str, ...] = (
        "industrial_production",
        "mining_manufacturing_production",
        "retail_sales",
        "facility_investment",
        "leading_index",
    )

    trade_cols: Tuple[str, ...] = (
        "export_amount",
        "import_amount",
        "trade_balance",
    )

    inflation_cols: Tuple[str, ...] = (
        "cpi",
    )

    gdelt_cols: Tuple[str, ...] = (
        "gdelt_macro_tone",
        "gdelt_macro_volume",
        "gdelt_employment_tone",
        "gdelt_trade_tone",
        "gdelt_inflation_tone",
        "gdelt_policy_tone",
    )

    global_cols: Tuple[str, ...] = (
        "sp500_close",
        "sp500_ret_1d",
        "nasdaq_close",
        "nasdaq_ret_1d",
        "us_10y_yield",
        "us_2y_yield",
        "us_term_spread_10y_2y",
        "us_policy_rate",
    )

    risk_cols: Tuple[str, ...] = (
        "kospi_ret_1d",
        "kosdaq_ret_1d",
        "usdkrw_ret_1d",
        "wti_ret_1d",
        "gold_ret_1d",
        "sp500_ret_1d",
        "nasdaq_ret_1d",
        "term_spread_10y_3y",
        "us_term_spread_10y_2y",
        "gdelt_macro_tone",
    )


# ============================================================
# 3. Event Entity
# ============================================================

@dataclass
class MacroEvent:
    event_id: str
    date: str
    macro_angle: str

    # LLM이 헤드라인을 직접 만들기 위한 핵심 실마리
    # summary처럼 완성된 문장이 아니라 "무슨 일이 일어났는지"만 짧게 기술
    angle_label: str          # 예: "코스피·코스닥 동반 하락"
    market_implication: str   # 예: "외국인 매도와 원화 약세가 겹치며 위험자산 회피 분위기"

    direction: str
    severity: str
    source_columns: List[str]
    evidence: Dict[str, Any]

    # LLM이 수치를 문장에 녹일 수 있도록 핵심 수치를 별도 추출
    key_figures: Dict[str, Any]

    event_role: str = "core"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "date": self.date,
            "macro_angle": self.macro_angle,
            "angle_label": self.angle_label,
            "market_implication": self.market_implication,
            "direction": self.direction,
            "severity": self.severity,
            "event_role": self.event_role,
            "key_figures": self.key_figures,
            "source_columns": self.source_columns,
            "evidence": self.evidence,
        }


# ============================================================
# 4. Builder
# ============================================================

class MacroNewsInputBuilder:
    def __init__(
        self,
        config: MacroNewsInputConfig,
        registry: Optional[MacroColumnRegistry] = None,
    ):
        self.config = config
        self.registry = registry or MacroColumnRegistry()
        self.calendar_events = self._load_macro_event_calendar()
        self.official_releases = self._load_official_release_calendar()
        self.context_overlays = self._load_context_overlays()

    def run(self) -> None:
        df = self._load()
        df = self._prepare(df)
        df = self._filter_trading_days(df)
        self._align_official_releases_to_trading_days(df)
        df = self._filter_period(df)

        records = []
        report_rows = []

        for _, row in df.iterrows():
            record = self._build_daily_record(row)
            records.append(record)

            macro_events = record["macro_events"]
            report_rows.append({
                "date": record["date"],
                "news_count_target": record["news_count_target"],
                "event_count": len(macro_events),
                "core_event_count": sum(1 for e in macro_events if e.get("event_role") == "core"),
                "support_event_count": sum(1 for e in macro_events if e.get("event_role") == "support"),
                "fallback_event_count": sum(1 for e in macro_events if e.get("event_role") == "fallback"),
                "angles": ",".join([e["macro_angle"] for e in macro_events]),
                "event_ids": ",".join([e["event_id"] for e in macro_events]),
            })

        self._save_jsonl(records)
        self._save_report(pd.DataFrame(report_rows))

        print("=" * 100)
        print("[pr04 완료] macro news generation input 생성")
        print(f"input      : {self.config.input_path}")
        print(f"output     : {self.config.output_path}")
        print(f"report     : {self.config.report_path}")
        print(f"trading_days: {len(records)}")
        print(f"news/day   : {self.config.news_per_day}")
        print("=" * 100)

    # --------------------------------------------------------
    # Load / Prepare
    # --------------------------------------------------------

    def _load(self) -> pd.DataFrame:
        if not self.config.input_path.exists():
            raise FileNotFoundError(f"입력 파일 없음: {self.config.input_path}")

        try:
            return pd.read_csv(self.config.input_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(self.config.input_path, encoding="cp949")

    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        if self.config.date_col not in df.columns:
            raise ValueError(f"date 컬럼 없음. 실제 컬럼: {list(df.columns)}")

        df[self.config.date_col] = pd.to_datetime(df[self.config.date_col], errors="coerce")
        df = df.dropna(subset=[self.config.date_col])
        df = df.sort_values(self.config.date_col)
        df = df.reset_index(drop=True)

        # market_indicator 산출물은 원천 중심 이름을 쓰고, 이 빌더는 기사 중심
        # canonical 이름을 쓴다. 값과 z-score를 함께 복사해 수집된 지표를 누락 없이 쓴다.
        aliases = {
            "kospi": "kospi_close",
            "kosdaq": "kosdaq_close",
            "gold_price": "gold",
            "gold_price_ret_1d": "gold_ret_1d",
            "wti_price": "wti",
            "wti_price_ret_1d": "wti_ret_1d",
            "ktb_3y_rate": "kr_3y_yield",
            "ktb_10y_rate": "kr_10y_yield",
            "ktb_10y_3y_spread": "term_spread_10y_3y",
        }
        for source, target in aliases.items():
            if source in df.columns and target not in df.columns:
                df[target] = df[source]
            source_z = f"{source}_z"
            target_z = f"{target}_z"
            if source_z in df.columns and target_z not in df.columns:
                df[target_z] = df[source_z]

        # Dubai 원유 파일은 전 기간에 걸쳐 주기적인 20~300% 비정상 점프가 있어
        # 기사 근거로 쓸 수 없다. WTI 마이너스 가격 전환일의 단순 수익률도 무의미하다.
        if "wti_ret_1d" in df.columns:
            invalid_wti_return = df["wti_ret_1d"].abs() > 50
            df.loc[invalid_wti_return, "wti_ret_1d"] = np.nan
            if "wti_ret_1d_z" in df.columns:
                df.loc[invalid_wti_return, "wti_ret_1d_z"] = np.nan

        # 이 파일들의 날짜는 실제 발표일이 아니라 참조월 1일이다. 발표일 매핑 없이
        # LLM에 노출하면 look-ahead가 생기므로, 안전한 일별 입력에서는 사용하지 않는다.
        unavailable_until_release_date_mapping = {
            "cpi",
            "leading_index",
            "export_amount_usd_thousand",
            "import_amount_usd_thousand",
            "trade_balance_usd_thousand",
            "industrial_production_index",
            "mining_manufacturing_production_index",
            "retail_sales_index",
            "facility_investment_index",
        }
        for col in unavailable_until_release_date_mapping:
            if col in df.columns:
                df[col] = np.nan
            z_col = f"{col}_z"
            if z_col in df.columns:
                df[z_col] = np.nan

        df = self._add_low_frequency_freshness_flags(df)

        return df

    def _load_macro_event_calendar(self) -> Dict[str, List[Dict[str, Any]]]:
        path = self.config.macro_event_calendar_path
        if path is None:
            return {}
        if not path.exists():
            raise FileNotFoundError(f"거시 사건 캘린더 없음: {path}")

        calendar = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
        required = {"event_date", "event_type", "title", "description", "direction", "severity"}
        missing = required - set(calendar.columns)
        if missing:
            raise ValueError(f"거시 사건 캘린더 필수 컬럼 없음: {sorted(missing)}")

        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for record in calendar.to_dict("records"):
            date = str(record["event_date"]).strip()
            if not date:
                continue
            by_date.setdefault(date, []).append(record)
        return by_date

    def _load_official_release_calendar(self) -> Dict[str, List[Dict[str, Any]]]:
        path = self.config.official_release_calendar_path
        if path is None:
            return {}
        if not path.exists():
            raise FileNotFoundError(f"공식 발표 캘린더 없음: {path}")

        calendar = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
        required = {
            "event_date", "source_release_date", "release_category", "region",
            "institution", "title", "description", "direction", "severity",
            "source_url", "key_figures_json", "verification_status",
        }
        missing = required - set(calendar.columns)
        if missing:
            raise ValueError(f"공식 발표 캘린더 필수 컬럼 없음: {sorted(missing)}")

        approved_domains = (
            "federalreserve.gov", "bok.or.kr", "bea.gov", "bls.gov",
            "kostat.go.kr", "motie.go.kr",
            # 국내 정책·법·정치 발표 레이어(build_policy_legal_releases.py)
            "nec.go.kr", "assembly.go.kr", "ccourt.go.kr", "ftc.go.kr",
            "fss.or.kr", "wikidata.org",
        )
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for record in calendar.to_dict("records"):
            if record["verification_status"] != "official_source_verified":
                continue
            source_url = record["source_url"].lower()
            if not source_url.startswith("https://") or not any(
                domain in source_url for domain in approved_domains
            ):
                raise ValueError(f"허용되지 않은 공식 발표 URL: {record['source_url']}")
            try:
                record["key_figures"] = json.loads(record["key_figures_json"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"공식 발표 key_figures_json 오류: {record['title']}") from exc
            event_date = record["event_date"].strip()
            if event_date:
                by_date.setdefault(event_date, []).append(record)
        return by_date

    def _load_context_overlays(self) -> Dict[str, List[Dict[str, Any]]]:
        path = self.config.context_overlay_path
        if path is None:
            return {}
        if not path.exists():
            raise FileNotFoundError(f"거시 컨텍스트 오버레이 없음: {path}")

        overlays: Dict[str, List[Dict[str, Any]]] = {}
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                date = str(record.get("date") or "")
                if date:
                    overlays[date] = list(record.get("events") or [])
        return overlays

    def _align_official_releases_to_trading_days(self, df: pd.DataFrame) -> None:
        if not self.official_releases:
            return
        trading_dates = pd.DatetimeIndex(pd.to_datetime(df[self.config.date_col])).sort_values()
        aligned: Dict[str, List[Dict[str, Any]]] = {}
        for calculated_date, records in self.official_releases.items():
            target = pd.Timestamp(calculated_date)
            position = trading_dates.searchsorted(target, side="left")
            if position >= len(trading_dates):
                continue
            trading_date = trading_dates[position].strftime("%Y-%m-%d")
            for record in records:
                item = dict(record)
                item["calculated_available_date_kr"] = calculated_date
                item["event_date"] = trading_date
                aligned.setdefault(trading_date, []).append(item)
        self.official_releases = aligned

    def _filter_trading_days(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        휴장일(주말·공휴일) 제거.

        판단 기준:
          kospi_ret_1d, kosdaq_ret_1d, usdkrw_ret_1d 세 수익률이
          모두 정확히 0.0인 날을 휴장일로 본다.

        이 방식을 쓰는 이유:
        - 종가 비교(전날과 동일)는 주말 연속 복사 구조에서 오탐이 발생한다.
          예) 금->토->일->월 흐름에서 월요일은 금요일 값 복사지만,
              토->일 복사가 중간에 끼어 있어 일요일과 비교하면 다른 값처럼 보임.
        - 실제 거래일에 코스피·코스닥·원달러 수익률이 동시에 정확히 0.0일 확률은
          사실상 없으므로 오탐 위험이 매우 낮다.
        - 세 지표 AND 조건으로 단일 지표 0.0을 추가로 방어한다.
        """
        df = df.copy()

        ret_cols = [
            col for col in ["kospi_ret_1d", "kosdaq_ret_1d", "usdkrw_ret_1d"]
            if col in df.columns
        ]

        if len(ret_cols) < 2:
            print("[경고] 수익률 컬럼 부족. 휴장일 필터 건너뜀.")
            return df

        is_holiday = pd.Series(True, index=df.index)
        for col in ret_cols:
            series = pd.to_numeric(df[col], errors="coerce")
            is_holiday = is_holiday & series.eq(0.0)

        n_removed = is_holiday.sum()
        df = df[~is_holiday].reset_index(drop=True)

        print(f"[휴장일 필터] 제거: {n_removed}일 -> 잔여: {len(df)}일")
        return df
    def _filter_period(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        if self.config.year is not None:
            df = df[df[self.config.date_col].dt.year == self.config.year]

        if self.config.start_date is not None:
            start = pd.to_datetime(self.config.start_date)
            df = df[df[self.config.date_col] >= start]

        if self.config.end_date is not None:
            end = pd.to_datetime(self.config.end_date)
            df = df[df[self.config.date_col] <= end]

        return df.reset_index(drop=True)

    # --------------------------------------------------------
    # Low-frequency freshness
    # --------------------------------------------------------

    def _get_low_frequency_cols(self) -> List[str]:
        cols = []
        cols.extend(list(self.registry.real_activity_cols))
        cols.extend(list(self.registry.trade_cols))
        cols.extend(list(self.registry.inflation_cols))
        return sorted(set(cols))

    def _add_low_frequency_freshness_flags(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in self._get_low_frequency_cols():
            if col not in df.columns:
                continue

            series = pd.to_numeric(df[col], errors="coerce")
            prev = series.shift(1)

            same_as_prev = pd.Series(
                np.isclose(
                    series.to_numpy(dtype=float),
                    prev.to_numpy(dtype=float),
                    rtol=1e-10,
                    atol=1e-10,
                    equal_nan=True,
                ),
                index=df.index,
            )

            changed = series.notna() & (~same_as_prev)
            df[f"{col}_changed_1d"] = changed

        return df

    def _has_recent_update(self, row: pd.Series, cols: List[str]) -> bool:
        for col in cols:
            flag_col = f"{col}_changed_1d"

            if flag_col not in row.index:
                continue

            value = row[flag_col]

            if pd.isna(value):
                continue

            if isinstance(value, (bool, np.bool_)) and bool(value):
                return True

            try:
                if int(value) == 1:
                    return True
            except Exception:
                pass

        return False

    # --------------------------------------------------------
    # Daily Record
    # --------------------------------------------------------

    def _build_daily_record(self, row: pd.Series) -> Dict[str, Any]:
        date_str = row[self.config.date_col].strftime("%Y-%m-%d")

        snapshot = {
            "index_context": self._extract_context(row, self.registry.index_cols),
            "rate_context": self._extract_context(row, self.registry.rate_cols),
            "fx_context": self._extract_context(row, self.registry.fx_cols),
            "commodity_context": self._extract_context(row, self.registry.commodity_cols),
            "real_activity_context": self._extract_context(row, self.registry.real_activity_cols),
            "trade_context": self._extract_context(row, self.registry.trade_cols),
            "inflation_context": self._extract_context(row, self.registry.inflation_cols),
            "gdelt_context": self._extract_context(row, self.registry.gdelt_cols),
            "global_market_context": self._extract_context(row, self.registry.global_cols),
            "risk_sentiment_context": self._extract_context(row, self.registry.risk_cols),
        }

        macro_events = self._build_macro_events(row, date_str)

        return {
            "date": date_str,
            "task": "generate_macro_news",
            "language": "ko",
            "news_count_target": self.config.news_per_day,
            "output_schema": {
                "date": "YYYY-MM-DD",
                "news_id": "string",
                "headline": "string",
                "detail_news": "string",
                "asset_class": "macro",
                "related_assets": ["KOSPI", "KOSDAQ", "KRW", "KTB", "WTI", "Gold"],
                "direction": "positive | negative | neutral | mixed",
                "source_event_ids": ["string"],
                "used_evidence": ["string"],
                "news_style": "macro_market_news",
            },
            "generation_rules": self._get_generation_rules(),
            "daily_market_snapshot": snapshot,
            "macro_events": [event.to_dict() for event in macro_events],
        }

    def _extract_context(self, row: pd.Series, cols: Tuple[str, ...]) -> Dict[str, Any]:
        context = {}

        for col in cols:
            if col not in row.index:
                continue

            value = self._clean_value(row[col])
            if value is None:
                continue

            item = {"value": value}

            z_col = f"{col}_z"
            if z_col in row.index:
                z_value = self._clean_value(row[z_col])
                if z_value is not None:
                    item["z_score"] = z_value
                    item["signal"] = self._z_to_signal(z_value)

            changed_col = f"{col}_changed_1d"
            if changed_col in row.index:
                changed_value = row[changed_col]
                if not pd.isna(changed_value):
                    item["changed_1d"] = bool(changed_value)

            context[col] = item

        return context

    # --------------------------------------------------------
    # Macro Events
    # --------------------------------------------------------

    def _build_macro_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events: List[MacroEvent] = []

        # 1. 핵심 이벤트
        events.extend(self._build_context_overlay_events(date_str))
        events.extend(self._build_official_release_events(date_str))
        events.extend(self._build_calendar_events(date_str))
        events.extend(self._build_market_breadth_events(row, date_str))
        events.extend(self._build_index_detail_events(row, date_str))
        if not self.official_releases:
            events.extend(self._build_policy_rate_events(row, date_str))
        events.extend(self._build_rate_fx_events(row, date_str))
        events.extend(self._build_commodity_events(row, date_str))
        events.extend(self._build_real_activity_events(row, date_str))
        events.extend(self._build_trade_events(row, date_str))
        events.extend(self._build_policy_inflation_events(row, date_str))
        events.extend(self._build_gdelt_sentiment_events(row, date_str))
        events.extend(self._build_global_market_events(row, date_str))

        events = self._deduplicate_events(events)
        events = self._rank_events(events)

        # 2. 10개 미만이면 보충 이벤트 생성
        if len(events) < self.config.news_per_day:
            support_events = self._build_support_events(row, date_str, existing_events=events)
            events.extend(support_events)

        events = self._deduplicate_events(events)
        events = self._rank_events(events)

        # 3. 그래도 부족하면 fallback 이벤트 생성
        if len(events) < self.config.news_per_day:
            fallback_events = self._build_fallback_events(row, date_str, existing_events=events)
            events.extend(fallback_events)

        events = self._deduplicate_events(events)
        events = self._rank_events(events)
        events = self._apply_angle_caps(events)

        return events[: self.config.news_per_day]

    def _build_context_overlay_events(self, date_str: str) -> List[MacroEvent]:
        events = []
        for record in self.context_overlays.get(date_str, []):
            events.append(MacroEvent(
                event_id=record["event_id"],
                date=date_str,
                macro_angle=record["macro_angle"],
                angle_label=record["angle_label"],
                market_implication=record["market_implication"],
                direction=record["direction"],
                severity=record["severity"],
                source_columns=list(record.get("source_columns") or []),
                evidence=dict(record.get("evidence") or {}),
                key_figures=dict(record.get("key_figures") or {}),
                event_role=record.get("event_role", "context"),
            ))
        return events

    def _build_official_release_events(self, date_str: str) -> List[MacroEvent]:
        angle_by_category = {
            "monetary_policy": "money_flow",
            "growth": "macro_regime",
            "inflation": "policy_inflation",
            "employment": "macro_regime",
            "production": "macro_regime",
            "trade": "external_pressure",
            # 국내 정책·법·정치 발표(build_policy_legal_releases.py)
            "legislation": "macro_regime",
            "court_ruling": "risk_sentiment",
            "election": "risk_sentiment",
            "regulatory_action": "macro_regime",
        }
        # 카테고리별 허용 동사. 경제지표는 '발표했다'지만 입법·사법·선거·규제는 다르다.
        # 붐비는 날 LLM이 능동/피동·다른 활용을 써도 통과하도록 행위 '어간'까지 포함(게이트 실패 근절).
        release_verbs_by_category = {
            "legislation": ["가결됐다", "의결됐다", "통과됐다", "처리됐다",
                            "가결", "의결", "통과", "처리"],
            "court_ruling": ["결정했다", "선고했다", "인용했다", "기각했다", "파면했다",
                             "결정", "선고", "인용", "기각", "각하", "파면", "위헌", "해산"],
            "election": ["실시됐다", "치러졌다", "진행됐다", "열렸다", "개최됐다",
                         "실시", "치러", "개최", "투표"],
            # 규제 조치는 표현이 다양(제재/과징금/시정명령/기업결합/동의의결)해 행위 어간까지 허용.
            # 안내·현황 등 발표성 규제 공시도 있어 '발표' 계열도 포함.
            "regulatory_action": [
                "제재했다", "부과했다", "의결했다", "조치했다", "적발했다", "심의했다",
                "결정했다", "명령했다", "고발했다", "처분했다", "착수했다", "개시했다", "승인했다",
                "발표했다", "공개했다", "안내했다",
                "제재", "과징금", "시정명령", "기업결합", "동의의결", "발표", "안내",
            ],
        }
        severity_map = {
            "critical": "strong", "high": "strong", "moderate": "moderate", "low": "weak",
        }
        events = []
        for idx, record in enumerate(self.official_releases.get(date_str, []), start=1):
            category = record["release_category"]
            key_figures = dict(record["key_figures"])
            institution = record["institution"]
            if "FOMC" in institution:
                institution_short = "FOMC"
            elif "한국은행" in institution:
                institution_short = "한국은행"
            elif "BEA" in institution:
                institution_short = "미국 BEA"
            else:
                institution_short = institution
            if category == "monetary_policy":
                action = str(key_figures.get("action") or "")
                allowed_release_verbs = ["결정했다"]
                if action:
                    allowed_release_verbs.append(f"{action}했다")
            elif category in release_verbs_by_category:
                allowed_release_verbs = list(release_verbs_by_category[category])
            else:
                allowed_release_verbs = ["발표했다"]
            direction = record["direction"]
            if direction not in {"positive", "negative", "neutral", "mixed"}:
                direction = "mixed"
            events.append(MacroEvent(
                event_id=f"{date_str}_official_release_{idx:02d}",
                date=date_str,
                macro_angle=angle_by_category.get(category, "macro_regime"),
                angle_label=record["title"],
                market_implication=record["description"],
                direction=direction,
                severity=severity_map.get(record["severity"], "moderate"),
                source_columns=["official_release_calendar"],
                evidence={
                    "institution": institution,
                    "required_attribution": institution_short,
                    "allowed_release_verbs": allowed_release_verbs,
                    "release_category": category,
                    "source_release_date": record["source_release_date"],
                    "available_date_kr": record["event_date"],
                    "calculated_available_date_kr": record.get(
                        "calculated_available_date_kr", record["event_date"]
                    ),
                    "reference_period": record.get("reference_period", ""),
                    "official_description": record["description"],
                    "source_url": record["source_url"],
                    "verification_status": record["verification_status"],
                },
                key_figures=key_figures,
                event_role="headline",
            ))
        return events

    def _build_policy_rate_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        specs = [
            ("kr", "한국 기준금리", "kr_policy_rate", "kr_policy_rate_chg_1d_bp", "money_flow"),
            ("us", "미국 정책금리", "us_policy_rate", "us_policy_rate_chg_1d_bp", "external_pressure"),
        ]
        events = []
        for region, label, level_col, change_col, angle in specs:
            level = self._get(row, level_col)
            change_bp = self._get(row, change_col)
            if level is None or change_bp is None or abs(change_bp) < 1:
                continue
            direction = "negative" if change_bp > 0 else "positive"
            verb = "인상" if change_bp > 0 else "인하"
            events.append(MacroEvent(
                event_id=f"{date_str}_policy_rate_{region}",
                date=date_str,
                macro_angle=angle,
                angle_label=f"{label} {verb}",
                market_implication=f"{label}가 {abs(change_bp):g}bp {verb}돼 {level:g}%를 기록",
                direction=direction,
                severity="strong" if abs(change_bp) >= 25 else "moderate",
                source_columns=[level_col, change_col],
                evidence={level_col: level, change_col: change_bp},
                key_figures={f"{level_col}_pct": f"{level:g}%", f"{change_col}": f"{change_bp:+g}bp"},
                event_role="core",
            ))
        return events

    def _build_calendar_events(self, date_str: str) -> List[MacroEvent]:
        angle_by_type = {
            "policy": "money_flow",
            "geopolitics": "external_pressure",
            "commodity": "policy_inflation",
            "em": "external_pressure",
            "financial": "risk_sentiment",
            "pandemic": "macro_regime",
        }
        severity_map = {
            "critical": "strong",
            "high": "strong",
            "moderate": "moderate",
            "low": "weak",
        }
        events = []
        for idx, record in enumerate(self.calendar_events.get(date_str, []), start=1):
            event_type = record.get("event_type", "")
            direction = record.get("direction", "mixed")
            if direction not in {"positive", "negative", "neutral", "mixed"}:
                direction = "mixed"
            title = record.get("title", "주요 거시 사건")
            events.append(MacroEvent(
                event_id=f"{date_str}_calendar_{idx:02d}",
                date=date_str,
                macro_angle=angle_by_type.get(event_type, "macro_regime"),
                angle_label=title,
                market_implication=record.get("description", ""),
                direction=direction,
                severity=severity_map.get(record.get("severity", ""), "moderate"),
                source_columns=["macro_event_calendar"],
                evidence={
                    "event_type": event_type,
                    "region": record.get("region", ""),
                    "description": record.get("description", ""),
                    "affected_markets": record.get("affected_markets", ""),
                },
                key_figures={"event_title": title},
                event_role="core",
            ))
        return events

    # --------------------------------------------------------
    # Core Event Builders
    # --------------------------------------------------------

    def _build_market_breadth_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        kospi_ret = self._get(row, "kospi_ret_1d")
        kosdaq_ret = self._get(row, "kosdaq_ret_1d")
        kospi_z = self._get(row, "kospi_ret_1d_z")
        kosdaq_z = self._get(row, "kosdaq_ret_1d_z")

        if self._is_meaningful(kospi_z) or self._is_meaningful(kosdaq_z):
            avg_ret = self._safe_mean([kospi_ret, kosdaq_ret])
            avg_z = self._safe_mean([kospi_z, kosdaq_z])

            direction = self._direction_from_value(avg_ret, positive_when_up=True)
            severity = self._severity_from_z(avg_z)

            if direction == "positive":
                angle_label = "코스피·코스닥 동반 강세"
                market_implication = "주요 지수가 함께 오르며 국내 위험자산 선호가 살아난 장세"
            elif direction == "negative":
                angle_label = "코스피·코스닥 동반 약세"
                market_implication = "주요 지수가 함께 밀리며 위험자산 회피 분위기가 확산된 장세"
            else:
                angle_label = "코스피·코스닥 방향성 부재"
                market_implication = "지수 움직임이 엇갈리거나 정체되며 관망 심리가 지배한 장세"

            key_figures = {
                "kospi_ret_1d_pct": f"{kospi_ret:+.2f}%" if kospi_ret is not None else None,
                "kosdaq_ret_1d_pct": f"{kosdaq_ret:+.2f}%" if kosdaq_ret is not None else None,
                "kospi_ret_1d_z": round(kospi_z, 2) if kospi_z is not None else None,
                "kosdaq_ret_1d_z": round(kosdaq_z, 2) if kosdaq_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_market_breadth",
                date=date_str,
                macro_angle="market_breadth",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["kospi_ret_1d", "kosdaq_ret_1d"],
                evidence={
                    "kospi_ret_1d": kospi_ret,
                    "kosdaq_ret_1d": kosdaq_ret,
                    "kospi_ret_1d_z": kospi_z,
                    "kosdaq_ret_1d_z": kosdaq_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_index_detail_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        specs = [
            {
                "name": "kospi",
                "ret_col": "kospi_ret_1d",
                "close_col": "kospi_close",
                "z_col": "kospi_ret_1d_z",
                "label_pos": "코스피 강세",
                "label_neg": "코스피 약세",
                "implication_pos": "대형주 중심 매수세가 유입되며 국내 증시 투자심리 개선",
                "implication_neg": "대형주 중심 매도 압력이 커지며 증시 전반 경계감 확산",
                "assets": ["KOSPI"],
            },
            {
                "name": "kosdaq",
                "ret_col": "kosdaq_ret_1d",
                "close_col": "kosdaq_close",
                "z_col": "kosdaq_ret_1d_z",
                "label_pos": "코스닥 강세",
                "label_neg": "코스닥 약세",
                "implication_pos": "성장주·중소형주로 매수세가 확산되며 위험선호 회복",
                "implication_neg": "성장주·중소형주 중심 매물 출회로 위험선호 위축",
                "assets": ["KOSDAQ"],
            },
        ]

        for spec in specs:
            ret = self._get(row, spec["ret_col"])
            close = self._get(row, spec["close_col"])
            z = self._get(row, spec["z_col"])

            if not self._is_meaningful(z):
                continue

            direction = self._direction_from_value(ret, positive_when_up=True)
            severity = self._severity_from_z(z)

            angle_label = spec["label_pos"] if direction == "positive" else spec["label_neg"]
            market_implication = spec["implication_pos"] if direction == "positive" else spec["implication_neg"]

            key_figures = {
                f"{spec['name']}_ret_1d_pct": f"{ret:+.2f}%" if ret is not None else None,
                f"{spec['name']}_close": close,
                f"{spec['name']}_ret_1d_z": round(z, 2) if z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_{spec['name']}_detail",
                date=date_str,
                macro_angle="market_breadth",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=[spec["ret_col"]],
                evidence={
                    spec["ret_col"]: ret,
                    spec["z_col"]: z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_rate_fx_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        usdkrw = self._get(row, "usdkrw")
        usdkrw_ret = self._get(row, "usdkrw_ret_1d")
        usdkrw_z = self._get(row, "usdkrw_ret_1d_z")

        y3 = self._get(row, "kr_3y_yield")
        y10 = self._get(row, "kr_10y_yield")
        spread = self._get(row, "term_spread_10y_3y")
        spread_z = self._get(row, "term_spread_10y_3y_z")

        if self._is_meaningful(usdkrw_z):
            direction = "negative" if usdkrw_ret is not None and usdkrw_ret > 0 else "positive"
            severity = self._severity_from_z(usdkrw_z)

            if direction == "negative":
                angle_label = "원화 약세"
                market_implication = "달러 강세 속 원화 하락으로 수입물가와 외국인 자금 이탈 부담 가중"
            else:
                angle_label = "원화 강세"
                market_implication = "원화 가치 회복으로 수입물가 부담과 외국인 이탈 우려 완화"

            key_figures = {
                "usdkrw": usdkrw,
                "usdkrw_ret_1d_pct": f"{usdkrw_ret:+.2f}%" if usdkrw_ret is not None else None,
                "usdkrw_ret_1d_z": round(usdkrw_z, 2) if usdkrw_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_fx_usdkrw",
                date=date_str,
                macro_angle="money_flow",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["usdkrw", "usdkrw_ret_1d"],
                evidence={
                    "usdkrw": usdkrw,
                    "usdkrw_ret_1d": usdkrw_ret,
                    "usdkrw_ret_1d_z": usdkrw_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        if self._is_meaningful(spread_z):
            direction = self._direction_from_value(spread, positive_when_up=True)
            severity = self._severity_from_z(spread_z)

            if spread is not None and spread < 0:
                angle_label = "장단기 금리차 역전"
                market_implication = "단기금리가 장기금리를 웃돌며 경기 둔화 우려가 채권시장에 반영"
                direction = "negative"
            else:
                angle_label = "장단기 금리차 확대"
                market_implication = "장기금리 상승폭이 단기를 앞서며 경기 및 금리 경로 재평가"

            key_figures = {
                "kr_3y_yield": y3,
                "kr_10y_yield": y10,
                "term_spread_10y_3y_bp": round(spread * 100, 1) if spread is not None else None,
                "term_spread_10y_3y_z": round(spread_z, 2) if spread_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_rate_spread",
                date=date_str,
                macro_angle="money_flow",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["kr_3y_yield", "kr_10y_yield", "term_spread_10y_3y"],
                evidence={
                    "kr_3y_yield": y3,
                    "kr_10y_yield": y10,
                    "term_spread_10y_3y": spread,
                    "term_spread_10y_3y_z": spread_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_commodity_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        wti = self._get(row, "wti")
        wti_ret = self._get(row, "wti_ret_1d")
        wti_z = self._get(row, "wti_ret_1d_z")

        dubai_oil = self._get(row, "dubai_oil")
        dubai_ret = self._get(row, "dubai_oil_ret_1d")
        dubai_z = self._get(row, "dubai_oil_ret_1d_z")

        gold = self._get(row, "gold")
        gold_ret = self._get(row, "gold_ret_1d")
        gold_z = self._get(row, "gold_ret_1d_z")

        if self._is_meaningful(wti_z):
            direction = "negative" if wti_ret is not None and wti_ret > 0 else "positive"
            severity = self._severity_from_z(wti_z)

            if wti_ret is not None and wti_ret > 0:
                angle_label = "국제유가(WTI) 상승"
                market_implication = "에너지 가격 반등으로 물가 재상승 압력과 기업 비용 부담 재부각"
            else:
                angle_label = "국제유가(WTI) 하락"
                market_implication = "유가 하락으로 에너지·물가 부담 완화, 소비 여력 개선 기대"

            key_figures = {
                "wti_usd": wti,
                "wti_ret_1d_pct": f"{wti_ret:+.2f}%" if wti_ret is not None else None,
                "wti_ret_1d_z": round(wti_z, 2) if wti_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_commodity_oil_wti",
                date=date_str,
                macro_angle="policy_inflation",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["wti", "wti_ret_1d"],
                evidence={
                    "wti": wti,
                    "wti_ret_1d": wti_ret,
                    "wti_ret_1d_z": wti_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        if self._is_meaningful(dubai_z):
            direction = "negative" if dubai_ret is not None and dubai_ret > 0 else "positive"
            severity = self._severity_from_z(dubai_z)

            if dubai_ret is not None and dubai_ret > 0:
                angle_label = "두바이유 상승"
                market_implication = "국내 수입 에너지 단가 상승으로 물가 압력 확대"
            else:
                angle_label = "두바이유 하락"
                market_implication = "수입 에너지 비용 감소로 국내 에너지 물가 안정 기대"

            key_figures = {
                "dubai_oil_usd": dubai_oil,
                "dubai_oil_ret_1d_pct": f"{dubai_ret:+.2f}%" if dubai_ret is not None else None,
                "dubai_oil_ret_1d_z": round(dubai_z, 2) if dubai_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_commodity_oil_dubai",
                date=date_str,
                macro_angle="policy_inflation",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["dubai_oil", "dubai_oil_ret_1d"],
                evidence={
                    "dubai_oil": dubai_oil,
                    "dubai_oil_ret_1d": dubai_ret,
                    "dubai_oil_ret_1d_z": dubai_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        if self._is_meaningful(gold_z):
            direction = "negative" if gold_ret is not None and gold_ret > 0 else "neutral"
            severity = self._severity_from_z(gold_z)

            if gold_ret is not None and gold_ret > 0:
                angle_label = "금 가격 상승"
                market_implication = "안전자산 수요 급증, 글로벌 불확실성 확대 신호"
            else:
                angle_label = "금 가격 하락"
                market_implication = "위험회피 심리 완화, 위험자산 선호 복귀 흐름"

            key_figures = {
                "gold_usd": gold,
                "gold_ret_1d_pct": f"{gold_ret:+.2f}%" if gold_ret is not None else None,
                "gold_ret_1d_z": round(gold_z, 2) if gold_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_commodity_gold",
                date=date_str,
                macro_angle="risk_sentiment",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["gold", "gold_ret_1d"],
                evidence={
                    "gold": gold,
                    "gold_ret_1d": gold_ret,
                    "gold_ret_1d_z": gold_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_real_activity_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        cols = [
            "industrial_production",
            "mining_manufacturing_production",
            "retail_sales",
            "facility_investment",
            "leading_index",
        ]

        if not self._has_recent_update(row, cols):
            return []

        z_values = [self._get(row, f"{col}_z") for col in cols]
        avg_z = self._safe_mean(z_values)

        if self._is_meaningful(avg_z):
            direction = self._direction_from_value(avg_z, positive_when_up=True)
            severity = self._severity_from_z(avg_z)

            if direction == "positive":
                angle_label = "실물경기 지표 개선"
                market_implication = "생산·소비·투자 지표가 함께 오르며 경기 회복 기대 강화"
            elif direction == "negative":
                angle_label = "실물경기 지표 둔화"
                market_implication = "생산·소비·투자 복수 지표 동반 약화로 경기 하강 우려 부각"
            else:
                angle_label = "실물경기 지표 혼조"
                market_implication = "주요 실물 지표가 방향성 없이 엇갈리며 경기 판단 불확실"

            key_figures = {
                col: self._get(row, col)
                for col in cols
                if col in row.index and self._get(row, col) is not None
            }
            key_figures["composite_z"] = round(avg_z, 2) if avg_z is not None else None

            events.append(MacroEvent(
                event_id=f"{date_str}_real_activity",
                date=date_str,
                macro_angle="macro_regime",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=[col for col in cols if col in row.index],
                evidence={col: self._get(row, col) for col in cols if col in row.index},
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_trade_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        trade_cols = ["export_amount", "import_amount", "trade_balance"]

        if not self._has_recent_update(row, trade_cols):
            return []

        export = self._get(row, "export_amount")
        export_z = self._get(row, "export_amount_z")
        import_z = self._get(row, "import_amount_z")
        balance = self._get(row, "trade_balance")
        balance_z = self._get(row, "trade_balance_z")

        key_z = self._safe_mean([export_z, balance_z])

        if self._is_meaningful(key_z):
            direction = self._direction_from_value(key_z, positive_when_up=True)
            severity = self._severity_from_z(key_z)

            if direction == "positive":
                angle_label = "수출 및 무역수지 개선"
                market_implication = "수출 증가와 흑자 확대로 대외 수요 개선 기대 강화"
            elif direction == "negative":
                angle_label = "수출 및 무역수지 악화"
                market_implication = "수출 부진과 적자 확대로 대외 교역 여건 악화 우려"
            else:
                angle_label = "수출입 지표 혼조"
                market_implication = "수출·무역수지 지표가 엇갈리며 대외 수요 판단에 불확실성 지속"

            key_figures = {
                "export_amount": export,
                "trade_balance": balance,
                "export_amount_z": round(export_z, 2) if export_z is not None else None,
                "trade_balance_z": round(balance_z, 2) if balance_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_trade_external",
                date=date_str,
                macro_angle="external_pressure",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["export_amount", "import_amount", "trade_balance"],
                evidence={
                    "export_amount_z": export_z,
                    "import_amount_z": import_z,
                    "trade_balance": balance,
                    "trade_balance_z": balance_z,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_policy_inflation_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        if not self._has_recent_update(row, ["cpi"]):
            return []

        cpi = self._get(row, "cpi")
        cpi_z = self._get(row, "cpi_z")
        policy_tone = self._get(row, "gdelt_policy_tone")
        inflation_tone = self._get(row, "gdelt_inflation_tone")

        if self._is_meaningful(cpi_z):
            direction = "negative" if cpi_z is not None and cpi_z > 0 else "positive"
            severity = self._severity_from_z(cpi_z)

            if direction == "negative":
                angle_label = "소비자물가 상승 압력 확대"
                market_implication = "CPI 상승으로 금리 인상 기대와 실질 구매력 약화 우려 동반 부각"
            else:
                angle_label = "소비자물가 안정세 확인"
                market_implication = "CPI 둔화로 긴축 부담 완화, 금리 동결·인하 기대 재부각"

            key_figures = {
                "cpi": cpi,
                "cpi_z": round(cpi_z, 2) if cpi_z is not None else None,
                "gdelt_inflation_tone": inflation_tone,
                "gdelt_policy_tone": policy_tone,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_inflation_policy",
                date=date_str,
                macro_angle="policy_inflation",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["cpi", "gdelt_policy_tone", "gdelt_inflation_tone"],
                evidence={
                    "cpi": cpi,
                    "cpi_z": cpi_z,
                    "gdelt_policy_tone": policy_tone,
                    "gdelt_inflation_tone": inflation_tone,
                },
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    def _build_gdelt_sentiment_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        gdelt_specs = [
            {
                "col": "gdelt_macro_tone",
                "vol_col": "gdelt_macro_volume",
                "event_id": "gdelt_macro_tone",
                "angle": "risk_sentiment",
                "label_pos": "거시경제 뉴스 심리 개선",
                "label_neg": "거시경제 뉴스 심리 악화",
                "implication_pos": "거시경제 관련 뉴스 전반의 톤이 개선되며 시장 불안 심리 완화",
                "implication_neg": "거시경제 뉴스 톤이 동반 악화되며 시장 경계감과 불확실성 확대",
            },
            {
                "col": "gdelt_trade_tone",
                "vol_col": None,
                "event_id": "gdelt_trade_tone",
                "angle": "external_pressure",
                "label_pos": "교역 뉴스 심리 개선",
                "label_neg": "교역 뉴스 심리 악화",
                "implication_pos": "교역 관련 보도 톤 개선, 대외 수요 둔화 경계감 일부 완화",
                "implication_neg": "교역 관련 뉴스 톤 악화, 수출입 여건 불안 및 대외 경기 우려 확대",
            },
            {
                "col": "gdelt_inflation_tone",
                "vol_col": None,
                "event_id": "gdelt_inflation_tone",
                "angle": "policy_inflation",
                "label_pos": "물가 뉴스 심리 개선",
                "label_neg": "물가 뉴스 심리 악화",
                "implication_pos": "물가 관련 뉴스 톤 개선, 인플레이션 우려 완화 분위기",
                "implication_neg": "물가 뉴스 톤 악화, 인플레이션·금리 부담 재확산",
            },
            {
                "col": "gdelt_policy_tone",
                "vol_col": None,
                "event_id": "gdelt_policy_tone",
                "angle": "money_flow",
                "label_pos": "정책 뉴스 심리 개선",
                "label_neg": "정책 뉴스 심리 악화",
                "implication_pos": "정책 관련 뉴스 톤 개선, 금리·통화정책 불확실성 완화 기대",
                "implication_neg": "정책 뉴스 톤 악화, 긴축·정책 불확실성 부담 재부각",
            },
            {
                "col": "gdelt_employment_tone",
                "vol_col": None,
                "event_id": "gdelt_employment_tone",
                "angle": "macro_regime",
                "label_pos": "고용 뉴스 심리 개선",
                "label_neg": "고용 뉴스 심리 악화",
                "implication_pos": "고용 관련 뉴스 톤 개선, 가계 소득·내수 경기 기대 회복",
                "implication_neg": "고용 뉴스 톤 악화, 가계 소득 감소와 내수 위축 우려 부각",
            },
        ]

        for spec in gdelt_specs:
            col = spec["col"]
            z_col = f"{col}_z"

            tone = self._get(row, col)
            z = self._get(row, z_col)

            if not self._is_meaningful(z):
                continue

            direction = self._direction_from_value(tone, positive_when_up=True)
            severity = self._severity_from_z(z)

            if direction in ("positive",):
                angle_label = spec["label_pos"]
                market_implication = spec["implication_pos"]
            else:
                angle_label = spec["label_neg"]
                market_implication = spec["implication_neg"]

            key_figures: Dict[str, Any] = {
                col: tone,
                z_col: round(z, 2) if z is not None else None,
            }

            evidence: Dict[str, Any] = {col: tone, z_col: z}
            source_columns = [col]

            if spec["vol_col"] is not None and spec["vol_col"] in row.index:
                vol = self._get(row, spec["vol_col"])
                source_columns.append(spec["vol_col"])
                evidence[spec["vol_col"]] = vol
                evidence[f"{spec['vol_col']}_z"] = self._get(row, f"{spec['vol_col']}_z")
                key_figures[spec["vol_col"]] = vol

            events.append(MacroEvent(
                event_id=f"{date_str}_{spec['event_id']}",
                date=date_str,
                macro_angle=spec["angle"],
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=source_columns,
                evidence=evidence,
                key_figures=key_figures,
                event_role="core",
            ))

        return events

    # --------------------------------------------------------
    # Global Market Events
    # --------------------------------------------------------

    def _build_global_market_events(self, row: pd.Series, date_str: str) -> List[MacroEvent]:
        events = []

        sp500 = self._get(row, "sp500_close")
        sp500_ret = self._get(row, "sp500_ret_1d")
        sp500_z = self._get(row, "sp500_ret_1d_z")

        nasdaq = self._get(row, "nasdaq_close")
        nasdaq_ret = self._get(row, "nasdaq_ret_1d")
        nasdaq_z = self._get(row, "nasdaq_ret_1d_z")

        us_10y = self._get(row, "us_10y_yield")
        us_2y = self._get(row, "us_2y_yield")
        us_spread = self._get(row, "us_term_spread_10y_2y")
        us_spread_z = self._get(row, "us_term_spread_10y_2y_z")

        us_policy = self._get(row, "us_policy_rate")

        # S&P500 / 나스닥 동반 흐름
        avg_us_z = self._safe_mean([sp500_z, nasdaq_z])
        if self._is_meaningful(avg_us_z):
            direction = self._direction_from_value(avg_us_z, positive_when_up=True)
            severity = self._severity_from_z(avg_us_z)

            if direction == "positive":
                angle_label = "미국 증시 강세"
                market_implication = "S&P500·나스닥 동반 상승으로 글로벌 위험선호 회복, 국내 증시 외국인 수급에 긍정적"
            else:
                angle_label = "미국 증시 약세"
                market_implication = "S&P500·나스닥 동반 하락으로 글로벌 위험회피 확산, 국내 증시 외국인 이탈 압력 증가"

            key_figures = {
                "sp500_ret_1d_pct": f"{sp500_ret:+.2f}%" if sp500_ret is not None else None,
                "nasdaq_ret_1d_pct": f"{nasdaq_ret:+.2f}%" if nasdaq_ret is not None else None,
                "sp500_close": sp500,
                "sp500_ret_1d_z": round(sp500_z, 2) if sp500_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_global_us_equity",
                date=date_str,
                macro_angle="risk_sentiment",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["sp500_close", "sp500_ret_1d", "nasdaq_close", "nasdaq_ret_1d"],
                evidence={
                    "sp500_close": sp500,
                    "sp500_ret_1d": sp500_ret,
                    "sp500_ret_1d_z": sp500_z,
                    "nasdaq_close": nasdaq,
                    "nasdaq_ret_1d": nasdaq_ret,
                    "nasdaq_ret_1d_z": nasdaq_z,
                },
                key_figures={k: v for k, v in key_figures.items() if v is not None},
                event_role="core",
            ))

        # 미국 장단기 금리차 (경기 선행 신호)
        if self._is_meaningful(us_spread_z):
            direction = self._direction_from_value(us_spread, positive_when_up=True)
            severity = self._severity_from_z(us_spread_z)

            if us_spread is not None and us_spread < 0:
                angle_label = "미국 장단기 금리 역전"
                market_implication = "미국 국채 장단기 금리가 역전되며 글로벌 경기 침체 우려 확산"
                direction = "negative"
            elif direction == "positive":
                angle_label = "미국 장단기 금리차 확대"
                market_implication = "미국 장기금리 상승으로 달러 강세·신흥국 자금 이탈 압력 재부각"
            else:
                angle_label = "미국 장단기 금리차 축소"
                market_implication = "미국 장기금리 하락으로 달러 약세 기대, 신흥국 자금 유입 환경 개선"

            key_figures = {
                "us_10y_yield": us_10y,
                "us_2y_yield": us_2y,
                "us_term_spread_10y_2y_bp": round(us_spread * 100, 1) if us_spread is not None else None,
                "us_term_spread_z": round(us_spread_z, 2) if us_spread_z is not None else None,
            }

            events.append(MacroEvent(
                event_id=f"{date_str}_global_us_rates",
                date=date_str,
                macro_angle="external_pressure",
                angle_label=angle_label,
                market_implication=market_implication,
                direction=direction,
                severity=severity,
                source_columns=["us_10y_yield", "us_2y_yield", "us_term_spread_10y_2y"],
                evidence={
                    "us_10y_yield": us_10y,
                    "us_2y_yield": us_2y,
                    "us_term_spread_10y_2y": us_spread,
                    "us_term_spread_10y_2y_z": us_spread_z,
                    "us_policy_rate": us_policy,
                },
                key_figures={k: v for k, v in key_figures.items() if v is not None},
                event_role="core",
            ))

        return events

    # --------------------------------------------------------
    # Support Events
    # --------------------------------------------------------

    def _build_support_events(
        self,
        row: pd.Series,
        date_str: str,
        existing_events: List[MacroEvent],
    ) -> List[MacroEvent]:
        events = []
        existing_ids = {e.event_id for e in existing_events}

        specs = [
            {
                "event_id": f"{date_str}_support_kospi_context",
                "angle": "market_breadth",
                "label": "코스피 흐름 점검",
                "implication": "지수 수준과 수익률을 바탕으로 대형주 중심 투자심리와 수급 변화를 확인",
                "direction_col": "kospi_ret_1d",
                "source_columns": ["kospi_close", "kospi_ret_1d", "kospi_ret_1d_z"],
            },
            {
                "event_id": f"{date_str}_support_kosdaq_context",
                "angle": "market_breadth",
                "label": "코스닥 흐름 점검",
                "implication": "코스닥 수익률과 수준을 바탕으로 중소형·성장주 위험선호 변화를 확인",
                "direction_col": "kosdaq_ret_1d",
                "source_columns": ["kosdaq_close", "kosdaq_ret_1d", "kosdaq_ret_1d_z"],
            },
            {
                "event_id": f"{date_str}_support_fx_context",
                "angle": "money_flow",
                "label": "원달러 환율 점검",
                "implication": "환율 수준과 변동폭을 바탕으로 원화자산 부담과 외국인 수급 압력을 확인",
                "direction_col": "usdkrw_ret_1d",
                "reverse_direction": True,
                "source_columns": ["usdkrw", "usdkrw_ret_1d", "usdkrw_ret_1d_z"],
            },
            {
                "event_id": f"{date_str}_support_rate_context",
                "angle": "money_flow",
                "label": "국고채 금리 및 장단기 스프레드 점검",
                "implication": "국고채 3년·10년 금리와 스프레드 수준으로 채권시장 자금 흐름과 경기 기대를 확인",
                "direction_col": "term_spread_10y_3y",
                "source_columns": ["kr_3y_yield", "kr_10y_yield", "term_spread_10y_3y"],
            },
            {
                "event_id": f"{date_str}_support_oil_context",
                "angle": "policy_inflation",
                "label": "WTI 유가 흐름 점검",
                "implication": "WTI 수준과 일간 등락으로 에너지 비용과 물가 경로에 미치는 영향을 확인",
                "direction_col": "wti_ret_1d",
                "reverse_direction": True,
                "source_columns": ["wti", "wti_ret_1d", "wti_ret_1d_z"],
            },
            {
                "event_id": f"{date_str}_support_dubai_oil_context",
                "angle": "policy_inflation",
                "label": "두바이유 흐름 점검",
                "implication": "국내 수입 기준 두바이유 등락으로 에너지 수입 단가와 물가 부담 변화를 확인",
                "direction_col": "dubai_oil_ret_1d",
                "reverse_direction": True,
                "source_columns": ["dubai_oil", "dubai_oil_ret_1d", "dubai_oil_ret_1d_z"],
            },
            {
                "event_id": f"{date_str}_support_gold_context",
                "angle": "risk_sentiment",
                "label": "금 가격 흐름 점검",
                "implication": "금 가격 등락으로 글로벌 위험회피 강도와 안전자산 선호 수준을 확인",
                "direction_col": "gold_ret_1d",
                "reverse_direction": True,
                "source_columns": ["gold", "gold_ret_1d", "gold_ret_1d_z"],
            },
            {
                "event_id": f"{date_str}_support_credit_spread_context",
                "angle": "money_flow",
                "label": "회사채 신용스프레드 점검",
                "implication": "회사채 AA-·BBB- 신용스프레드 수준으로 국내 신용 위험과 자금 조달 여건을 확인",
                "direction_col": "corp_bbb_minus_spread",
                "reverse_direction": True,
                "source_columns": ["corp_aa_minus_spread", "corp_bbb_minus_spread"],
            },
            {
                "event_id": f"{date_str}_support_us_rate_context",
                "angle": "external_pressure",
                "label": "미국 국채 금리와 장단기 스프레드 점검",
                "implication": "미국 국채 2년·10년 금리와 장단기 스프레드로 글로벌 금리 부담을 확인",
                "direction_col": "us_10y_yield",
                "reverse_direction": True,
                "source_columns": ["us_2y_yield", "us_10y_yield", "us_term_spread_10y_2y"],
            },
            {
                "event_id": f"{date_str}_support_us_equity_context",
                "angle": "risk_sentiment",
                "label": "미국 증시 흐름 점검",
                "implication": "S&P500과 나스닥 일간 수익률로 글로벌 위험자산 선호 강도를 확인",
                "direction_col": "sp500_ret_1d",
                "source_columns": ["sp500_close", "sp500_ret_1d", "nasdaq_close", "nasdaq_ret_1d"],
            },
            {
                "event_id": f"{date_str}_support_gdelt_macro_context",
                "angle": "risk_sentiment",
                "label": "거시경제 뉴스 톤·보도량 점검",
                "implication": "GDELT 거시 뉴스 톤과 보도량으로 글로벌 시장 심리와 불안 수위를 확인",
                "direction_col": "gdelt_macro_tone",
                "source_columns": ["gdelt_macro_tone", "gdelt_macro_volume"],
            },
            {
                "event_id": f"{date_str}_support_gdelt_trade_context",
                "angle": "external_pressure",
                "label": "교역 뉴스 톤 점검",
                "implication": "교역 관련 뉴스 톤으로 수출입 여건에 대한 시장 시각 변화를 확인",
                "direction_col": "gdelt_trade_tone",
                "source_columns": ["gdelt_trade_tone"],
            },
            {
                "event_id": f"{date_str}_support_gdelt_policy_context",
                "angle": "money_flow",
                "label": "정책 뉴스 톤 점검",
                "implication": "통화·재정정책 관련 뉴스 톤으로 금리 부담과 유동성 기대를 확인",
                "direction_col": "gdelt_policy_tone",
                "source_columns": ["gdelt_policy_tone"],
            },
            {
                "event_id": f"{date_str}_support_gdelt_inflation_context",
                "angle": "policy_inflation",
                "label": "물가 뉴스 톤 점검",
                "implication": "물가 관련 뉴스 톤으로 인플레이션 부담과 정책 대응 기대를 확인",
                "direction_col": "gdelt_inflation_tone",
                "source_columns": ["gdelt_inflation_tone"],
            },
            {
                "event_id": f"{date_str}_support_gdelt_employment_context",
                "angle": "macro_regime",
                "label": "고용 뉴스 톤 점검",
                "implication": "고용 관련 뉴스 톤으로 가계 소득·소비 여력과 내수 경기를 확인",
                "direction_col": "gdelt_employment_tone",
                "source_columns": ["gdelt_employment_tone"],
            },
        ]

        for spec in specs:
            if spec["event_id"] in existing_ids:
                continue

            valid_source_cols = [
                col for col in spec["source_columns"]
                if col in row.index and self._get(row, col) is not None
            ]

            if not valid_source_cols:
                continue

            signal_sources = {
                col for col in valid_source_cols
                if "ret_1d" in col or "spread" in col or col.endswith("_chg_1d_bp")
            }
            if signal_sources and any(
                signal_sources & set(event.source_columns) for event in existing_events
            ):
                continue

            direction_value = self._get(row, spec["direction_col"])
            reverse = bool(spec.get("reverse_direction", False))
            direction = self._direction_from_value(direction_value, positive_when_up=not reverse)

            z_candidates = []
            for col in spec["source_columns"]:
                if col.endswith("_z"):
                    z_candidates.append(self._get(row, col))
                else:
                    z_candidates.append(self._get(row, f"{col}_z"))

            avg_z = self._safe_mean(z_candidates)
            severity = self._severity_from_z(avg_z)

            if avg_z is not None and abs(avg_z) < self.config.weak_event_abs_z:
                severity = "normal"

            evidence = {col: self._get(row, col) for col in valid_source_cols}
            key_figures: Dict[str, Any] = {}

            for col in valid_source_cols:
                z_col = f"{col}_z"
                if z_col in row.index:
                    evidence[z_col] = self._get(row, z_col)

                val = self._get(row, col)
                if val is not None:
                    if "spread" in col:
                        key_figures[f"{col}_pct_point"] = f"{val:g}%p"
                    elif col.endswith("_z"):
                        key_figures[col] = round(val, 2)
                    elif "ret_1d" in col:
                        key_figures[f"{col}_pct"] = f"{val:+.2f}%"
                    else:
                        key_figures[col] = val

            events.append(MacroEvent(
                event_id=spec["event_id"],
                date=date_str,
                macro_angle=spec["angle"],
                angle_label=spec["label"],
                market_implication=spec["implication"],
                direction=direction,
                severity=severity,
                source_columns=valid_source_cols,
                evidence=evidence,
                key_figures=key_figures,
                event_role="support",
            ))

        return events

    def _build_fallback_events(
        self,
        row: pd.Series,
        date_str: str,
        existing_events: List[MacroEvent],
    ) -> List[MacroEvent]:
        events = []
        existing_ids = {e.event_id for e in existing_events}

        fallback_specs = [
            {
                "suffix": "market_overview",
                "angle": "market_breadth",
                "label": "증시 전반 흐름 종합",
                "implication": "주요 지수와 시장 분위기를 종합해 국내 증시 방향성과 위험선호 수준을 확인",
                "source_columns": ["kospi_ret_1d", "kosdaq_ret_1d"],
            },
            {
                "suffix": "risk_preference",
                "angle": "risk_sentiment",
                "label": "위험자산 선호 종합 점검",
                "implication": "주식·환율·원자재 흐름을 교차해 위험자산과 안전자산 간 자금 이동을 확인",
                "source_columns": ["kospi_ret_1d", "kosdaq_ret_1d", "gold_ret_1d"],
            },
            {
                "suffix": "money_flow",
                "angle": "money_flow",
                "label": "환율·금리 연계 자금 흐름 점검",
                "implication": "원달러 환율과 국고채 금리 흐름을 교차해 원화채권 자금 이동 방향을 확인",
                "source_columns": ["usdkrw_ret_1d", "kr_3y_yield", "kr_10y_yield"],
            },
            {
                "suffix": "inflation_pressure",
                "angle": "policy_inflation",
                "label": "물가·정책 부담 종합 점검",
                "implication": "유가·CPI·정책 뉴스를 교차해 인플레이션과 금리 경로 부담을 종합 확인",
                "source_columns": ["wti_ret_1d", "dubai_oil_ret_1d", "cpi", "gdelt_inflation_tone"],
            },
            {
                "suffix": "external_condition",
                "angle": "external_pressure",
                "label": "대외 여건 종합 점검",
                "implication": "환율·수출·교역 뉴스를 교차해 외부 충격과 원화자산 부담을 종합 확인",
                "source_columns": ["usdkrw_ret_1d", "export_amount", "trade_balance", "gdelt_trade_tone"],
            },
            {
                "suffix": "macro_regime",
                "angle": "macro_regime",
                "label": "경기 국면 신호 종합 점검",
                "implication": "생산·소비·투자·고용 신호를 교차해 경기 회복과 침체 신호 균형을 확인",
                "source_columns": ["industrial_production", "retail_sales", "facility_investment", "gdelt_employment_tone"],
            },
        ]

        for spec in fallback_specs:
            event_id = f"{date_str}_fallback_{spec['suffix']}"

            if event_id in existing_ids:
                continue

            valid_source_cols = [
                col for col in spec["source_columns"]
                if col in row.index and self._get(row, col) is not None
            ]

            if not valid_source_cols:
                continue

            evidence: Dict[str, Any] = {}
            key_figures: Dict[str, Any] = {}
            z_values = []

            for col in valid_source_cols:
                val = self._get(row, col)
                evidence[col] = val

                z_col = f"{col}_z"
                if z_col in row.index:
                    z_val = self._get(row, z_col)
                    evidence[z_col] = z_val
                    z_values.append(z_val)

                if val is not None:
                    if "ret_1d" in col:
                        key_figures[f"{col}_pct"] = f"{val:+.2f}%"
                    else:
                        key_figures[col] = val

            avg_z = self._safe_mean(z_values)
            direction = self._direction_from_value(avg_z, positive_when_up=True)

            events.append(MacroEvent(
                event_id=event_id,
                date=date_str,
                macro_angle=spec["angle"],
                angle_label=spec["label"],
                market_implication=spec["implication"],
                direction=direction,
                severity="normal",
                source_columns=valid_source_cols,
                evidence=evidence,
                key_figures=key_figures,
                event_role="fallback",
            ))

            if len(existing_events) + len(events) >= self.config.news_per_day:
                break

        # 극단적으로 데이터가 부족한 경우에도 10개 보장
        while len(existing_events) + len(events) < self.config.news_per_day:
            idx = len(existing_events) + len(events) + 1
            event_id = f"{date_str}_fallback_generic_{idx:02d}"

            events.append(MacroEvent(
                event_id=event_id,
                date=date_str,
                macro_angle="market_breadth",
                angle_label=f"시장 보조 점검 {idx:02d}",
                market_implication="입력된 거시·시장 지표를 바탕으로 해당 일자 시장 분위기를 확인하는 보조 이벤트",
                direction="mixed",
                severity="normal",
                source_columns=[],
                evidence={},
                key_figures={},
                event_role="fallback",
            ))

        return events

    # --------------------------------------------------------
    # Generation Rules  ← 전면 재설계
    # --------------------------------------------------------

    def _get_generation_rules(self) -> Dict[str, Any]:
        return {
            # --------------------------------------------------
            # 기본 설정
            # --------------------------------------------------
            "task": "generate_macro_market_news",
            "language": "Korean",
            "news_count_required": self.config.news_per_day,
            "detail_news_length": {
                "min_chars": self.config.detail_length_min,
                "max_chars": self.config.detail_length_max,
                "note": "공백 포함 글자 수 기준. 짧으면 부족한 느낌, 길면 AI 글 느낌이 난다. 범위를 반드시 지킨다.",
            },

            # --------------------------------------------------
            # 이벤트 사용 우선순위
            # --------------------------------------------------
            "event_priority": {
                "00_headline": "거시 뉴스 편입 기준을 통과한 예외적으로 중요한 개별 종목 사건. 반드시 소화한다.",
                "0_context": "검증된 섹터 확산도·업종 괴리 또는 예외적으로 큰 개별 종목 반응. 최우선 소화한다.",
                "1_core": "z-score 기반 유의미한 움직임이 감지된 핵심 이벤트. 반드시 우선 소화한다.",
                "2_support": "핵심 이벤트 보강용. 수치 흐름이 있으나 z 기준 미달인 경우.",
                "3_fallback": "이벤트 수 부족 시에만 사용. 특정 사건을 지어내지 말고 지표 흐름 설명에 집중한다.",
            },

            # --------------------------------------------------
            # 핵심 수치 활용 규칙
            # --------------------------------------------------
            "key_figures_usage": {
                "rule": (
                    "각 이벤트의 key_figures에 담긴 수치를 헤드라인 또는 detail_news에 반드시 한 번 이상 사용한다. "
                    "수치 없이 분위기만 서술하는 기사는 반려된다. 단, macro_event_calendar 사건은 "
                    "캘린더의 사건명·설명·영향 시장을 근거로 쓰며 입력에 없는 수치를 만들지 않는다."
                ),
                "examples": [
                    "코스피가 1.4% 하락하며 사흘 만에 반락했다.",
                    "원달러 환율이 1,340원대로 올라서며 연중 최고 수준에 근접했다.",
                    "WTI 선물이 하루 만에 2.3% 급락하며 배럴당 75달러 선을 내줬다.",
                    "장단기 금리차가 -15bp로 확대되며 경기 경보 신호가 강해졌다.",
                ],
            },

            # --------------------------------------------------
            # 문체·어조 기준
            # --------------------------------------------------
            "writing_style": {
                "tone": "한국 경제지 시황 기사 어조. 건조하고 사실 중심. 과장 없이 수치와 시장 반응을 서술한다.",
                "sentence_structure": [
                    "주어를 명확히 쓴다. '코스피가', 'WTI가', '원달러 환율이' 등.",
                    "문장은 짧게 끊는다. 한 문장에 두 가지 사실을 억지로 합치지 않는다.",
                    "인과를 쓸 때는 '~하며', '~속에', '~를 배경으로' 등 자연스러운 연결어를 쓴다.",
                    "'~의 모습입니다', '~한 흐름입니다' 같은 존댓말 종결은 쓰지 않는다.",
                    "기사 종결은 '~했다', '~됐다', '~나타났다', '~확산됐다' 등 과거형 평어체.",
                ],
                "forbidden_endings": [
                    "~의 모습입니다",
                    "~한 흐름입니다",
                    "~될 것으로 보입니다",
                    "~예상됩니다",
                    "~전망입니다",
                    "~주목됩니다",
                ],
                "forbidden_phrases": [
                    "주목할 필요가 있습니다",
                    "점검할 필요가 있습니다",
                    "판단하기 어렵습니다",
                    "시장 분위기를 확인할 필요가",
                    "~에 주목해야",
                    "~을 살펴볼 필요가",
                    "~에 대한 경계감이 커진 모습",
                ],
            },

            # --------------------------------------------------
            # 헤드라인 작성 기준
            # --------------------------------------------------
            "headline_rules": {
                "length": "15자 이상 35자 이하",
                "must_include": "핵심 지표명 또는 수치 중 하나 이상",
                "style": "신문 헤드라인. 동사형 또는 명사형 종결. 의문문 금지.",
                "examples": {
                    "good": [
                        "코스피 1.4% 하락…외국인 사흘째 순매도",
                        "원달러 1,340원 돌파, 수입물가 압력 재확대",
                        "WTI 2% 급락에 유가 부담 완화 기대",
                        "장단기 금리차 역전 심화, 경기 경보 신호 강화",
                        "금 0.8% 오르며 안전자산 선호 재부상",
                    ],
                    "bad": [
                        "국내 증시 전반 투자심리 약화",
                        "원화 약세로 대외 부담 확대",
                        "유가 상승으로 비용 부담 재부각",
                        "물가 부담 완화 신호",
                    ],
                    "reason_bad": "bad 예시는 수치가 없고 '~부각', '~확대' 등 AI 투의 추상 표현만 사용함",
                },
            },

            # --------------------------------------------------
            # detail_news 작성 기준
            # --------------------------------------------------
            "detail_news_rules": {
                "structure": [
                    "1문장: 핵심 사실 (지표명 + 수치 + 방향)",
                    "1~2문장: 원인 또는 연관 흐름 (다른 지표와의 연계, 배경)",
                    "1문장: 시장 반응 또는 파급 (투자심리, 자금 흐름, 물가, 금리 등)",
                ],
                "field_name": "detail_news",
                "do_not_use": "body",
                "length_note": f"공백 포함 {self.config.detail_length_min}~{self.config.detail_length_max}자",
                "examples": {
                    "good": (
                        "코스피가 1.4% 하락하며 사흘 만에 반락했다. "
                        "외국인이 현선물에서 동반 순매도에 나선 가운데 원달러 환율도 1,340원대로 올라서며 "
                        "위험자산 회피 심리가 겹쳤다. 코스닥 역시 1.1% 내리며 중소형주까지 약세가 확산됐다."
                    ),
                    "bad": (
                        "코스피가 약세를 보이며 국내 증시 전반에 대한 경계감이 커진 모습입니다. "
                        "투자심리가 위축되며 위험자산에 대한 회피 심리가 나타나고 있습니다."
                    ),
                    "reason_bad": "수치 없음, 존댓말 종결, AI 투 추상 표현 반복",
                },
            },

            # --------------------------------------------------
            # 작성 범위 제한
            # --------------------------------------------------
            "scope_constraints": {
                "allowed_topics": [
                    "코스피·코스닥 등 국내 증시 지수 흐름",
                    "원달러 환율 및 원화 강약 흐름",
                    "국고채 금리 및 장단기 스프레드",
                    "WTI·두바이유 등 국제유가",
                    "금 등 안전자산 가격",
                    "수출입·무역수지 등 대외 교역 지표",
                    "CPI 등 물가 지표",
                    "생산·소비·투자 등 실물경기 지표",
                    "GDELT 뉴스 톤 기반 시장 심리",
                    "S&P500·나스닥 등 미국 증시 흐름",
                    "미국 국채 금리 및 장단기 스프레드",
                    "미국 기준금리 및 통화정책 기대",
                    "macro_event_calendar에 수록된 정책·금융·지정학·원자재·팬데믹 사건",
                    "sector_context_daily 기반 업종 상승·하락 확산도와 시장 대비 업종 괴리",
                    "split_article_reaction으로 검증된 예외적으로 큰 개별 종목 반응",
                ],
                "forbidden_topics": [
                    "context overlay에 포함되지 않은 개별 기업 실적·이벤트",
                    "특정 업종 투자의견 (반도체, 화학, 운송 등 개별 업종 리뷰)",
                    "입력 근거에 없는 기관명·인물명·정책 발표",
                    "미래 예측 (전망, 예상, ~할 것으로 보임)",
                ],
                "note": (
                    "daily_market_snapshot과 macro_events의 evidence·key_figures 범위 안에서만 기사를 쓴다. "
                    "없는 사실을 만들지 않는다. major_stock 사건은 해당 종목의 큰 반응만 서술하고 "
                    "시장 전체나 업종 움직임의 원인이라고 확대 해석하지 않는다."
                ),
            },

            # --------------------------------------------------
            # angle별 기사 수 할당 (pr05에서 직접 사용)
            # --------------------------------------------------
            "angle_allocation": {
                "market_breadth":    2,
                "risk_sentiment":    2,
                "money_flow":        2,
                "policy_inflation":  2,
                "external_pressure": 2,
                "macro_regime":      1,
                "note_global": (
                    "external_pressure에 미국 증시·금리 흐름이 포함된다. "
                    "risk_sentiment 기사 중 하나는 S&P500 또는 나스닥 흐름을 다룬다."
                ),
                "note": (
                    "각 angle에서 해당 개수만큼 기사를 작성한다. "
                    "해당 angle 이벤트가 부족하면 인접 angle로 대체할 수 있다. "
                    "market_breadth(증시) 기사가 전체의 절반을 넘으면 안 된다."
                ),
            },

            # --------------------------------------------------
            # 다양성 확보
            # --------------------------------------------------
            "diversity_rules": {
                "angle_distribution": (
                    "10개 기사가 모두 같은 각도(예: 증시 하락)에서 쓰이지 않도록 한다. "
                    "macro_angle이 다른 이벤트를 활용해 환율·금리·유가·심리·실물 각도를 골고루 커버한다."
                ),
                "opening_variation": (
                    "기사 첫 문장 구조를 반복하지 않는다. "
                    "'코스피가~'로만 시작하지 말고 '원달러 환율이~', 'WTI 선물이~', '장단기 금리차가~' 등을 섞어 쓴다."
                ),
                "expression_variation": (
                    "같은 표현을 두 기사에 반복하지 않는다. "
                    "'위험자산 회피'는 한 번만. '투자심리 위축'도 한 번만. 비슷한 뜻의 다른 표현을 찾는다."
                ),
            },

            # --------------------------------------------------
            # 출력 형식
            # --------------------------------------------------
            "output_format": {
                "type": "JSON array",
                "item_fields": {
                    "date": "YYYY-MM-DD",
                    "news_id": "날짜_순번 형식 (예: 2015-03-12_01)",
                    "headline": "헤드라인",
                    "detail_news": "본문",
                    "asset_class": "macro (고정)",
                    "related_assets": "관련 자산 리스트",
                    "direction": "positive | negative | neutral | mixed",
                    "source_event_ids": "사용한 이벤트 ID 리스트",
                    "used_evidence": "실제 사용한 수치 또는 지표명 리스트",
                },
                "strict": "JSON 배열만 출력한다. 앞뒤 설명, 마크다운 코드블록 불필요.",
            },
        }

    # --------------------------------------------------------
    # Ranking
    # --------------------------------------------------------

    def _deduplicate_events(self, events: List[MacroEvent]) -> List[MacroEvent]:
        seen = set()
        result = []

        for event in events:
            key = (event.event_id, event.angle_label)
            if key in seen:
                continue

            seen.add(key)
            result.append(event)

        return result

    def _rank_events(self, events: List[MacroEvent]) -> List[MacroEvent]:
        role_score = {
            "headline": 5,
            "context": 4,
            "core": 3,
            "support": 2,
            "fallback": 1,
        }

        severity_score = {
            "strong": 4,
            "moderate": 3,
            "weak": 2,
            "normal": 1,
        }

        angle_priority = {
            "market_breadth": 6,
            "risk_sentiment": 5,
            "money_flow": 4,
            "policy_inflation": 3,
            "external_pressure": 2,
            "macro_regime": 1,
        }

        def score(event: MacroEvent) -> Tuple[int, int, int]:
            return (
                role_score.get(event.event_role, 0),
                severity_score.get(event.severity, 0),
                angle_priority.get(event.macro_angle, 0),
            )

        return sorted(events, key=score, reverse=True)

    def _apply_angle_caps(self, events: List[MacroEvent]) -> List[MacroEvent]:
        """
        angle별 이벤트 상한을 적용해 특정 angle 편중을 방지한다.

        기본 캡:
          market_breadth   : 2개 — 코스피/코스닥 기사 반복 방지
          risk_sentiment   : 2개
          money_flow       : 2개
          policy_inflation : 2개
          external_pressure: 2개
          macro_regime     : 2개

        캡 초과 이벤트는 버리지 않고 overflow로 보관.
        primary가 news_per_day보다 적으면 overflow로 보충한다.
        """
        caps: Dict[str, int] = {
            "market_breadth":    2,
            "risk_sentiment":    2,
            "money_flow":        2,
            "policy_inflation":  2,
            "external_pressure": 2,
            "macro_regime":      2,
        }

        angle_counts: Dict[str, int] = {}
        primary: List[MacroEvent] = []
        overflow: List[MacroEvent] = []

        for event in events:
            angle = event.macro_angle
            count = angle_counts.get(angle, 0)
            cap = caps.get(angle, 3)

            if count < cap:
                primary.append(event)
                angle_counts[angle] = count + 1
            else:
                overflow.append(event)

        # primary가 부족하면 overflow로 보충
        result = primary
        if len(result) < self.config.news_per_day:
            result = result + overflow

        return result

    # --------------------------------------------------------
    # Utils
    # --------------------------------------------------------

    def _get(self, row: pd.Series, col: str) -> Optional[float]:
        if col not in row.index:
            return None

        value = row[col]
        return self._clean_value(value)

    def _clean_value(self, value: Any) -> Optional[float]:
        if pd.isna(value):
            return None

        if isinstance(value, (bool, np.bool_)):
            return int(value)

        if isinstance(value, (np.integer, int)):
            return int(value)

        if isinstance(value, (np.floating, float)):
            if np.isinf(value):
                return None
            return round(float(value), 6)

        try:
            value = float(value)
            if np.isinf(value):
                return None
            return round(value, 6)
        except Exception:
            return None

    def _safe_mean(self, values: List[Optional[float]]) -> Optional[float]:
        clean = []

        for value in values:
            if value is None:
                continue

            try:
                if np.isnan(value):
                    continue
            except TypeError:
                pass

            clean.append(value)

        if not clean:
            return None

        return round(float(np.mean(clean)), 6)

    def _is_meaningful(self, z_value: Optional[float]) -> bool:
        if z_value is None:
            return False

        return abs(z_value) >= self.config.min_event_abs_z

    def _z_to_signal(self, z_value: float) -> str:
        if z_value >= self.config.strong_event_abs_z:
            return "strong_high"

        if z_value >= self.config.min_event_abs_z:
            return "high"

        if z_value <= -self.config.strong_event_abs_z:
            return "strong_low"

        if z_value <= -self.config.min_event_abs_z:
            return "low"

        return "normal"

    def _severity_from_z(self, z_value: Optional[float]) -> str:
        if z_value is None:
            return "normal"

        abs_z = abs(z_value)

        if abs_z >= self.config.strong_event_abs_z:
            return "strong"

        if abs_z >= self.config.min_event_abs_z:
            return "moderate"

        if abs_z >= self.config.weak_event_abs_z:
            return "weak"

        return "normal"

    def _direction_from_value(
        self,
        value: Optional[float],
        positive_when_up: bool = True,
    ) -> str:
        if value is None:
            return "mixed"

        if value > 0:
            return "positive" if positive_when_up else "negative"

        if value < 0:
            return "negative" if positive_when_up else "positive"

        return "neutral"

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    def _save_jsonl(self, records: List[Dict[str, Any]]) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.config.output_path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _save_report(self, report_df: pd.DataFrame) -> None:
        self.config.report_path.parent.mkdir(parents=True, exist_ok=True)
        report_df.to_csv(self.config.report_path, index=False, encoding="utf-8-sig")


# ============================================================
# 5. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input-path",
        type=str,
        default="data/processed/macro_signal_daily_cleaned.csv",
    )

    parser.add_argument(
        "--output-path",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--report-path",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--year",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--news-per-day",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--macro-event-calendar",
        type=str,
        default=None,
        help="날짜가 검증된 주요 거시 사건 캘린더 CSV (선택)",
    )

    parser.add_argument(
        "--official-release-calendar",
        type=str,
        default=None,
        help="공식 URL·발표일·수치가 검증된 기관 발표 캘린더 CSV (선택)",
    )

    parser.add_argument(
        "--context-overlay",
        type=str,
        default=None,
        help="섹터 분위기와 중요 개별 종목 사건을 담은 날짜별 JSONL (선택)",
    )

    parser.add_argument(
        "--min-event-abs-z",
        type=float,
        default=1.0,
    )

    parser.add_argument(
        "--strong-event-abs-z",
        type=float,
        default=1.8,
    )

    parser.add_argument(
        "--weak-event-abs-z",
        type=float,
        default=0.3,
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.output_path is None:
        if args.year is not None:
            output_path = f"data/raw/news_generation_input_{args.year}.jsonl"
        else:
            output_path = "data/raw/news_generation_input_macro.jsonl"
    else:
        output_path = args.output_path

    if args.report_path is None:
        if args.year is not None:
            report_path = f"data/processed/news_generation_input_{args.year}_report.csv"
        else:
            report_path = "data/processed/news_generation_input_macro_report.csv"
    else:
        report_path = args.report_path

    config = MacroNewsInputConfig(
        input_path=Path(args.input_path),
        output_path=Path(output_path),
        report_path=Path(report_path),
        macro_event_calendar_path=(
            Path(args.macro_event_calendar) if args.macro_event_calendar else None
        ),
        official_release_calendar_path=(
            Path(args.official_release_calendar) if args.official_release_calendar else None
        ),
        context_overlay_path=(Path(args.context_overlay) if args.context_overlay else None),
        year=args.year,
        start_date=args.start_date,
        end_date=args.end_date,
        news_per_day=args.news_per_day,
        min_event_abs_z=args.min_event_abs_z,
        strong_event_abs_z=args.strong_event_abs_z,
        weak_event_abs_z=args.weak_event_abs_z,
    )

    builder = MacroNewsInputBuilder(config)
    builder.run()


if __name__ == "__main__":
    main()
