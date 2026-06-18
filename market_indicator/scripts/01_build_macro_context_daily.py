from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


class SafeCsvReader:
    ENCODINGS = ["utf-8-sig", "utf-8", "cp949", "euc-kr", "latin1"]

    def read(self, path: Path) -> Tuple[pd.DataFrame, str]:
        last_error = None

        for encoding in self.ENCODINGS:
            try:
                df = pd.read_csv(path, encoding=encoding)
                return df, encoding
            except UnicodeDecodeError as e:
                last_error = e
            except Exception as e:
                last_error = e

        raise RuntimeError(f"CSV 읽기 실패: {path} / last_error={last_error}")


class DateNormalizer:
    DATE_CANDIDATES = [
        "date",
        "Date",
        "DATE",
        "날짜",
        "일자",
        "기준일",
        "기간",
        "PRD_DE",
        "time",
        "Time",
    ]

    def find_date_column(self, df: pd.DataFrame) -> str:
        for col in self.DATE_CANDIDATES:
            if col in df.columns:
                return col

        for col in df.columns:
            c = str(col).lower()
            if "date" in c or "prd" in c or "날짜" in c or "일자" in c or "기간" in c:
                return col

        raise ValueError(f"date 컬럼을 찾지 못함. columns={list(df.columns)}")

    def parse(self, series: pd.Series) -> pd.Series:
        s = series.astype(str).str.strip()

        s = s.str.replace(r"\.0$", "", regex=True)
        s = s.str.replace("년", "-", regex=False)
        s = s.str.replace("월", "-", regex=False)
        s = s.str.replace("일", "", regex=False)
        s = s.str.replace(".", "-", regex=False)
        s = s.str.replace("/", "-", regex=False)
        s = s.str.replace(" ", "", regex=False)

        # 2013-01-01- 처럼 끝에 -가 남는 경우 제거
        s = s.str.replace(r"-+$", "", regex=True)

        parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

        formats = [
            "%Y%m%d",
            "%Y%m",
            "%Y-%m-%d",
            "%Y-%m",
        ]

        for fmt in formats:
            candidate = pd.to_datetime(s, format=fmt, errors="coerce")
            parsed = parsed.fillna(candidate)

        candidate = pd.to_datetime(s, errors="coerce")
        parsed = parsed.fillna(candidate)

        return parsed

class NumericCleaner:
    def clean_series(self, series: pd.Series) -> pd.Series:
        s = series.astype(str).str.strip()
        s = s.str.replace(",", "", regex=False)
        s = s.str.replace("%", "", regex=False)
        s = s.str.replace(" ", "", regex=False)
        s = s.replace({"": None, "-": None, "nan": None, "None": None})
        return pd.to_numeric(s, errors="coerce")


