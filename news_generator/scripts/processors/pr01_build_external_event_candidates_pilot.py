from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class ExternalPilotConfig:
    year: int = 2020

    project_root: Path = Path(".")
    dart_json_path: Path = Path("dart_collector/dart_results_2013_2023.json")
    gdelt_parquet_dir: Path = Path("scripts/output")
    output_dir: Path = Path("data/raw")

    forbidden_terms: str = (
        "장중 급등|장 초반 강세|시초가|고가|저가|고점 대비 하락|저점 반등|"
        "갭상승|갭하락|장 마감 직전 매수세"
    )

    def resolve_paths(self) -> "ExternalPilotConfig":
        self.project_root = self.project_root.resolve()

        if not self.dart_json_path.is_absolute():
            self.dart_json_path = self.project_root / self.dart_json_path

        if not self.gdelt_parquet_dir.is_absolute():
            self.gdelt_parquet_dir = self.project_root / self.gdelt_parquet_dir

        if not self.output_dir.is_absolute():
            self.output_dir = self.project_root / self.output_dir

        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self


class DateParser:
    def parse_series(self, series: pd.Series) -> pd.Series:
        s = series.astype(str).str.strip()

        parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

        raw_candidates = [
            s,
            s.str.replace(r"\.0$", "", regex=True),
            s.str.replace("-", "", regex=False)
             .str.replace("/", "", regex=False)
             .str.replace(".", "", regex=False)
             .str.replace(" ", "", regex=False),
        ]

        formats = [
            "%Y%m%d",
            "%Y%m%d%H%M%S",
            "%Y%m",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y.%m.%d",
        ]

        for raw in raw_candidates:
            for fmt in formats:
                candidate = pd.to_datetime(raw, format=fmt, errors="coerce")
                parsed = parsed.fillna(candidate)

        parsed = parsed.fillna(pd.to_datetime(series, errors="coerce"))

        return parsed


class TextCleaner:
    @staticmethod
    def clean(value) -> str:
        if pd.isna(value):
            return ""

        text = str(value).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def remove_spaces(value) -> str:
        return TextCleaner.clean(value).replace(" ", "")


