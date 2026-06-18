import os
from pathlib import Path

import pandas as pd


class RawFileAuditor:
    def __init__(self, raw_dir: str, output_dir: str):
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self):
        rows = []

        csv_files = sorted(self.raw_dir.glob("*.csv"))

        print(f"[RAW CSV 파일 수] {len(csv_files)}")

        for file_path in csv_files:
            info = self._audit_one_file(file_path)
            rows.append(info)

        result = pd.DataFrame(rows)
        output_path = self.output_dir / "raw_file_audit_report.csv"
        result.to_csv(output_path, index=False, encoding="utf-8-sig")

        print(f"[저장 완료] {output_path}")
        print(result)

    def _audit_one_file(self, file_path: Path) -> dict:
        info = {
            "file_name": file_path.name,
            "file_path": str(file_path),
            "rows": None,
            "cols": None,
            "columns": None,
            "date_column": None,
            "date_min": None,
            "date_max": None,
            "date_unique_count": None,
            "period_type_guess": None,
            "missing_total": None,
            "missing_ratio_mean": None,
            "read_error": None,
        }

        try:
            df = pd.read_csv(file_path)
        except Exception as e:
            info["read_error"] = str(e)
            return info

        info["rows"] = len(df)
        info["cols"] = len(df.columns)
        info["columns"] = " | ".join(map(str, df.columns.tolist()))
        info["missing_total"] = int(df.isna().sum().sum())
        info["missing_ratio_mean"] = round(float(df.isna().mean().mean()), 4)

        date_col = self._find_date_column(df)

        if date_col is not None:
            info["date_column"] = date_col

            parsed = self._parse_date_series(df[date_col])
            valid_dates = parsed.dropna()

            if len(valid_dates) > 0:
                info["date_min"] = valid_dates.min().strftime("%Y-%m-%d")
                info["date_max"] = valid_dates.max().strftime("%Y-%m-%d")
                info["date_unique_count"] = valid_dates.nunique()
                info["period_type_guess"] = self._guess_period_type(valid_dates)

        return info

    def _find_date_column(self, df: pd.DataFrame):
        candidates = [
            "date",
            "Date",
            "DATE",
            "일자",
            "날짜",
            "기준일",
            "PRD_DE",
            "time",
            "Time",
        ]

        for col in candidates:
            if col in df.columns:
                return col

        for col in df.columns:
            lower = str(col).lower()
            if "date" in lower or "prd" in lower or "일자" in lower or "날짜" in lower:
                return col

        return None

    def _parse_date_series(self, series: pd.Series) -> pd.Series:
        s = series.astype(str).str.strip()

        # 201301 형태
        parsed_yyyymm = pd.to_datetime(s, format="%Y%m", errors="coerce")

        # 20130101 형태
        parsed_yyyymmdd = pd.to_datetime(s, format="%Y%m%d", errors="coerce")

        # 일반 날짜 형태
        parsed_general = pd.to_datetime(s, errors="coerce")

        parsed = parsed_yyyymm.copy()
        parsed = parsed.fillna(parsed_yyyymmdd)
        parsed = parsed.fillna(parsed_general)

        return parsed

    def _guess_period_type(self, valid_dates: pd.Series) -> str:
        unique_dates = pd.Series(valid_dates.sort_values().unique())

        if len(unique_dates) <= 1:
            return "unknown"

        diffs = unique_dates.diff().dropna().dt.days

        median_diff = diffs.median()

        if median_diff <= 3:
            return "daily"
        if 25 <= median_diff <= 35:
            return "monthly"
        if 80 <= median_diff <= 100:
            return "quarterly"
        if 350 <= median_diff <= 380:
            return "yearly"

        return f"irregular_median_diff_{median_diff}"


if __name__ == "__main__":
    RAW_DIR = "data/raw"
    OUTPUT_DIR = "data/processed"

    auditor = RawFileAuditor(
        raw_dir=RAW_DIR,
        output_dir=OUTPUT_DIR,
    )
    auditor.run()