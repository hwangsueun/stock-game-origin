from pathlib import Path

import pandas as pd
import yfinance as yf


class YFinanceIndexCollector:
    def __init__(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        output_path: str,
    ):
        self.ticker = ticker
        self.start_date = start_date
        self.end_date = end_date
        self.output_path = Path(output_path)

    def download(self) -> pd.DataFrame:
        df = yf.download(
            self.ticker,
            start=self.start_date,
            end=self.end_date,
            interval="1d",
            auto_adjust=False,
            progress=False,
        )

        if df.empty:
            raise ValueError(f"다운로드된 데이터가 비어 있습니다. ticker={self.ticker}")

        df = df.reset_index()

        # yfinance에서 컬럼이 MultiIndex로 들어오는 경우 대비
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] if col[0] else col[1] for col in df.columns]

        keep_cols = ["Date", "Adj Close", "Volume"]
        df = df[[col for col in keep_cols if col in df.columns]].copy()

        if "Volume" not in df.columns:
            df["Volume"] = pd.NA

        df = df.rename(
            columns={
                "Date": "date",
                "Adj Close": "adj_close",
                "Volume": "volume",
            }
        )

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

        df = df.dropna(subset=["date", "adj_close"]).copy()
        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        return df[["date", "adj_close", "volume"]]

    def save(self, df: pd.DataFrame) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.output_path, index=False, encoding="utf-8-sig")

        print(df.head())
        print(df.tail())
        print(f"rows: {len(df)}")
        print(f"saved: {self.output_path}")

    def run(self) -> pd.DataFrame:
        df = self.download()
        self.save(df)
        return df


def main():
    collector = YFinanceIndexCollector(
        ticker="^KQ11",
        start_date="2013-01-01",
        end_date="2024-01-01",
        output_path="data/raw/kosdaq_20130101_20231231.csv",
    )

    collector.run()


if __name__ == "__main__":
    main()