class DartDisclosureProcessor:
    def __init__(self, config: ExternalPilotConfig):
        self.config = config
        self.date_parser = DateParser()

    def run(self) -> pd.DataFrame:
        if not self.config.dart_json_path.exists():
            raise FileNotFoundError(f"DART JSON 없음: {self.config.dart_json_path}")

        records = self._load_records(self.config.dart_json_path)
        df = pd.DataFrame(records)

        if df.empty:
            raise ValueError("DART JSON에서 record를 추출하지 못함")

        print(f"[DART 원본 rows] {len(df):,}")
        print(f"[DART 컬럼] {list(df.columns)}")

        normalized = self._normalize(df)
        normalized = normalized[normalized["date"].dt.year == self.config.year].copy()

        print(f"[DART {self.config.year} rows] {len(normalized):,}")

        events = self._build_events(normalized)

        print(f"[DART 이벤트 rows] {len(events):,}")
        return events

    def _load_records(self, path: Path) -> List[Dict]:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)

        records: List[Dict] = []

        date_cols = {
            "rcept_dt",
            "receipt_date",
            "date",
            "rcept_date",
            "접수일자",
            "공시일자",
        }
        report_cols = {
            "report_nm",
            "report_name",
            "title",
            "공시제목",
            "보고서명",
        }
        corp_cols = {
            "corp_name",
            "company_name",
            "corp_nm",
            "기업명",
            "회사명",
        }

        def is_dart_like_list(items) -> bool:
            if not isinstance(items, list):
                return False

            dict_items = [x for x in items if isinstance(x, dict)]

            if not dict_items:
                return False

            sample_cols = set()

            for item in dict_items[:30]:
                sample_cols.update(item.keys())

            has_date = bool(sample_cols & date_cols)
            has_report = bool(sample_cols & report_cols)
            has_corp = bool(sample_cols & corp_cols)

            return has_date and has_report and has_corp

        def walk(x):
            if isinstance(x, list):
                if is_dart_like_list(x):
                    records.extend([item for item in x if isinstance(item, dict)])

                for item in x:
                    if isinstance(item, (dict, list)):
                        walk(item)

            elif isinstance(x, dict):
                for value in x.values():
                    if isinstance(value, (dict, list)):
                        walk(value)

        walk(obj)

        if not records:
            return []

        df = pd.DataFrame(records)

        raw_count = len(df)

        if "rcept_no" in df.columns:
            df = df.drop_duplicates(subset=["rcept_no"])
        else:
            dedup_cols = [
                col for col in ["rcept_dt", "stock_code", "corp_name", "report_nm"]
                if col in df.columns
            ]

            if dedup_cols:
                df = df.drop_duplicates(subset=dedup_cols)
            else:
                df = df.drop_duplicates()

        print(f"[DART flatten records] raw={raw_count:,}, dedup={len(df):,}")

        if "rcept_dt" in df.columns:
            years = pd.to_datetime(
                df["rcept_dt"].astype(str),
                format="%Y%m%d",
                errors="coerce",
            ).dt.year.value_counts().sort_index()

            print("[DART flatten 연도 분포]")
            print(years.to_string())

        return df.to_dict("records")
    
    
    def _find_largest_list_of_dicts(self, obj) -> List[Dict]:
        candidates: List[List[Dict]] = []

        def walk(x):
            if isinstance(x, list):
                dict_items = [item for item in x if isinstance(item, dict)]
                if dict_items:
                    candidates.append(dict_items)

                for item in x:
                    walk(item)

            elif isinstance(x, dict):
                for value in x.values():
                    walk(value)

        walk(obj)

        if not candidates:
            return []

        return max(candidates, key=len)

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        date_col = self._find_first_existing(
            df,
            ["rcept_dt", "receipt_date", "date", "rcept_date", "접수일자", "공시일자"],
        )
        report_col = self._find_first_existing(
            df,
            ["report_nm", "report_name", "title", "공시제목", "보고서명"],
        )
        corp_col = self._find_first_existing(
            df,
            ["corp_name", "company_name", "corp_nm", "기업명", "회사명"],
        )
        ticker_col = self._find_first_existing(
            df,
            ["stock_code", "ticker", "종목코드", "stock_cd"],
            required=False,
        )
        receipt_no_col = self._find_first_existing(
            df,
            ["rcept_no", "receipt_no", "접수번호"],
            required=False,
        )

        out = pd.DataFrame()
        out["date"] = self.date_parser.parse_series(df[date_col])
        out["report_nm"] = df[report_col].apply(TextCleaner.clean)
        out["company_name"] = df[corp_col].apply(TextCleaner.clean)

        if ticker_col is not None:
            out["ticker"] = (
                df[ticker_col]
                .astype(str)
                .str.replace(r"\.0$", "", regex=True)
                .str.replace("nan", "", regex=False)
                .str.strip()
                .str.zfill(6)
            )
            out.loc[out["ticker"].eq("000nan"), "ticker"] = ""
        else:
            out["ticker"] = ""

        if receipt_no_col is not None:
            out["receipt_no"] = df[receipt_no_col].apply(TextCleaner.clean)
        else:
            out["receipt_no"] = ""

        out = out.dropna(subset=["date"])
        out = out[out["report_nm"].ne("")].copy()

        return out

    def _find_first_existing(
        self,
        df: pd.DataFrame,
        candidates: List[str],
        required: bool = True,
    ) -> Optional[str]:
        for col in candidates:
            if col in df.columns:
                return col

        lowered = {str(col).lower(): col for col in df.columns}

        for candidate in candidates:
            key = candidate.lower()
            if key in lowered:
                return lowered[key]

        if required:
            raise ValueError(
                f"필수 컬럼을 찾지 못함: {candidates} / 현재 컬럼={list(df.columns)}"
            )

        return None

    def _build_events(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = []

        for _, row in df.iterrows():
            report_nm = row["report_nm"]
            event_type, direction, strength, news_style = self._classify_report(report_nm)

            if event_type == "ignore":
                continue

            ticker = row["ticker"]
            company_name = row["company_name"]
            asset_id = ticker if ticker else company_name

            rows.append(
                {
                    "date": row["date"].strftime("%Y-%m-%d"),
                    "source_table": f"disclosure_event_candidates_{self.config.year}",
                    "signal_group": "disclosure",
                    "asset_class": "stock",
                    "asset_id": asset_id,
                    "market": "",
                    "sector": "",
                    "ticker": ticker,
                    "company_name": company_name,
                    "event_type": event_type,
                    "event_frame": (
                        f"{company_name}의 '{report_nm}' 공시가 확인되어 "
                        f"종목별 뉴스 문맥에 반영할 수 있음"
                    ),
                    "direction": direction,
                    "strength": strength,
                    "evidence_1": f"공시명: {report_nm}",
                    "evidence_2": f"접수번호: {row['receipt_no']}" if row["receipt_no"] else "",
                    "evidence_3": "DART 공시 메타데이터 기반",
                    "news_style": news_style,
                    "forbidden_terms": self.config.forbidden_terms,
                }
            )

        event_df = pd.DataFrame(rows)

        if event_df.empty:
            return self._empty_event_df()

        event_df = event_df.sort_values(["date", "strength"], ascending=[True, False])
        return event_df.reset_index(drop=True)

    def _classify_report(self, report_nm: str) -> Tuple[str, str, int, str]:
        text = TextCleaner.remove_spaces(report_nm)

        rules = [
            (["단일판매", "공급계약", "수주"], "supply_contract", "positive", 4, "disclosure_supply_contract"),
            (["잠정실적", "영업실적", "매출액", "손익구조", "실적"], "earnings", "neutral", 3, "disclosure_earnings"),
            (["유상증자"], "capital_increase", "negative", 4, "disclosure_financing"),
            (["무상증자"], "bonus_issue", "positive", 3, "disclosure_capital_event"),
            (["자기주식취득", "자사주취득"], "share_buyback", "positive", 4, "disclosure_shareholder_return"),
            (["자기주식처분", "자사주처분"], "share_disposal", "negative", 3, "disclosure_shareholder_return"),
            (["배당", "현금ㆍ현물배당", "현금배당"], "dividend", "positive", 3, "disclosure_dividend"),
            (["소송", "피소", "손해배상"], "litigation", "negative", 5, "disclosure_risk"),
            (["횡령", "배임"], "legal_risk", "negative", 5, "disclosure_risk"),
            (["최대주주", "주요주주", "지분변동"], "ownership_change", "neutral", 3, "disclosure_ownership"),
            (["합병", "분할", "영업양수", "영업양도"], "restructuring", "neutral", 4, "disclosure_restructuring"),
            (["투자판단관련주요경영사항"], "major_management_issue", "neutral", 3, "disclosure_major_management"),
            (["상장폐지", "관리종목", "거래정지"], "listing_risk", "negative", 5, "disclosure_risk"),
        ]

        for keywords, event_type, direction, strength, news_style in rules:
            if any(keyword in text for keyword in keywords):
                return event_type, direction, strength, news_style

        return "ignore", "neutral", 1, "ignore"

    def _empty_event_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "date",
                "source_table",
                "signal_group",
                "asset_class",
                "asset_id",
                "market",
                "sector",
                "ticker",
                "company_name",
                "event_type",
                "event_frame",
                "direction",
                "strength",
                "evidence_1",
                "evidence_2",
                "evidence_3",
                "news_style",
                "forbidden_terms",
            ]
        )


