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
    "itmId": "T1",
    "objL1": "ALL",
    "objL2": "",
    "objL3": "",
    "objL4": "",
    "objL5": "",
    "objL6": "",
    "objL7": "",
    "objL8": "",
    "format": "json",
    "jsonVD": "Y",
    "prdSe": "M",
    "startPrdDe": "201301",
    "endPrdDe": "202401",
    "orgId": "101",
    "tblId": "DT_1C8015",
}

resp = requests.get(url, params=params, timeout=60)
resp.raise_for_status()
data = resp.json()

df = pd.DataFrame(data)

print("[원본 컬럼]")
print(df.columns.tolist())
print(df.head())

# 필요한 행만 추출: 선행종합지수(2020=100)
# 실제 컬럼명은 보통 ITM_NM 또는 C1_NM 쪽에 들어있음
target_mask = False

if "ITM_NM" in df.columns:
    target_mask = target_mask | df["ITM_NM"].astype(str).str.contains("선행종합지수", na=False)

if "C1_NM" in df.columns:
    target_mask = target_mask | df["C1_NM"].astype(str).str.contains("선행종합지수", na=False)

if "C2_NM" in df.columns:
    target_mask = target_mask | df["C2_NM"].astype(str).str.contains("선행종합지수", na=False)

df_target = df[target_mask].copy()

# 전월비/순환변동치/기타 보조행 제거
exclude_keywords = ["전월비", "순환변동치", "전년동월비", "재고순환", "경제심리", "수출입", "코스피", "장단기금리차"]
for col in ["ITM_NM", "C1_NM", "C2_NM"]:
    if col in df_target.columns:
        for kw in exclude_keywords:
            df_target = df_target[~df_target[col].astype(str).str.contains(kw, na=False)]

# 날짜/값 정리
df_target["date"] = pd.to_datetime(df_target["PRD_DE"].astype(str), format="%Y%m", errors="coerce")
df_target["leading_index"] = pd.to_numeric(df_target["DT"], errors="coerce")

result = df_target[["date", "leading_index"]].dropna().sort_values("date").reset_index(drop=True)

print("\n[필터 후 결과]")
print(result.head())
print(result.tail())
print("행 수:", len(result))

os.makedirs("data/raw", exist_ok=True)
output_path = "data/raw/korea_leading_index_201301_202401.csv"
result.to_csv(output_path, index=False, encoding="utf-8-sig")
print("저장 완료:", output_path)