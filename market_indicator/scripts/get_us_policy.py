import pandas as pd

url = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv"
    "?id=DFEDTARU&cosd=2013-01-01&coed=2023-12-31"
)

df = pd.read_csv(url)

print(df.head())
print(df.columns)

df = df.rename(columns={
    "observation_date": "date",
    "DFEDTARU": "rate"
})

df["date"] = pd.to_datetime(df["date"], errors="coerce")
df["rate"] = pd.to_numeric(df["rate"], errors="coerce")

df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

print(df.head())
print(df.tail())
print(f"rows: {len(df)}")

output_path = "us_policy_rate_20130101_20231231.csv"
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(f"saved: {output_path}")