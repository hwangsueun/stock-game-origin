import os
import math
import time
from pathlib import Path

import requests
import pandas as pd
from dotenv import load_dotenv

BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
STAT_CODE = "817Y002"
CYCLE = "D"
START_DATE = "20140101"
END_DATE = "20231231"
PAGE_SIZE = 1000
TIMEOUT = 20
SLEEP_SEC = 0.15

SERIES = {
    "KTB_3Y": {
        "item_code": "010200000",
        "name_kr": "국고채(3년)",
    },
    "KTB_10Y": {
        "item_code": "010210000",
        "name_kr": "국고채(10년)",
    },
}


def build_url(api_key: str, start_no: int, end_no: int, item_code: str) -> str:
    return (
        f"{BASE_URL}/{api_key}/json/kr/"
        f"{start_no}/{end_no}/{STAT_CODE}/{CYCLE}/{START_DATE}/{END_DATE}/{item_code}"
    )


def request_json(url: str) -> dict:
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def parse_rows(payload: dict):
    if "StatisticSearch" not in payload:
        return []
    return payload["StatisticSearch"].get("row", [])


def get_total_count(payload: dict) -> int:
    if "StatisticSearch" not in payload:
        return 0
    return int(payload["StatisticSearch"].get("list_total_count", 0))


def fetch_one_series(api_key: str, series_name: str, item_code: str) -> pd.DataFrame:
    first_url = build_url(api_key, 1, PAGE_SIZE, item_code)
    first_payload = request_json(first_url)

    total_count = get_total_count(first_payload)
    first_rows = parse_rows(first_payload)

    if total_count == 0 or not first_rows:
        raise RuntimeError(f"{series_name}: 데이터가 없습니다.")

    all_rows = list(first_rows)
    total_pages = math.ceil(total_count / PAGE_SIZE)

    for page in range(2, total_pages + 1):
        start_no = (page - 1) * PAGE_SIZE + 1
        end_no = page * PAGE_SIZE

        url = build_url(api_key, start_no, end_no, item_code)
        payload = request_json(url)
        rows = parse_rows(payload)
        all_rows.extend(rows)
        time.sleep(SLEEP_SEC)

    df = pd.DataFrame(all_rows)

    df["date"] = pd.to_datetime(df["TIME"], format="%Y%m%d", errors="coerce")
    df["yield_pct"] = pd.to_numeric(df["DATA_VALUE"], errors="coerce")
    df["series"] = series_name

    df = (
        df[["date", "series", "yield_pct"]]
        .dropna(subset=["date", "yield_pct"])
        .drop_duplicates(subset=["date", "series"])
        .sort_values("date")
        .reset_index(drop=True)
    )

    return df


def save_outputs(df_long: pd.DataFrame, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    df_wide = df_long.pivot(index="date", columns="series", values="yield_pct").reset_index()
    df_wide = df_wide.rename(columns={
        "KTB_3Y": "ktb_3y",
        "KTB_10Y": "ktb_10y",
    })

    df_spread = df_wide.copy()
    df_spread["spread_10y_3y"] = df_spread["ktb_10y"] - df_spread["ktb_3y"]

    df_change = df_wide.copy()
    df_change["ktb_3y_chg_bp"] = df_change["ktb_3y"].diff() * 100
    df_change["ktb_10y_chg_bp"] = df_change["ktb_10y"].diff() * 100

    df_long.to_csv(output_dir / "kr_treasury_yields_long.csv", index=False, encoding="utf-8-sig")
    df_wide.to_csv(output_dir / "kr_treasury_yields_wide.csv", index=False, encoding="utf-8-sig")
    df_spread.to_csv(output_dir / "kr_treasury_yields_with_spread.csv", index=False, encoding="utf-8-sig")
    df_change.to_csv(output_dir / "kr_treasury_yields_with_changes.csv", index=False, encoding="utf-8-sig")

    df_long[df_long["series"] == "KTB_3Y"].to_csv(
        output_dir / "kr_treasury_3y.csv", index=False, encoding="utf-8-sig"
    )
    df_long[df_long["series"] == "KTB_10Y"].to_csv(
        output_dir / "kr_treasury_10y.csv", index=False, encoding="utf-8-sig"
    )

    print("[INFO] 저장 완료")
    for name in [
        "kr_treasury_yields_long.csv",
        "kr_treasury_yields_wide.csv",
        "kr_treasury_yields_with_spread.csv",
        "kr_treasury_yields_with_changes.csv",
        "kr_treasury_3y.csv",
        "kr_treasury_10y.csv",
    ]:
        print(f" - {output_dir / name}")


def main():
    load_dotenv()
    api_key = os.getenv("ECOS_API_KEY")

    if not api_key:
        raise RuntimeError("ECOS_API_KEY를 읽지 못했습니다. .env 파일 확인하세요.")

    frames = []
    for series_name, meta in SERIES.items():
        print(f"[INFO] 수집 시작: {meta['name_kr']} ({series_name})")
        df = fetch_one_series(api_key, series_name, meta["item_code"])
        print(f"[INFO] 수집 완료: {series_name}, {len(df):,}건")
        frames.append(df)

    df_long = pd.concat(frames, ignore_index=True)
    save_outputs(df_long, Path("data"))


if __name__ == "__main__":
    main()