class GdeltProcessor:
    def __init__(self, config: ExternalPilotConfig):
        self.config = config
        self.date_parser = DateParser()

        self.theme_rules = {
            "interest_rate_policy": {
                "label": "금리·통화정책",
                "keywords": ["INTEREST_RATE", "CENTRAL_BANK", "MONETARY", "FED", "RATE"],
                "asset_class": "macro",
            },
            "inflation_price": {
                "label": "물가·인플레이션",
                "keywords": ["INFLATION", "PRICE", "COST_OF_LIVING", "CONSUMER_PRICE"],
                "asset_class": "macro",
            },
            "oil_energy": {
                "label": "유가·에너지",
                "keywords": ["OIL", "ENERGY", "FUEL", "GAS", "PETROLEUM"],
                "asset_class": "commodity",
            },
            "trade_export": {
                "label": "무역·수출입",
                "keywords": ["TRADE", "EXPORT", "IMPORT", "TARIFF", "SUPPLY_CHAIN"],
                "asset_class": "macro",
            },
            "stock_market": {
                "label": "주식시장",
                "keywords": ["STOCK", "STOCKMARKET", "EQUITY", "SECURITIES", "FINANCE"],
                "asset_class": "market",
            },
            "labor_employment": {
                "label": "고용·노동시장",
                "keywords": ["UNEMPLOYMENT", "EMPLOYMENT", "JOBS", "LABOR"],
                "asset_class": "macro",
            },
            "geopolitical_risk": {
                "label": "지정학·정책 리스크",
                "keywords": ["WAR", "SANCTION", "CRISIS", "PROTEST", "POLITICAL", "TERROR"],
                "asset_class": "global",
            },
            "pandemic_health": {
                "label": "감염병·보건 리스크",
                "keywords": ["PANDEMIC", "COVID", "CORONAVIRUS", "DISEASE", "HEALTH"],
                "asset_class": "global",
            },
            "crypto": {
                "label": "가상자산",
                "keywords": ["BITCOIN", "CRYPTO", "CRYPTOCURRENCY", "BLOCKCHAIN"],
                "asset_class": "crypto",
            },
        }

    def run(self) -> pd.DataFrame:
        parquet_files = self._find_year_files()

        if not parquet_files:
            raise FileNotFoundError(
                f"GDELT {self.config.year}년 parquet 파일 없음: {self.config.gdelt_parquet_dir}"
            )

        print(f"[GDELT {self.config.year} 파일 수] {len(parquet_files)}")
        for path in parquet_files:
            print(f"  - {path.name}")

        parts = []

        for path in parquet_files:
            part = self._process_one_parquet(path)
            if not part.empty:
                parts.append(part)

        if not parts:
            print("[GDELT] theme 매칭 row 없음")
            return self._empty_event_df()

        article_df = pd.concat(parts, ignore_index=True)

        print(f"[GDELT theme article rows] {len(article_df):,}")

        daily_theme = self._aggregate_daily_theme(article_df)
        events = self._build_events(daily_theme)

        print(f"[GDELT 이벤트 rows] {len(events):,}")
        return events

    def _find_year_files(self) -> List[Path]:
        pattern = f"gkg_{self.config.year}*.parquet"
        return sorted(self.config.gdelt_parquet_dir.glob(pattern))

    def _process_one_parquet(self, path: Path) -> pd.DataFrame:
        df = pd.read_parquet(path)

        print(f"[GDELT 읽기] {path.name} rows={len(df):,} cols={len(df.columns)}")
        print(f"[GDELT 컬럼 샘플] {list(df.columns)[:30]}")

        date_col = self._find_col(
            df,
            [
                "ref_date",
                "published_at",
                "date",
                "DATE",
                "Date",
                "day",
                "Day",
                "SQLDATE",
                "MonthYear",
            ],
            required=True,
        )

        theme_col = self._find_col(
            df,
            [
                "themes_json",
                "themes_raw",
                "V2Themes",
                "Themes",
                "themes",
                "theme",
                "THEMES",
            ],
            required=True,
        )

        tone_col = self._find_col(
            df,
            [
                "tone_score",
                "V2Tone",
                "Tone",
                "tone",
                "AvgTone",
                "avg_tone",
            ],
            required=False,
        )

        source_col = self._find_col(
            df,
            [
                "source_name",
                "SourceCommonName",
                "source",
                "domain",
                "source_domain",
            ],
            required=False,
        )

        url_col = self._find_col(
            df,
            [
                "url",
                "URL",
                "DocumentIdentifier",
                "document_identifier",
            ],
            required=False,
        )

        use_cols = [date_col, theme_col]

        for col in [tone_col, source_col, url_col]:
            if col is not None and col not in use_cols:
                use_cols.append(col)

        temp = df[use_cols].copy()

        temp["date"] = self.date_parser.parse_series(temp[date_col])
        temp = temp.dropna(subset=["date"])
        temp = temp[temp["date"].dt.year == self.config.year].copy()

        if temp.empty:
            return pd.DataFrame()

        temp["theme_raw"] = temp[theme_col].apply(self._themes_to_search_text)

        if tone_col is not None:
            temp["tone"] = temp[tone_col].apply(self._parse_tone)
        else:
            temp["tone"] = np.nan

        if source_col is not None:
            temp["source"] = temp[source_col].astype(str)
        else:
            temp["source"] = ""

        if url_col is not None:
            temp["url"] = temp[url_col].astype(str)
        else:
            temp["url"] = ""

        matched = []

        for theme_key, rule in self.theme_rules.items():
            pattern = "|".join([re.escape(keyword.upper()) for keyword in rule["keywords"]])
            mask = temp["theme_raw"].str.contains(pattern, na=False)

            part = temp.loc[mask, ["date", "tone", "source", "url"]].copy()

            if part.empty:
                continue

            part["theme_key"] = theme_key
            part["theme_label"] = rule["label"]
            part["asset_class"] = rule["asset_class"]
            matched.append(part)

        if not matched:
            return pd.DataFrame()

        return pd.concat(matched, ignore_index=True)
    
    def _themes_to_search_text(self, value) -> str:
        if value is None:
            return ""

        if isinstance(value, float) and pd.isna(value):
            return ""

        if isinstance(value, (list, tuple, set, dict)):
            flattened = self._flatten_json_like(value)
            return " ".join(flattened).upper()

        text = str(value).strip()

        if not text:
            return ""

        # themes_json이 JSON 문자열인 경우 처리
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
                flattened = self._flatten_json_like(parsed)
                return " ".join(flattened).upper()
            except Exception:
                pass

        return text.upper()

    def _flatten_json_like(self, value) -> List[str]:
        result: List[str] = []

        def walk(x):
            if x is None:
                return

            if isinstance(x, float) and pd.isna(x):
                return

            if isinstance(x, str):
                cleaned = x.strip()
                if cleaned:
                    result.append(cleaned)
                return

            if isinstance(x, (int, float)):
                result.append(str(x))
                return

            if isinstance(x, dict):
                for v in x.values():
                    walk(v)
                return

            if isinstance(x, (list, tuple, set)):
                for item in x:
                    walk(item)
                return

            result.append(str(x))

        walk(value)
        return result

    def _find_col(
        self,
        df: pd.DataFrame,
        candidates: List[str],
        required: bool,
    ) -> Optional[str]:
        for col in candidates:
            if col in df.columns:
                return col

        lowered = {str(col).lower(): col for col in df.columns}

        for candidate in candidates:
            key = candidate.lower()
            if key in lowered:
                return lowered[key]

        if required:
            raise ValueError(
                f"GDELT 필수 컬럼 없음: {candidates} / 현재 컬럼={list(df.columns)}"
            )

        return None

    def _parse_tone(self, value) -> float:
        if value is None:
            return np.nan

        if isinstance(value, float) and pd.isna(value):
            return np.nan

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()

        if not text:
            return np.nan

        # 기존 GDELT V2Tone 형식: "AvgTone,PositiveScore,NegativeScore,..."
        if "," in text:
            text = text.split(",")[0]

        try:
            return float(text)
        except Exception:
            return np.nan

    def _aggregate_daily_theme(self, article_df: pd.DataFrame) -> pd.DataFrame:
        df = article_df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()

        grouped = (
            df.groupby(["date", "theme_key", "theme_label", "asset_class"], as_index=False)
            .agg(
                article_count=("theme_key", "size"),
                avg_tone=("tone", "mean"),
                source_count=("source", pd.Series.nunique),
                sample_url=("url", "first"),
            )
        )

        grouped = grouped.sort_values(["theme_key", "date"]).reset_index(drop=True)

        grouped["article_count_7d_avg"] = (
            grouped.groupby("theme_key")["article_count"]
            .transform(lambda s: s.rolling(7, min_periods=3).mean())
        )
        grouped["article_count_30d_avg"] = (
            grouped.groupby("theme_key")["article_count"]
            .transform(lambda s: s.rolling(30, min_periods=7).mean())
        )
        grouped["article_count_30d_std"] = (
            grouped.groupby("theme_key")["article_count"]
            .transform(lambda s: s.rolling(30, min_periods=7).std())
        )

        grouped["volume_zscore_30d"] = (
            (grouped["article_count"] - grouped["article_count_30d_avg"])
            / grouped["article_count_30d_std"].replace(0, np.nan)
        )

        grouped["tone_abs"] = grouped["avg_tone"].abs()

        return grouped

    def _build_events(self, daily_theme: pd.DataFrame) -> pd.DataFrame:
        df = daily_theme.copy()

        mask = (
            (df["article_count"] >= 5)
            & (
                (df["volume_zscore_30d"] >= 1.5)
                | (df["tone_abs"] >= 2.0)
            )
        )

        df = df.loc[mask].copy()

        if df.empty:
            return self._empty_event_df()

        df["direction"] = np.select(
            [
                df["avg_tone"] >= 1.0,
                df["avg_tone"] <= -1.0,
            ],
            [
                "positive",
                "negative",
            ],
            default="neutral",
        )

        df["strength"] = df.apply(self._calc_strength, axis=1)

        rows = []

        for _, row in df.iterrows():
            rows.append(
                {
                    "date": row["date"].strftime("%Y-%m-%d"),
                    "source_table": f"gdelt_event_candidates_{self.config.year}",
                    "signal_group": "gdelt_news",
                    "asset_class": row["asset_class"],
                    "asset_id": row["theme_key"],
                    "market": "",
                    "sector": "",
                    "ticker": "",
                    "company_name": "",
                    "event_type": "gdelt_theme_event",
                    "event_frame": (
                        f"GDELT 기준 {row['theme_label']} 관련 보도량과 감성 변화가 관측되어 "
                        f"시장 해석 뉴스의 배경 문맥으로 사용할 수 있음"
                    ),
                    "direction": row["direction"],
                    "strength": int(row["strength"]),
                    "evidence_1": f"관련 기사 수 {int(row['article_count'])}건",
                    "evidence_2": f"평균 Tone {row['avg_tone']:.2f}",
                    "evidence_3": f"30일 보도량 z-score {row['volume_zscore_30d']:.2f}",
                    "news_style": "gdelt_market_context",
                    "forbidden_terms": self.config.forbidden_terms,
                }
            )

        event_df = pd.DataFrame(rows)
        event_df = event_df.sort_values(["date", "strength"], ascending=[True, False])
        return event_df.reset_index(drop=True)

    def _calc_strength(self, row: pd.Series) -> int:
        z = row.get("volume_zscore_30d", np.nan)
        tone_abs = row.get("tone_abs", np.nan)

        z = 0.0 if pd.isna(z) else float(z)
        tone_abs = 0.0 if pd.isna(tone_abs) else float(tone_abs)

        score = z + tone_abs * 0.5

        if score >= 5:
            return 5
        if score >= 3:
            return 4
        return 3

    def _empty_event_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "date",
                "source_table",
                "signal_group",
                "asset_class",
                "asset_id",
                "market",
                "sector",
                "ticker",
                "company_name",
                "event_type",
                "event_frame",
                "direction",
                "strength",
                "evidence_1",
                "evidence_2",
                "evidence_3",
                "news_style",
                "forbidden_terms",
            ]
        )


