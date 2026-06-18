import pandas as pd
from pathlib import Path


class FredTreasuryRateCollector:
    def __init__(
        self,
        start_date: str = "2013-01-01",
        end_date: str = "2023-12-31",
        output_dir: str = "data/raw",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.series_map = {
            "DGS2": {
                "rate_col": "us_treasury_2y_rate",
                "output": "us_treasury_2y_20130101_20231231.csv",
            },
            "DGS5": {
                "rate_col": "us_treasury_5y_rate",
                "output": "us_treasury_5y_20130101_20231231.csv",
            },
            "DGS10": {
                "rate_col": "us_treasury_10y_rate",
                "output": "us_treasury_10y_20130101_20231231.csv",
            },
            "DGS30": {
                "rate_col": "us_treasury_30y_rate",
                "output": "us_treasury_30y_20130101_20231231.csv",
            },
        }

    def _build_url(self, series_id: str) -> str:
        return (
            "https://fred.stlouisfed.org/graph/fredgraph.csv"
            f"?id={series_id}&cosd={self.start_date}&coed={self.end_date}"
        )

    def fetch_one_series(self, series_id: str, rate_col: str) -> pd.DataFrame:
        url = self._build_url(series_id)

        df = pd.read_csv(url)

        if df.empty:
            raise ValueError(f"{series_id} 데이터가 비어 있습니다.")

        if "observation_date" not in df.columns:
            raise ValueError(f"{series_id} 날짜 컬럼이 없습니다. columns={list(df.columns)}")

        if series_id not in df.columns:
            raise ValueError(f"{series_id} 값 컬럼이 없습니다. columns={list(df.columns)}")

        df = df.rename(columns={
            "observation_date": "date",
            series_id: rate_col,
        })

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df[rate_col] = pd.to_numeric(df[rate_col], errors="coerce")

        df = df.dropna(subset=["date"]).copy()
        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        return df[["date", rate_col]].copy()

    def save_one(self, df: pd.DataFrame, filename: str) -> None:
        output_path = self.output_dir / filename
        df.to_csv(output_path, index=False, encoding="utf-8-sig")

        print("\n" + "=" * 80)
        print(f"saved: {output_path}")
        print(df.head())
        print(df.tail())
        print(f"rows: {len(df)}")

    def run(self) -> pd.DataFrame:
        series_dfs = []

        for series_id, meta in self.series_map.items():
            print(f"\n[수집 시작] {series_id}")

            df = self.fetch_one_series(
                series_id=series_id,
                rate_col=meta["rate_col"],
            )

            self.save_one(df, meta["output"])
            series_dfs.append(df)

        merged = series_dfs[0]

        for df in series_dfs[1:]:
            merged = merged.merge(df, on="date", how="outer")

        merged = merged.sort_values("date").reset_index(drop=True)

        merged["us_10y_2y_spread"] = (
            merged["us_treasury_10y_rate"] - merged["us_treasury_2y_rate"]
        )

        merged["us_30y_2y_spread"] = (
            merged["us_treasury_30y_rate"] - merged["us_treasury_2y_rate"]
        )

        output_path = self.output_dir / "us_treasury_rates_merged_20130101_20231231.csv"
        merged.to_csv(output_path, index=False, encoding="utf-8-sig")

        print("\n" + "=" * 80)
        print(f"merged saved: {output_path}")
        print(merged.head())
        print(merged.tail())
        print(f"rows: {len(merged)}")

        return merged


def main():
    collector = FredTreasuryRateCollector(
        start_date="2013-01-01",
        end_date="2023-12-31",
        output_dir="data/raw",
    )

    collector.run()


if __name__ == "__main__":
    main()