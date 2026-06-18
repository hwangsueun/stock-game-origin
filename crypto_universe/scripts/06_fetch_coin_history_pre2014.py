import os
import time
import requests
import pandas as pd

# =========================
# 설정
# =========================
OUTPUT_DIR = "data/raw/coin_history_pre2014"
MERGED_OUTPUT_PATH = "data/processed/coin_history_pre2014_all.csv"
FAILED_LOG_PATH = "data/processed/coin_history_pre2014_failed.csv"

# 2014년부터는 CoinGecko 데이터가 있으므로 2013-12-31까지만 수집
END_DATE = "2013-12-31"

SLEEP_SEC = 1.5
TIMEOUT = 30
MAX_RETRIES = 5
MAX_LIMIT = 2000  # CryptoCompare 1회 최대 반환 행 수

SAVE_PER_COIN = True
MERGE_ALL_AFTER_FETCH = True
SKIP_IF_EXISTS = True

BASE_URL = "https://min-api.cryptocompare.com/data/v2/histoday"

# =========================
# 코인 목록
# coin_id: (fsym, 거래 시작 추정일)
# CryptoCompare는 심볼 기반 (BTC, LTC 등)
# =========================
COIN_MAP = {
    "bitcoin":  ("BTC", "2010-07-01"),  # Mt. Gox 오픈 시점
    "litecoin": ("LTC", "2011-10-01"),  # LTC 출시 시점
}

END_TS = int(pd.Timestamp(END_DATE).timestamp())

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("data/processed", exist_ok=True)


# =========================
# 유틸
# =========================
def safe_request(params: dict) -> dict:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=TIMEOUT)
            if resp.status_code == 429:
                wait = min(10 * attempt, 60)
                print(f"  [429] rate limit. {wait}초 대기 후 재시도")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            wait = min(3 * attempt, 20)
            print(f"  [retry {attempt}/{MAX_RETRIES}] {e}")
            time.sleep(wait)
    raise last_error


def fetch_histoday_full(fsym: str, start_date: str) -> pd.DataFrame:
    """
    CryptoCompare histoday는 toTs 기준으로 과거 방향으로 최대 2000행 반환.
    toTs를 이동시키며 start_date까지 페이지네이션.
    """
    start_ts = int(pd.Timestamp(start_date).timestamp())
    to_ts = END_TS
    all_rows = []

    while True:
        params = {
            "fsym": fsym,
            "tsym": "USD",
            "limit": MAX_LIMIT,
            "toTs": to_ts,
        }
        data = safe_request(params)

        if data.get("Response") == "Error":
            raise ValueError(f"CryptoCompare 오류: {data.get('Message')}")

        rows = data.get("Data", {}).get("Data", [])
        if not rows:
            break

        # 유효한 행만 (price=0인 행 제거 — 거래 시작 전 패딩 데이터)
        rows = [r for r in rows if r.get("close", 0) > 0]
        if not rows:
            break

        all_rows = rows + all_rows  # 과거 → 현재 순서로 앞에 붙임

        oldest_ts = rows[0]["time"]

        # 시작일보다 오래된 데이터까지 도달하면 종료
        if oldest_ts <= start_ts:
            break

        # 다음 페이지: oldest_ts 이전으로 이동
        to_ts = oldest_ts - 1

        if len(rows) < MAX_LIMIT:
            break

        time.sleep(SLEEP_SEC)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)

    df["date"] = pd.to_datetime(df["time"], unit="s").dt.date
    df["price"] = df["close"].astype(float)
    df["total_volume"] = df["volumeto"].astype(float)  # USD 기준 거래량

    # start_date ~ END_DATE 범위만 필터
    df = df[
        (df["time"] >= start_ts) &
        (df["time"] <= END_TS)
    ].copy()

    # 날짜 중복 제거
    df = (
        df.sort_values("time")
          .groupby("date", as_index=False)
          .agg({"price": "last", "total_volume": "last"})
          .sort_values("date")
          .reset_index(drop=True)
    )

    return df


# =========================
# 메인
# =========================
failed = []
saved_files = []

print(f"수집 코인: {list(COIN_MAP.keys())}")
print(f"수집 종료일: {END_DATE} (2014년부터는 CoinGecko 데이터 사용)")
print()

for i, (coin_id, (fsym, start_date)) in enumerate(COIN_MAP.items(), start=1):
    out_path = os.path.join(OUTPUT_DIR, f"{coin_id}.csv")
    print(f"[{i}/{len(COIN_MAP)}] {coin_id} ({fsym}/USD) | 시작: {start_date}")

    if SKIP_IF_EXISTS and os.path.exists(out_path):
        print("  -> 이미 존재, skip")
        saved_files.append(out_path)
        continue

    try:
        df = fetch_histoday_full(fsym, start_date)

        if df.empty:
            print("  -> 데이터 없음")
            failed.append({"coin_id": coin_id, "fsym": fsym, "reason": "empty_data"})
            continue

        df["coin_id"] = coin_id
        df = df[["date", "coin_id", "price", "total_volume"]]

        if SAVE_PER_COIN:
            df.to_csv(out_path, index=False, encoding="utf-8-sig")
            saved_files.append(out_path)
            print(f"  -> 저장 완료: {out_path} ({len(df)}행, {df['date'].min()} ~ {df['date'].max()})")

        time.sleep(SLEEP_SEC)

    except Exception as e:
        print(f"  -> 실패: {e}")
        failed.append({"coin_id": coin_id, "fsym": fsym, "reason": str(e)})

# 실패 로그
if failed:
    pd.DataFrame(failed).to_csv(FAILED_LOG_PATH, index=False, encoding="utf-8-sig")
    print(f"\n실패 로그 저장: {FAILED_LOG_PATH}")
    for f in failed:
        print(f"  - {f['coin_id']}: {f['reason']}")
else:
    print("\n실패: 0건")

# 전체 병합
if MERGE_ALL_AFTER_FETCH and saved_files:
    merged = []
    for f in sorted(saved_files):
        try:
            merged.append(pd.read_csv(f))
        except Exception as e:
            print(f"병합 skip: {f} / {e}")

    if merged:
        df_all = pd.concat(merged, ignore_index=True)
        df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
        df_all = df_all.sort_values(["coin_id", "date"]).reset_index(drop=True)
        df_all.to_csv(MERGED_OUTPUT_PATH, index=False, encoding="utf-8-sig")
        print(f"\n병합 저장: {MERGED_OUTPUT_PATH} ({len(df_all)}행)")