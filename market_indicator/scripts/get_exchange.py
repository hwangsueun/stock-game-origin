import pandas as pd
import yfinance as yf

ticker = "KRW=X"

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

# 필요한 컬럼만 추출
keep_cols = ["Date", "Adj Close", "Volume"]
df = df[[col for col in keep_cols if col in df.columns]].copy()

# 혹시 Volume이 아예 없으면 빈 컬럼 생성
if "Volume" not in df.columns:
    df["Volume"] = pd.NA

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
print(df["volume"].isna().sum(), "missing volume rows")
print(f"rows: {len(df)}")

# 저장
output_path = "usdkrw_20130101_20231231.csv"
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(f"saved: {output_path}")