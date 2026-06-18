import os
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

KOSIS_API_KEY = os.getenv("KOSIS_API_KEY")
if not KOSIS_API_KEY:
    raise ValueError("KOSIS_API_KEY가 .env에 없습니다.")

url = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

params = {
    "method": "getList",
    "apiKey": KOSIS_API_KEY,
    "itmId": "T80",
    "objL1": "ALL",
    "objL2": "ALL",
    "objL3": "ALL",
    "objL4": "",
    "objL5": "",
    "objL6": "",
    "objL7": "",
    "objL8": "",
    "format": "json",
    "jsonVD": "Y",
    "prdSe": "Q",
    "startPrdDe": "201201",
    "endPrdDe": "202401",
    "orgId": "101",
    "tblId": "DT_1DA7107S",
}

resp = requests.get(url, params=params, timeout=60)
resp.raise_for_status()
data = resp.json()

if not isinstance(data, list) or len(data) == 0:
    print(data)
    raise ValueError("데이터가 비어 있습니다.")

df = pd.DataFrame(data)

# 전국 + 전체 연령만 남기기
df = df[(df["C1"] == "00") & (df["C2"] == "00")].copy()

# 날짜형 변환
df["date"] = pd.to_datetime(df["PRD_DE"].astype(str), format="%Y%m", errors="coerce")
df["unemployment_rate"] = pd.to_numeric(df["DT"], errors="coerce")

# 2013-12-30 ~ 2023-12-31 포함
start_date = pd.Timestamp("2012-01-01")
end_date = pd.Timestamp("2023-12-31")

result = df[["date", "unemployment_rate"]].dropna().copy()
result = result[(result["date"] >= pd.Timestamp("2013-12-01")) & (result["date"] <= end_date)]
result = result.sort_values("date").reset_index(drop=True)

os.makedirs("data/raw", exist_ok=True)
output_path = "data/raw/korea_unemployment_rate_2013-01-01_to_2023-12-31.csv"
result.to_csv(output_path, index=False, encoding="utf-8-sig")

print(result.head())
print(result.tail())
print("행 수:", len(result))
print("저장 완료:", output_path)