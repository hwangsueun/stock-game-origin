import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

API_KEY = os.getenv("COINGECKO_API_KEY")
if not API_KEY:
    raise ValueError("COINGECKO_API_KEY가 .env에 없음")

BASE_URL = "https://pro-api.coingecko.com/api/v3"
HEADERS = {"x-cg-pro-api-key": API_KEY}

# 상장일부터 2023-12-31까지 평가하려면 시작 시점을 충분히 과거로 잡음
FROM_TS = int(datetime(2009, 1, 1, tzinfo=timezone.utc).timestamp())
TO_TS = int(datetime(2023, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())

INPUT_PATH = "data/raw/coins_list.csv"
OUTPUT_PATH = "data/processed/coin_marketcap_scan.csv"
ERROR_PATH = "data/logs/scan_errors.csv"

REQUEST_SLEEP = 0.12
SAVE_EVERY = 20
MAX_RETRIES = 5


def ensure_dirs():
    os.makedirs("data/processed", exist_ok=True)
    os.makedirs("data/logs", exist_ok=True)


def atomic_to_csv(df: pd.DataFrame, path: str):
    tmp_path = path + ".tmp"
    df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
    os.replace(tmp_path, path)


def fetch_market_chart_range(coin_id: str):
    url = f"{BASE_URL}/coins/{coin_id}/market_chart/range"
    params = {
        "vs_currency": "usd",
        "from": FROM_TS,
        "to": TO_TS
    }

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=60)

            if resp.status_code == 429:
                wait_sec = min(10 * attempt, 60)
                print(f"[429] {coin_id} retry in {wait_sec}s")
                time.sleep(wait_sec)
                continue

            if 500 <= resp.status_code < 600:
                wait_sec = min(5 * attempt, 30)
                print(f"[{resp.status_code}] {coin_id} retry in {wait_sec}s")
                time.sleep(wait_sec)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.RequestException as e:
            last_error = e
            wait_sec = min(5 * attempt, 30)
            print(f"[ERR] {coin_id} attempt {attempt}/{MAX_RETRIES} retry in {wait_sec}s: {e}")
            time.sleep(wait_sec)

    raise last_error if last_error else RuntimeError(f"Unknown error for {coin_id}")


def safe_date_from_ms(ms):
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def summarize_coin(coin_id: str, symbol: str, name: str):
    data = fetch_market_chart_range(coin_id)

    market_caps = data.get("market_caps", [])
    prices = data.get("prices", [])
    volumes = data.get("total_volumes", [])

    if not market_caps and not prices and not volumes:
        return {
            "id": coin_id,
            "symbol": symbol,
            "name": name,
            "first_observed_date": None,
            "last_observed_date": None,
            "max_market_cap": None,
            "max_market_cap_date": None,
            "valid_days": 0
        }

    dates = set()

    for arr in (market_caps, prices, volumes):
        for row in arr:
            if isinstance(row, list) and len(row) >= 2 and row[0] is not None:
                dates.add(safe_date_from_ms(row[0]))

    first_date = min(dates) if dates else None
    last_date = max(dates) if dates else None
    valid_days = len(dates)

    max_market_cap = None
    max_market_cap_date = None

    valid_market_caps = [
        row for row in market_caps
        if isinstance(row, list) and len(row) >= 2 and row[0] is not None and row[1] is not None
    ]

    if valid_market_caps:
        max_row = max(valid_market_caps, key=lambda x: x[1])
        max_market_cap = max_row[1]
        max_market_cap_date = safe_date_from_ms(max_row[0])

    return {
        "id": coin_id,
        "symbol": symbol,
        "name": name,
        "first_observed_date": first_date,
        "last_observed_date": last_date,
        "max_market_cap": max_market_cap,
        "max_market_cap_date": max_market_cap_date,
        "valid_days": valid_days
    }


def load_existing():
    results = []
    errors = []

    if os.path.exists(OUTPUT_PATH):
        try:
            results = pd.read_csv(OUTPUT_PATH).to_dict("records")
        except Exception as e:
            print(f"[WARN] failed to read {OUTPUT_PATH}: {e}")

    if os.path.exists(ERROR_PATH):
        try:
            errors = pd.read_csv(ERROR_PATH).to_dict("records")
        except Exception as e:
            print(f"[WARN] failed to read {ERROR_PATH}: {e}")

    return results, errors


def build_processed_ids(results, errors):
    done_ids = set()
    error_ids = set()

    for row in results:
        if "id" in row and pd.notna(row["id"]):
            done_ids.add(str(row["id"]))

    for row in errors:
        if "id" in row and pd.notna(row["id"]):
            error_ids.add(str(row["id"]))

    return done_ids, error_ids


def save_results(results, errors):
    results_df = pd.DataFrame(results)
    errors_df = pd.DataFrame(errors)

    if not results_df.empty:
        results_df = results_df.drop_duplicates(subset=["id"], keep="last")
        atomic_to_csv(results_df, OUTPUT_PATH)

    if not errors_df.empty:
        errors_df = errors_df.drop_duplicates(subset=["id"], keep="last")
        atomic_to_csv(errors_df, ERROR_PATH)


def main():
    ensure_dirs()

    coins = pd.read_csv(INPUT_PATH)
    results, errors = load_existing()

    done_ids, error_ids = build_processed_ids(results, errors)

    # 완료한 코인 + 이미 실패 로그 남긴 코인 모두 스킵
    processed_ids = done_ids | error_ids

    print(f"resume mode: success={len(done_ids)}, error={len(error_ids)}, total_skip={len(processed_ids)}")

    processed_since_save = 0

    for row in tqdm(coins.itertuples(index=False), total=len(coins)):
        coin_id = str(row.id)
        symbol = str(row.symbol)
        name = str(row.name)

        if coin_id in processed_ids:
            continue

        try:
            summary = summarize_coin(coin_id, symbol, name)
            results.append(summary)
            done_ids.add(coin_id)
            processed_ids.add(coin_id)

        except Exception as e:
            errors.append({
                "id": coin_id,
                "symbol": symbol,
                "name": name,
                "error": str(e)
            })
            error_ids.add(coin_id)
            processed_ids.add(coin_id)

        processed_since_save += 1

        if processed_since_save >= SAVE_EVERY:
            save_results(results, errors)
            print(f"[SAVE] success={len(done_ids)} error={len(error_ids)}")
            processed_since_save = 0

        time.sleep(REQUEST_SLEEP)

    save_results(results, errors)

    print("done")
    print(f"saved results: {len(done_ids)}")
    print(f"errors: {len(error_ids)}")


if __name__ == "__main__":
    main()