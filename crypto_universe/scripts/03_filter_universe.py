import os
import pandas as pd

INPUT_PATH = "data/processed/coin_marketcap_scan.csv"
OUTPUT_PATH = "data/processed/coin_universe_selected.csv"

MARKET_CAP_THRESHOLD = 50_000_000
MIN_VALID_DAYS = 30

df = pd.read_csv(INPUT_PATH)

# 예전 컬럼명 / 새 컬럼명 둘 다 대응
if "max_market_cap" in df.columns:
    market_cap_col = "max_market_cap"
elif "max_market_cap_2014_2023" in df.columns:
    market_cap_col = "max_market_cap_2014_2023"
else:
    raise ValueError(
        f"시가총액 컬럼을 찾을 수 없음. 현재 컬럼: {list(df.columns)}"
    )

if "valid_days" not in df.columns:
    raise ValueError(
        f"valid_days 컬럼을 찾을 수 없음. 현재 컬럼: {list(df.columns)}"
    )

df[market_cap_col] = pd.to_numeric(df[market_cap_col], errors="coerce")
df["valid_days"] = pd.to_numeric(df["valid_days"], errors="coerce")

selected = df[
    (df[market_cap_col] >= MARKET_CAP_THRESHOLD) &
    (df["valid_days"] >= MIN_VALID_DAYS)
].copy()

selected = selected.sort_values(
    by=market_cap_col,
    ascending=False
).reset_index(drop=True)

os.makedirs("data/processed", exist_ok=True)
selected.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

print(f"selected: {len(selected)} coins")
print(f"saved to: {OUTPUT_PATH}")
print(f"market cap column used: {market_cap_col}")