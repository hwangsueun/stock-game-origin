import pandas as pd
import yfinance as yf

ticker = "GC=F"

df = yf.download(
    ticker,
    start="2013-01-01",
    end="2024-01-01",   # end는 보통 제외 처리라 2024-01-01로 둠
    interval="1d",
    auto_adjust=False,
    progress=False
)

df = df.reset_index()

# yfinance에서 컬럼이 MultiIndex로 들어오는 경우 대비
if isinstance(df.columns, pd.MultiIndex):
    df.columns = [col[0] if col[0] else col[1] for col in df.columns]

# 필요한 컬럼만 추출
df = df[["Date", "Adj Close", "Volume"]].copy()

# 컬럼명 변경
df = df.rename(columns={
    "Date": "date",
    "Adj Close": "adj_close",
    "Volume": "volume"
})

# 날짜 정리
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)

print(df.head())
print(df.tail())
print(f"rows: {len(df)}")

# 저장
output_path = "gold_price_20130101_20231231.csv"
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(f"saved: {output_path}")