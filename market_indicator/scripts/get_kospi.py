import pandas as pd
import yfinance as yf

ticker = "^KS11"

df = yf.download(
    ticker,
    start="2013-01-01",
    end="2024-01-01",   # end는 보통 제외 처리
    interval="1d",
    auto_adjust=False,
    progress=False
)

df = df.reset_index()

# yfinance에서 컬럼이 MultiIndex로 들어오는 경우 대비
if isinstance(df.columns, pd.MultiIndex):
    df.columns = [col[0] if col[0] else col[1] for col in df.columns]

keep_cols = ["Date", "Adj Close", "Volume"]
df = df[[col for col in keep_cols if col in df.columns]].copy()

if "Volume" not in df.columns:
    df["Volume"] = pd.NA

df = df.rename(columns={
    "Date": "date",
    "Adj Close": "adj_close",
    "Volume": "volume"
})

df["date"] = pd.to_datetime(df["date"], errors="coerce")
df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

print(df.head())
print(df.tail())
print(f"rows: {len(df)}")

output_path = "kospi_20130101_20231231.csv"
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(f"saved: {output_path}")