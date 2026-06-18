import os
import pandas as pd

HISTORY_PATH = "data/processed/coin_history_all.csv"
UNIVERSE_PATH = "data/processed/coin_universe_selected.csv"

OUTPUT_METADATA_PATH = "data/processed/coin_listing_metadata.csv"
OUTPUT_UNIVERSE_ENRICHED_PATH = "data/processed/coin_universe_enriched.csv"

os.makedirs("data/processed", exist_ok=True)


def detect_id_column(df: pd.DataFrame, candidates: list[str], df_name: str) -> str:
    for col in candidates:
        if col in df.columns:
            return col
    raise ValueError(f"{df_name}에서 id 컬럼을 찾을 수 없음. 현재 컬럼: {list(df.columns)}")


# =========================
# 1. 데이터 로드
# =========================
history = pd.read_csv(HISTORY_PATH)
universe = pd.read_csv(UNIVERSE_PATH)

history_id_col = detect_id_column(history, ["coin_id", "id", "coingecko_id"], "history")
universe_id_col = detect_id_column(universe, ["id", "coin_id", "coingecko_id"], "universe")

required_history_cols = ["date", history_id_col, "price", "market_cap", "total_volume"]
missing_history_cols = [col for col in required_history_cols if col not in history.columns]
if missing_history_cols:
    raise ValueError(
        f"history 파일 필수 컬럼 누락: {missing_history_cols} / 현재 컬럼: {list(history.columns)}"
    )

# =========================
# 2. 타입 정리
# =========================
history[history_id_col] = history[history_id_col].astype(str).str.strip()
universe[universe_id_col] = universe[universe_id_col].astype(str).str.strip()

history["date"] = pd.to_datetime(history["date"], errors="coerce")
history["price"] = pd.to_numeric(history["price"], errors="coerce")
history["market_cap"] = pd.to_numeric(history["market_cap"], errors="coerce")
history["total_volume"] = pd.to_numeric(history["total_volume"], errors="coerce")

history = history.dropna(subset=[history_id_col, "date"]).copy()
history = history.sort_values([history_id_col, "date"]).reset_index(drop=True)

selected_ids = set(universe[universe_id_col].dropna().astype(str).str.strip())

# =========================
# 3. selected 코인만 필터링
# =========================
history = history[history[history_id_col].isin(selected_ids)].copy()

# price, market_cap, total_volume 중 하나라도 있으면 유효
valid_mask = (
    history["price"].notna() |
    history["market_cap"].notna() |
    history["total_volume"].notna()
)
valid_history = history.loc[valid_mask].copy()

if valid_history.empty:
    raise ValueError("selected universe 기준 유효한 시계열 데이터가 없음")

coin_ids = sorted(valid_history[history_id_col].unique().tolist())
print(f"selected coins in universe: {len(selected_ids)}")
print(f"selected coins with valid history: {len(coin_ids)}")

# =========================
# 4. coin_id별 listing metadata 생성
# =========================
summary_rows = []

for i, coin_id in enumerate(coin_ids, start=1):
    coin_df = valid_history[valid_history[history_id_col] == coin_id].copy()
    coin_df = coin_df.sort_values("date").reset_index(drop=True)

    if coin_df.empty:
        print(f"[{i}/{len(coin_ids)}] {coin_id} -> empty, skip")
        continue

    first_date = coin_df["date"].min()
    last_date = coin_df["date"].max()
    available_days = int(coin_df["date"].nunique())

    price_non_na = coin_df["price"].dropna()
    market_cap_non_na = coin_df["market_cap"].dropna()
    volume_non_na = coin_df["total_volume"].dropna()

    first_price = float(price_non_na.iloc[0]) if not price_non_na.empty else None
    last_price = float(price_non_na.iloc[-1]) if not price_non_na.empty else None

    max_market_cap = float(market_cap_non_na.max()) if not market_cap_non_na.empty else None
    mean_market_cap = float(market_cap_non_na.mean()) if not market_cap_non_na.empty else None

    max_total_volume = float(volume_non_na.max()) if not volume_non_na.empty else None
    mean_total_volume = float(volume_non_na.mean()) if not volume_non_na.empty else None

    summary_rows.append({
        "coin_id": coin_id,
        "first_date": first_date.strftime("%Y-%m-%d"),
        "last_date": last_date.strftime("%Y-%m-%d"),
        "available_days": available_days,
        "listing_year": int(first_date.year),
        "delisting_year": int(last_date.year),
        "listed_before_2024": bool(first_date <= pd.Timestamp("2023-12-31")),
        "survived_to_2023_end": bool(last_date >= pd.Timestamp("2023-12-31")),
        "first_price": first_price,
        "last_price": last_price,
        "max_market_cap": max_market_cap,
        "mean_market_cap": mean_market_cap,
        "max_total_volume": max_total_volume,
        "mean_total_volume": mean_total_volume
    })

    print(
        f"[{i}/{len(coin_ids)}] {coin_id} -> "
        f"{first_date.date()} ~ {last_date.date()} / {available_days} days"
    )

summary_df = pd.DataFrame(summary_rows)

if summary_df.empty:
    raise ValueError("요약 결과가 비어 있음")

summary_df["first_date"] = pd.to_datetime(summary_df["first_date"], errors="coerce")
summary_df["last_date"] = pd.to_datetime(summary_df["last_date"], errors="coerce")

summary_df = summary_df.sort_values(
    by=["first_date", "available_days", "max_market_cap"],
    ascending=[True, False, False]
).reset_index(drop=True)

summary_df["first_date"] = summary_df["first_date"].dt.strftime("%Y-%m-%d")
summary_df["last_date"] = summary_df["last_date"].dt.strftime("%Y-%m-%d")

summary_df.to_csv(OUTPUT_METADATA_PATH, index=False, encoding="utf-8-sig")

# =========================
# 5. universe에 metadata 붙여서 enriched 파일 저장
# =========================
universe_enriched = universe.copy()

if universe_id_col != "coin_id":
    universe_enriched = universe_enriched.rename(columns={universe_id_col: "coin_id"})

universe_enriched["coin_id"] = universe_enriched["coin_id"].astype(str).str.strip()

universe_enriched = universe_enriched.merge(
    summary_df,
    on="coin_id",
    how="left"
)

universe_enriched.to_csv(OUTPUT_UNIVERSE_ENRICHED_PATH, index=False, encoding="utf-8-sig")

print()
print(f"saved metadata: {OUTPUT_METADATA_PATH}")
print(f"saved enriched universe: {OUTPUT_UNIVERSE_ENRICHED_PATH}")
print(f"coins summarized: {len(summary_df)}")
print(summary_df.head(10))