class MacroContextBuilder:
    def __init__(
        self,
        raw_dir: str = "data/raw",
        output_dir: str = "data/processed",
        start_date: str = "2013-01-01",
        end_date: str = "2023-12-31",
    ):
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.start_date = pd.Timestamp(start_date)
        self.end_date = pd.Timestamp(end_date)

        self.reader = SafeCsvReader()
        self.date_normalizer = DateNormalizer()
        self.numeric_cleaner = NumericCleaner()

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.macro = pd.DataFrame(
            {"date": pd.date_range(self.start_date, self.end_date, freq="D")}
        )

        self.logs: List[str] = []

    def run(self) -> None:
        self._merge_daily_level_files()
        self._merge_monthly_files()
        self._merge_international_oil_file()
        self._finalize()
        self._save()

    def _merge_daily_level_files(self) -> None:
        daily_specs = [
            ("kospi_20130101_20231231.csv", {"adj_close": "kospi"}),
            ("kosdaq_20130101_20231231.csv", {"adj_close": "kosdaq"}),
            ("nasdaq_20130101_20231231.csv", {"adj_close": "nasdaq"}),
            ("sp500_20130101_20231231.csv", {"adj_close": "sp500"}),
            ("gold_price_20130101_20231231.csv", {"adj_close": "gold_price"}),
            ("usdkrw_20130101_20231231.csv", {"adj_close": "usdkrw"}),
            ("wti_price_20130101_20231231.csv", {"adj_close": "wti_price"}),
            ("kr_policy_rate_20130101_20231231.csv", {"rate": "kr_policy_rate"}),
            ("us_policy_rate_20130101_20231231.csv", {"rate": "us_policy_rate"}),
            (
                "kr_bond_rates_merged_20130101_20231231.csv",
                {
                    "ktb_3y_rate": "ktb_3y_rate",
                    "ktb_5y_rate": "ktb_5y_rate",
                    "ktb_10y_rate": "ktb_10y_rate",
                    "corp_aa_minus_3y_rate": "corp_aa_minus_3y_rate",
                    "corp_bbb_minus_3y_rate": "corp_bbb_minus_3y_rate",
                    "cd_91d_rate": "cd_91d_rate",
                    "corp_aa_minus_spread": "corp_aa_minus_spread",
                    "corp_bbb_minus_spread": "corp_bbb_minus_spread",
                    "ktb_10y_3y_spread": "ktb_10y_3y_spread",
                },
            ),
            (
                "us_treasury_rates_merged_20130101_20231231.csv",
                {
                    "us_treasury_2y_rate": "us_treasury_2y_rate",
                    "us_treasury_5y_rate": "us_treasury_5y_rate",
                    "us_treasury_10y_rate": "us_treasury_10y_rate",
                    "us_treasury_30y_rate": "us_treasury_30y_rate",
                    "us_10y_2y_spread": "us_10y_2y_spread",
                    "us_30y_2y_spread": "us_30y_2y_spread",
                },
            ),
        ]

        for file_name, column_map in daily_specs:
            self._merge_file(
                file_name=file_name,
                column_map=column_map,
                monthly=False,
                allow_missing=False,
            )

    def _merge_monthly_files(self) -> None:
        monthly_specs = [
            (
                "korea_cpi_2013_2023.csv",
                {
                    "cpi": "cpi",
                },
            ),
            (
                "korea_leading_index_201301_202401.csv",
                {
                    "leading_index": "leading_index",
                },
            ),
            (
                "korea_trade_201301_202312.csv",
                {
                    "export_amount_usd_thousand": "export_amount_usd_thousand",
                    "import_amount_usd_thousand": "import_amount_usd_thousand",
                    "trade_balance_usd_thousand": "trade_balance_usd_thousand",
                },
            ),
            (
                "korea_real_activity_201301_202312.csv",
                {
                    "industrial_production_index": "industrial_production_index",
                    "mining_manufacturing_production_index": "mining_manufacturing_production_index",
                    "retail_sales_index": "retail_sales_index",
                    "facility_investment_index": "facility_investment_index",
                },
            ),
        ]

        for file_name, column_map in monthly_specs:
            self._merge_file(
                file_name=file_name,
                column_map=column_map,
                monthly=True,
                allow_missing=False,
            )

    def _merge_file(
        self,
        file_name: str,
        column_map: Dict[str, str],
        monthly: bool,
        allow_missing: bool,
    ) -> None:
        path = self.raw_dir / file_name

        if not path.exists():
            msg = f"[스킵] 파일 없음: {file_name}"
            if allow_missing:
                self.logs.append(msg)
                print(msg)
                return
            raise FileNotFoundError(path)

        df, encoding = self.reader.read(path)
        date_col = self.date_normalizer.find_date_column(df)
        df["date"] = self.date_normalizer.parse(df[date_col])

        if monthly:
            df["date"] = df["date"].dt.to_period("M").dt.to_timestamp()

        df = df[(df["date"] >= self.start_date) & (df["date"] <= self.end_date)].copy()

        missing_cols = [col for col in column_map if col not in df.columns]
        if missing_cols:
            raise ValueError(
                f"{file_name}에 필요한 컬럼이 없음: {missing_cols} / 현재 컬럼={list(df.columns)}"
            )

        keep_cols = ["date"] + list(column_map.keys())
        df = df[keep_cols].rename(columns=column_map)

        for col in column_map.values():
            df[col] = self.numeric_cleaner.clean_series(df[col])

        df = df.dropna(subset=["date"])
        df = df.groupby("date", as_index=False).last()

        before_cols = set(self.macro.columns)
        self.macro = self.macro.merge(df, on="date", how="left")
        added_cols = [c for c in self.macro.columns if c not in before_cols]

        self.logs.append(
            f"[병합] {file_name} | encoding={encoding} | monthly={monthly} | added={added_cols}"
        )
        print(self.logs[-1])

    def _merge_international_oil_file(self) -> None:
        candidates = sorted(self.raw_dir.glob("국제_원유가격*.csv"))

        if not candidates:
            print("[스킵] 국제 원유가격 파일 없음")
            return

        path = candidates[0]
        df, encoding = self.reader.read(path)

        print(f"[국제 원유가격 읽기 성공] {path.name} | encoding={encoding}")
        print(f"[국제 원유가격 컬럼] {list(df.columns)}")

        date_col = self.date_normalizer.find_date_column(df)
        df = df.copy()
        df["date"] = self.date_normalizer.parse(df[date_col])
        df = df.dropna(subset=["date"])

        if "Dubai" not in df.columns:
            raise ValueError(
                f"국제 원유가격 파일에 Dubai 컬럼이 없음. 현재 컬럼={list(df.columns)}"
            )

        oil_df = df[["date", "Dubai"]].copy()
        oil_df = oil_df.rename(columns={"Dubai": "dubai_oil_price"})
        oil_df["dubai_oil_price"] = self.numeric_cleaner.clean_series(
            oil_df["dubai_oil_price"]
        )

        oil_df = oil_df[
            (oil_df["date"] >= self.start_date) &
            (oil_df["date"] <= self.end_date)
        ].copy()

        oil_df = oil_df.groupby("date", as_index=False).last()

        self.macro = self.macro.merge(oil_df, on="date", how="left")

        print(f"[국제 원유가격 병합] columns={list(oil_df.columns)}")
        
    def _extract_oil_prices(self, df: pd.DataFrame) -> pd.DataFrame:
        date_col = self.date_normalizer.find_date_column(df)
        df = df.copy()
        df["date"] = self.date_normalizer.parse(df[date_col])
        df = df.dropna(subset=["date"])

        wide = self._extract_oil_prices_from_wide(df)
        if not wide.empty:
            return wide

        long = self._extract_oil_prices_from_long(df)
        if not long.empty:
            return long

        return pd.DataFrame()

    def _extract_oil_prices_from_wide(self, df: pd.DataFrame) -> pd.DataFrame:
        result = pd.DataFrame({"date": df["date"]})

        patterns = {
            "dubai_oil_price": ["dubai", "두바이"],
            "wti_oil_price_official": ["wti", "서부텍사스"],
            "brent_oil_price": ["brent", "브렌트"],
        }

        matched_count = 0

        for output_col, keywords in patterns.items():
            source_col = self._find_column_by_keywords(df.columns, keywords)
            if source_col is not None:
                result[output_col] = self.numeric_cleaner.clean_series(df[source_col])
                matched_count += 1

        if matched_count == 0:
            return pd.DataFrame()

        return result

    def _extract_oil_prices_from_long(self, df: pd.DataFrame) -> pd.DataFrame:
        product_col = self._find_product_column(df)
        price_col = self._find_price_column(df, exclude_cols=["date", product_col])

        if product_col is None or price_col is None:
            return pd.DataFrame()

        temp = df[["date", product_col, price_col]].copy()
        temp["product_key"] = temp[product_col].astype(str).str.lower()
        temp["price"] = self.numeric_cleaner.clean_series(temp[price_col])

        rows = []

        mapping = [
            ("dubai_oil_price", ["dubai", "두바이"]),
            ("wti_oil_price_official", ["wti", "서부텍사스"]),
            ("brent_oil_price", ["brent", "브렌트"]),
        ]

        for output_col, keywords in mapping:
            mask = temp["product_key"].apply(
                lambda x: any(keyword.lower() in x for keyword in keywords)
            )
            part = temp.loc[mask, ["date", "price"]].copy()
            if len(part) == 0:
                continue
            part = part.rename(columns={"price": output_col})
            rows.append(part)

        if not rows:
            return pd.DataFrame()

        result = rows[0]
        for part in rows[1:]:
            result = result.merge(part, on="date", how="outer")

        return result

    def _find_column_by_keywords(self, columns, keywords: List[str]) -> Optional[str]:
        for col in columns:
            c = str(col).lower()
            for keyword in keywords:
                if keyword.lower() in c:
                    return col
        return None

    def _find_product_column(self, df: pd.DataFrame) -> Optional[str]:
        object_cols = [
            col for col in df.columns
            if col != "date" and df[col].dtype == "object"
        ]

        keywords = ["dubai", "두바이", "wti", "brent", "브렌트", "서부텍사스"]

        best_col = None
        best_count = 0

        for col in object_cols:
            values = df[col].astype(str).str.lower()
            count = 0
            for keyword in keywords:
                count += values.str.contains(keyword.lower(), na=False).sum()

            if count > best_count:
                best_count = count
                best_col = col

        return best_col if best_count > 0 else None

    def _find_price_column(
        self,
        df: pd.DataFrame,
        exclude_cols: List[Optional[str]],
    ) -> Optional[str]:
        exclude = {col for col in exclude_cols if col is not None}

        name_keywords = ["가격", "price", "value", "값", "유가"]

        for col in df.columns:
            if col in exclude:
                continue
            c = str(col).lower()
            if any(keyword in c for keyword in name_keywords):
                numeric_ratio = self.numeric_cleaner.clean_series(df[col]).notna().mean()
                if numeric_ratio >= 0.5:
                    return col

        best_col = None
        best_ratio = 0.0

        for col in df.columns:
            if col in exclude:
                continue
            numeric_ratio = self.numeric_cleaner.clean_series(df[col]).notna().mean()
            if numeric_ratio > best_ratio:
                best_ratio = numeric_ratio
                best_col = col

        return best_col if best_ratio >= 0.5 else None

    def _finalize(self) -> None:
        self.macro = self.macro.sort_values("date").reset_index(drop=True)

        value_cols = [col for col in self.macro.columns if col != "date"]

        for col in value_cols:
            self.macro[col] = pd.to_numeric(self.macro[col], errors="coerce")

        self.macro[value_cols] = self.macro[value_cols].ffill().bfill()

        self.macro = self.macro[
            (self.macro["date"] >= self.start_date) &
            (self.macro["date"] <= self.end_date)
        ].copy()

    def _save(self) -> None:
        output_path = self.output_dir / "macro_context_daily.csv"
        missing_report_path = self.output_dir / "macro_context_daily_missing_report.csv"
        log_path = self.output_dir / "macro_context_daily_build_log.txt"

        missing_report = (
            self.macro.isna()
            .sum()
            .reset_index()
            .rename(columns={"index": "column", 0: "missing_count"})
        )
        missing_report["missing_ratio"] = missing_report["missing_count"] / len(self.macro)

        self.macro.to_csv(output_path, index=False, encoding="utf-8-sig")
        missing_report.to_csv(missing_report_path, index=False, encoding="utf-8-sig")

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.logs))

        print("=" * 80)
        print(f"[저장 완료] {output_path}")
        print(f"[결측 리포트 저장] {missing_report_path}")
        print(f"[로그 저장] {log_path}")
        print(f"[행 수] {len(self.macro):,}")
        print(f"[기간] {self.macro['date'].min().date()} ~ {self.macro['date'].max().date()}")
        print("[컬럼]")
        for col in self.macro.columns:
            print(f"  - {col}")
        print("=" * 80)


if __name__ == "__main__":
    builder = MacroContextBuilder(
        raw_dir="data/raw",
        output_dir="data/processed",
        start_date="2013-01-01",
        end_date="2023-12-31",
    )
    builder.run()