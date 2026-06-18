import os
import pandas as pd

UNIVERSE_PATH = "data/processed/coin_universe_selected.csv"
HISTORY_PATH = "data/processed/coin_history_all.csv"
OUTPUT_PATH = "data/processed/coin_history_selected.csv"

universe = pd.read_csv(UNIVERSE_PATH)
history = pd.read_csv(HISTORY_PATH)

# universe 쪽 id 컬럼 찾기
universe_id_col = None
for col in ["id", "coin_id", "coingecko_id"]:
    if col in universe.columns:
        universe_id_col = col
        break

if universe_id_col is None:
    raise ValueError(f"유니버스 파일에서 id 컬럼을 찾을 수 없음: {list(universe.columns)}")

# history 쪽 id 컬럼 찾기
history_id_col = None
for col in ["coin_id", "id", "coingecko_id"]:
    if col in history.columns:
        history_id_col = col
        break

if history_id_col is None:
    raise ValueError(f"히스토리 파일에서 id 컬럼을 찾을 수 없음: {list(history.columns)}")

selected_ids = set(universe[universe_id_col].astype(str).str.strip())
history[history_id_col] = history[history_id_col].astype(str).str.strip()

filtered = history[history[history_id_col].isin(selected_ids)].copy()

if "date" in filtered.columns:
    filtered["date"] = pd.to_datetime(filtered["date"], errors="coerce")
    filtered = filtered.sort_values([history_id_col, "date"]).reset_index(drop=True)
else:
    filtered = filtered.sort_values([history_id_col]).reset_index(drop=True)

os.makedirs("data/processed", exist_ok=True)
filtered.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

print(f"selected coin count: {len(selected_ids)}")
print(f"filtered row count: {len(filtered)}")
print(f"saved to: {OUTPUT_PATH}")