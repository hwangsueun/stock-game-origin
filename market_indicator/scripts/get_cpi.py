import os
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

ECOS_API_KEY = os.getenv("ECOS_API_KEY")
if not ECOS_API_KEY:
    raise ValueError("ECOS_API_KEY가 .env에 없습니다.")

STAT_CODE = "901Y009"   # 소비자물가지수
CYCLE = "M"
START = "201301"
END = "202312"
ITEM_CODE_1 = "0"       # 총지수

url = (
    f"https://ecos.bok.or.kr/api/StatisticSearch/"
    f"{ECOS_API_KEY}/json/kr/1/1000/"
    f"{STAT_CODE}/{CYCLE}/{START}/{END}/{ITEM_CODE_1}"
)

resp = requests.get(url, timeout=30)
resp.raise_for_status()
data = resp.json()

rows = data.get("StatisticSearch", {}).get("row", [])
if not rows:
    raise ValueError("데이터가 비어 있습니다. API KEY 또는 통계코드를 확인하세요.")

df = pd.DataFrame(rows)

keep_cols = [col for col in ["TIME", "DATA_VALUE", "STAT_NAME", "ITEM_NAME1"] if col in df.columns]
df = df[keep_cols].copy()

df["TIME"] = pd.to_datetime(df["TIME"], format="%Y%m")
df["DATA_VALUE"] = pd.to_numeric(df["DATA_VALUE"], errors="coerce")

df = df.sort_values("TIME").reset_index(drop=True)
df = df.rename(columns={
    "TIME": "date",
    "DATA_VALUE": "cpi"
})

os.makedirs("data/raw", exist_ok=True)
output_path = "data/raw/korea_cpi_2013_2023.csv"
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(df.head())
print(df.tail())
print(f"저장 완료: {output_path}")