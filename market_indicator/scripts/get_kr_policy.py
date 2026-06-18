import os
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ECOS_API_KEY")
if not API_KEY:
    raise ValueError("ECOS_API_KEY가 .env에 없습니다.")

url = (
    f"https://ecos.bok.or.kr/api/StatisticSearch/{API_KEY}/xml/kr/1/10000/"
    "722Y001/D/20130101/20231231/0101000"
)

response = requests.get(url, timeout=30)
response.raise_for_status()

root = ET.fromstring(response.content)

# ECOS 에러 응답 체크
result_code = root.findtext(".//CODE")
result_msg = root.findtext(".//MESSAGE")
if result_code and result_code != "INFO-000":
    raise ValueError(f"ECOS API 오류: {result_code} / {result_msg}")

rows = []
for row in root.findall(".//row"):
    date_value = row.findtext("TIME")
    rate_value = row.findtext("DATA_VALUE")
    rows.append({
        "date": date_value,
        "rate": rate_value
    })

df = pd.DataFrame(rows)

if df.empty:
    raise ValueError("데이터가 비어 있습니다. 통계표 코드나 항목 코드를 다시 확인해야 합니다.")

df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
df["rate"] = pd.to_numeric(df["rate"], errors="coerce")

df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

print(df.head())
print(df.tail())
print(f"rows: {len(df)}")

output_path = "kr_policy_rate_20130101_20231231.csv"
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(f"saved: {output_path}")