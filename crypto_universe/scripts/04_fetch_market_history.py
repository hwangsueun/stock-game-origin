import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# =========================
# 설정
# =========================
INPUT_PATH = "data/processed/coin_universe_selected.csv"
OUTPUT_DIR = "data/raw/coin_history"
MERGED_OUTPUT_PATH = "data/processed/coin_history_all.csv"
FAILED_LOG_PATH = "data/processed/coin_history_failed.csv"

START_DATE = "2014-01-01"
END_DATE = "2023-12-31"

SAVE_PER_COIN = True
MERGE_ALL_AFTER_FETCH = True
SKIP_IF_EXISTS = True
SLEEP_SEC = 1.2
TIMEOUT = 30
MAX_RETRIES = 5

API_KEY = os.getenv("COINGECKO_API_KEY")
BASE_URL = "https://pro-api.coingecko.com/api/v3"

headers = {
    "x-cg-pro-api-key": API_KEY
}

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("data/processed", exist_ok=True)

# =========================
# 유틸
# =========================
def to_unix_ts(date_str: str, end_of_day: bool = False) -> int:
    ts = pd.Timestamp(date_str)
    if end_of_day:
        ts = ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return int(ts.timestamp())

FROM_TS = to_unix_ts(START_DATE, end_of_day=False)
TO_TS = to_unix_ts(END_DATE, end_of_day=True)


def detect_id_column(df: pd.DataFrame) -> str:
    candidates = ["id", "coin_id", "coingecko_id"]
    for col in candidates:
        if col in df.columns:
            return col
    raise ValueError(f"코인 id 컬럼을 찾을 수 없음. 현재 컬럼: {df.columns.tolist()}")


def safe_request(url: str, params: dict):
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)

            if resp.status_code == 429:
                wait_sec = min(10 * attempt, 60)
                print(f"[429] rate limit. {wait_sec}초 대기 후 재시도")
                time.sleep(wait_sec)
                continue

            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            last_error = e
            wait_sec = min(3 * attempt, 20)
            print(f"[retry {attempt}/{MAX_RETRIES}] {e}")
            time.sleep(wait_sec)

    raise last_error


def build_market_df(coin_id: str, raw: dict) -> pd.DataFrame:
    prices = raw.get("prices", [])
    market_caps = raw.get("market_caps", [])
    total_volumes = raw.get("total_volumes", [])

    df_price = pd.DataFrame(prices, columns=["timestamp", "price"])
    df_mcap = pd.DataFrame(market_caps, columns=["timestamp", "market_cap"])
    df_vol = pd.DataFrame(total_volumes, columns=["timestamp", "total_volume"])

    if df_price.empty and df_mcap.empty and df_vol.empty:
        return pd.DataFrame()

    df = df_price.merge(df_mcap, on="timestamp", how="outer")
    df = df.merge(df_vol, on="timestamp", how="outer")

    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.date
    df = df.sort_values("timestamp").reset_index(drop=True)

    # 같은 날짜에 여러 행이 있을 수 있으니 날짜 단위로 마지막 값 유지
    df = (
        df.groupby("date", as_index=False)
        .agg({
            "timestamp": "max",
            "price": "last",
            "market_cap": "last",
            "total_volume": "last"
        })
        .sort_values("date")
        .reset_index(drop=True)
    )

    df["coin_id"] = coin_id
    cols = ["date", "coin_id", "price", "market_cap", "total_volume"]
    return df[cols]


def fetch_coin_history(coin_id: str) -> pd.DataFrame:
    url = f"{BASE_URL}/coins/{coin_id}/market_chart/range"
    params = {
        "vs_currency": "usd",
        "from": FROM_TS,
        "to": TO_TS
    }
    raw = safe_request(url, params)
    return build_market_df(coin_id, raw)


# =========================
# 메인
# =========================
universe = pd.read_csv(INPUT_PATH)
id_col = detect_id_column(universe)

failed = []
saved_files = []

print(f"input coins: {len(universe)}")
print(f"id column: {id_col}")
print(f"period: {START_DATE} ~ {END_DATE}")

for i, row in universe.iterrows():
    coin_id = str(row[id_col]).strip()
    out_path = os.path.join(OUTPUT_DIR, f"{coin_id}.csv")

    print(f"[{i+1}/{len(universe)}] {coin_id}")

    if SKIP_IF_EXISTS and os.path.exists(out_path):
        print("  -> already exists, skip")
        saved_files.append(out_path)
        continue

    try:
        df_hist = fetch_coin_history(coin_id)

        if df_hist.empty:
            print("  -> empty data")
            failed.append({
                "coin_id": coin_id,
                "reason": "empty_data"
            })
            continue

        if SAVE_PER_COIN:
            df_hist.to_csv(out_path, index=False, encoding="utf-8-sig")
            saved_files.append(out_path)
            print(f"  -> saved: {out_path} ({len(df_hist)} rows)")

        time.sleep(SLEEP_SEC)

    except Exception as e:
        print(f"  -> failed: {e}")
        failed.append({
            "coin_id": coin_id,
            "reason": str(e)
        })

# 실패 로그 저장
if failed:
    df_failed = pd.DataFrame(failed)
    df_failed.to_csv(FAILED_LOG_PATH, index=False, encoding="utf-8-sig")
    print(f"failed log saved: {FAILED_LOG_PATH}")
else:
    print("failed: 0")

# 전체 병합
if MERGE_ALL_AFTER_FETCH:
    files = [
        os.path.join(OUTPUT_DIR, f)
        for f in os.listdir(OUTPUT_DIR)
        if f.endswith(".csv")
    ]
    files = sorted(files)

    merged = []
    for f in files:
        try:
            temp = pd.read_csv(f)
            merged.append(temp)
        except Exception as e:
            print(f"merge skip: {f} / {e}")

    if merged:
        df_all = pd.concat(merged, ignore_index=True)
        df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
        df_all = df_all.sort_values(["coin_id", "date"]).reset_index(drop=True)
        df_all.to_csv(MERGED_OUTPUT_PATH, index=False, encoding="utf-8-sig")
        print(f"merged saved: {MERGED_OUTPUT_PATH} ({len(df_all)} rows)")
    else:
        print("병합할 파일이 없음")