class ExternalEventPilot:
    def __init__(self, config: ExternalPilotConfig):
        self.config = config

    def run(self) -> None:
        disclosure_events = DartDisclosureProcessor(self.config).run()
        gdelt_events = GdeltProcessor(self.config).run()

        external_events = self._combine(disclosure_events, gdelt_events)
        self._save(disclosure_events, gdelt_events, external_events)

    def _combine(
        self,
        disclosure_events: pd.DataFrame,
        gdelt_events: pd.DataFrame,
    ) -> pd.DataFrame:
        frames = []

        if not disclosure_events.empty:
            frames.append(disclosure_events)

        if not gdelt_events.empty:
            frames.append(gdelt_events)

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        df["strength"] = pd.to_numeric(df["strength"], errors="coerce").fillna(1).astype(int)

        df = df.sort_values(["date", "strength"], ascending=[True, False])
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")

        return df.reset_index(drop=True)

    def _save(
        self,
        disclosure_events: pd.DataFrame,
        gdelt_events: pd.DataFrame,
        external_events: pd.DataFrame,
    ) -> None:
        year = self.config.year

        disclosure_path = self.config.output_dir / f"disclosure_event_candidates_{year}.csv"
        gdelt_path = self.config.output_dir / f"gdelt_event_candidates_{year}.csv"
        external_path = self.config.output_dir / f"external_event_candidates_{year}.csv"

        disclosure_events.to_csv(disclosure_path, index=False, encoding="utf-8-sig")
        gdelt_events.to_csv(gdelt_path, index=False, encoding="utf-8-sig")
        external_events.to_csv(external_path, index=False, encoding="utf-8-sig")

        print("=" * 80)
        print(f"[저장 완료] {disclosure_path}")
        print(f"[저장 완료] {gdelt_path}")
        print(f"[저장 완료] {external_path}")
        print(f"[공시 이벤트 수] {len(disclosure_events):,}")
        print(f"[GDELT 이벤트 수] {len(gdelt_events):,}")
        print(f"[외부 이벤트 합계] {len(external_events):,}")
        print("=" * 80)

        if not external_events.empty:
            print("[외부 이벤트 샘플]")
            print(external_events.head(30))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument("--year", type=int, default=2020)
    parser.add_argument("--project-root", type=str, default=".")
    parser.add_argument("--dart-json", type=str, default="dart_collector/dart_results_2013_2023.json")
    parser.add_argument("--gdelt-dir", type=str, default="scripts/output")
    parser.add_argument("--output-dir", type=str, default="data/raw")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = ExternalPilotConfig(
        year=args.year,
        project_root=Path(args.project_root),
        dart_json_path=Path(args.dart_json),
        gdelt_parquet_dir=Path(args.gdelt_dir),
        output_dir=Path(args.output_dir),
    ).resolve_paths()

    print("=" * 80)
    print("[pr01 외부 이벤트 후보 생성 시작]")
    print(f"project_root: {config.project_root}")
    print(f"year: {config.year}")
    print(f"dart_json_path: {config.dart_json_path}")
    print(f"gdelt_parquet_dir: {config.gdelt_parquet_dir}")
    print(f"output_dir: {config.output_dir}")
    print("=" * 80)

    pilot = ExternalEventPilot(config)
    pilot.run()


if __name__ == "__main__":
    main()