import os
import re
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from pathlib import Path
from dotenv import load_dotenv


class EcosBondRateCollector:
    """
    ECOS 817Y002 일별 시장금리 데이터 수집기.

    수집 대상:
    - 국고채 3년
    - 국고채 5년
    - 국고채 10년
    - 회사채 3년 AA-
    - 회사채 3년 BBB-
    - CD 91일

    출력:
    - data/raw/kr_ktb_3y_20130101_20231231.csv
    - data/raw/kr_ktb_5y_20130101_20231231.csv
    - data/raw/kr_ktb_10y_20130101_20231231.csv
    - data/raw/kr_corp_aa_minus_3y_20130101_20231231.csv
    - data/raw/kr_corp_bbb_minus_3y_20130101_20231231.csv
    - data/raw/kr_cd_91d_20130101_20231231.csv
    - data/raw/kr_bond_rates_merged_20130101_20231231.csv
    """

    BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
    STAT_CODE = "817Y002"
    CYCLE = "D"

    def __init__(
        self,
        api_key: str,
        start_date: str = "20130101",
        end_date: str = "20231231",
        output_dir: str = "data/raw",
    ):
        self.api_key = api_key
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 확실한 항목코드
        self.fixed_targets = {
            "kr_ktb_3y": {
                "item_code": "010200000",
                "rate_col": "ktb_3y_rate",
                "output": "kr_ktb_3y_20130101_20231231.csv",
            },
            "kr_ktb_5y": {
                "item_code": "010200001",
                "rate_col": "ktb_5y_rate",
                "output": "kr_ktb_5y_20130101_20231231.csv",
            },
            "kr_ktb_10y": {
                "item_code": "010210000",
                "rate_col": "ktb_10y_rate",
                "output": "kr_ktb_10y_20130101_20231231.csv",
            },
            "kr_corp_aa_minus_3y": {
                "item_code": "010310000",
                "rate_col": "corp_aa_minus_3y_rate",
                "output": "kr_corp_aa_minus_3y_20130101_20231231.csv",
            },
            "kr_corp_bbb_minus_3y": {
                "item_code": "010320000",
                "rate_col": "corp_bbb_minus_3y_rate",
                "output": "kr_corp_bbb_minus_3y_20130101_20231231.csv",
            },
        }

    @staticmethod
    def _normalize_text(value: str) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", "", str(value)).upper()

    def _build_url(
        self,
        start_idx: int,
        end_idx: int,
        start_date: str,
        end_date: str,
        item_code: str | None = None,
    ) -> str:
        parts = [
            self.BASE_URL,
            self.api_key,
            "xml",
            "kr",
            str(start_idx),
            str(end_idx),
            self.STAT_CODE,
            self.CYCLE,
            start_date,
            end_date,
        ]

        if item_code:
            parts.append(item_code)

        return "/".join(parts)

    def _request_xml(
        self,
        start_idx: int,
        end_idx: int,
        start_date: str,
        end_date: str,
        item_code: str | None = None,
    ) -> ET.Element:
        url = self._build_url(
            start_idx=start_idx,
            end_idx=end_idx,
            start_date=start_date,
            end_date=end_date,
            item_code=item_code,
        )

        response = requests.get(url, timeout=30)
        response.raise_for_status()

        root = ET.fromstring(response.content)

        result_code = root.findtext(".//CODE")
        result_msg = root.findtext(".//MESSAGE")

        if result_code and result_code != "INFO-000":
            raise ValueError(f"ECOS API 오류: {result_code} / {result_msg}")

        return root

    def _get_total_count(
        self,
        start_date: str,
        end_date: str,
        item_code: str | None = None,
    ) -> int:
        root = self._request_xml(
            start_idx=1,
            end_idx=1,
            start_date=start_date,
            end_date=end_date,
            item_code=item_code,
        )

        total_text = root.findtext(".//list_total_count")

        if total_text is None:
            rows = root.findall(".//row")
            return len(rows)

        return int(total_text)

    def _fetch_rows(
        self,
        start_date: str,
        end_date: str,
        item_code: str | None = None,
        page_size: int = 10000,
    ) -> list[dict]:
        total_count = self._get_total_count(
            start_date=start_date,
            end_date=end_date,
            item_code=item_code,
        )

        if total_count == 0:
            return []

        rows = []

        for start_idx in range(1, total_count + 1, page_size):
            end_idx = min(start_idx + page_size - 1, total_count)

            root = self._request_xml(
                start_idx=start_idx,
                end_idx=end_idx,
                start_date=start_date,
                end_date=end_date,
                item_code=item_code,
            )

            for row in root.findall(".//row"):
                rows.append({
                    "stat_code": row.findtext("STAT_CODE"),
                    "stat_name": row.findtext("STAT_NAME"),
                    "item_code": row.findtext("ITEM_CODE1"),
                    "item_name": row.findtext("ITEM_NAME1"),
                    "date": row.findtext("TIME"),
                    "rate": row.findtext("DATA_VALUE"),
                    "unit": row.findtext("UNIT_NAME"),
                })

        return rows

    def discover_item_codes(self) -> pd.DataFrame:
        """
        817Y002 항목코드 확인용.
        짧은 구간을 조회해서 ITEM_CODE1 / ITEM_NAME1 목록을 출력한다.
        """
        rows = self._fetch_rows(
            start_date="20230102",
            end_date="20230110",
            item_code=None,
        )

        df = pd.DataFrame(rows)

        if df.empty:
            raise ValueError("817Y002 항목 목록 조회 결과가 비어 있습니다.")

        code_map = (
            df[["item_code", "item_name", "unit"]]
            .drop_duplicates()
            .sort_values(["item_code", "item_name"])
            .reset_index(drop=True)
        )

        print("\n[817Y002 항목코드 목록]")
        print(code_map.to_string(index=False))

        return code_map

    def find_item_code_by_name(self, code_map: pd.DataFrame, keyword: str) -> str:
        keyword_norm = self._normalize_text(keyword)

        candidates = code_map.copy()
        candidates["norm_name"] = candidates["item_name"].apply(self._normalize_text)

        matched = candidates[candidates["norm_name"].str.contains(keyword_norm, regex=False)]

        if matched.empty:
            raise ValueError(
                f"항목명에서 '{keyword}'를 찾지 못했습니다.\n"
                f"전체 항목:\n{code_map.to_string(index=False)}"
            )

        if len(matched) > 1:
            print(f"\n[주의] '{keyword}' 매칭 후보가 여러 개입니다.")
            print(matched[["item_code", "item_name", "unit"]].to_string(index=False))
            print("첫 번째 후보를 사용합니다.")

        return matched.iloc[0]["item_code"]

    def fetch_one_series(
        self,
        item_code: str,
        rate_col: str,
    ) -> pd.DataFrame:
        rows = self._fetch_rows(
            start_date=self.start_date,
            end_date=self.end_date,
            item_code=item_code,
        )

        df = pd.DataFrame(rows)

        if df.empty:
            raise ValueError(f"데이터가 비어 있습니다. item_code={item_code}")

        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        df[rate_col] = pd.to_numeric(df["rate"], errors="coerce")

        df = df.dropna(subset=["date"]).copy()
        df = df[["date", rate_col]].copy()
        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        return df

    def save_one(self, df: pd.DataFrame, filename: str) -> None:
        output_path = self.output_dir / filename
        df.to_csv(output_path, index=False, encoding="utf-8-sig")

        print("\n" + "=" * 80)
        print(f"saved: {output_path}")
        print(df.head())
        print(df.tail())
        print(f"rows: {len(df)}")

    def run(self):
        code_map = self.discover_item_codes()

        targets = dict(self.fixed_targets)

        # CD 91일은 항목명으로 자동 탐색
        cd_91d_code = self.find_item_code_by_name(code_map, "CD(91일)")
        targets["kr_cd_91d"] = {
            "item_code": cd_91d_code,
            "rate_col": "cd_91d_rate",
            "output": "kr_cd_91d_20130101_20231231.csv",
        }

        series_dfs = []

        for name, meta in targets.items():
            item_code = meta["item_code"]
            rate_col = meta["rate_col"]
            output = meta["output"]

            print(f"\n[수집 시작] {name} / item_code={item_code}")

            df = self.fetch_one_series(
                item_code=item_code,
                rate_col=rate_col,
            )

            self.save_one(df, output)
            series_dfs.append(df)

        merged = series_dfs[0]

        for df in series_dfs[1:]:
            merged = merged.merge(df, on="date", how="outer")

        merged = merged.sort_values("date").reset_index(drop=True)

        # 뉴스 생성용 파생 변수
        if {"corp_aa_minus_3y_rate", "ktb_3y_rate"}.issubset(merged.columns):
            merged["corp_aa_minus_spread"] = (
                merged["corp_aa_minus_3y_rate"] - merged["ktb_3y_rate"]
            )

        if {"corp_bbb_minus_3y_rate", "ktb_3y_rate"}.issubset(merged.columns):
            merged["corp_bbb_minus_spread"] = (
                merged["corp_bbb_minus_3y_rate"] - merged["ktb_3y_rate"]
            )

        if {"ktb_10y_rate", "ktb_3y_rate"}.issubset(merged.columns):
            merged["ktb_10y_3y_spread"] = (
                merged["ktb_10y_rate"] - merged["ktb_3y_rate"]
            )

        merged_output = self.output_dir / "kr_bond_rates_merged_20130101_20231231.csv"
        merged.to_csv(merged_output, index=False, encoding="utf-8-sig")

        print("\n" + "=" * 80)
        print(f"merged saved: {merged_output}")
        print(merged.head())
        print(merged.tail())
        print(f"rows: {len(merged)}")

        return merged


def main():
    load_dotenv()

    api_key = os.getenv("ECOS_API_KEY")

    if not api_key:
        raise ValueError("ECOS_API_KEY가 .env에 없습니다.")

    collector = EcosBondRateCollector(
        api_key=api_key,
        start_date="20130101",
        end_date="20231231",
        output_dir="data/raw",
    )

    collector.run()


if __name__ == "__main__":
    main()