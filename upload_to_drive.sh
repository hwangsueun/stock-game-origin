#!/bin/bash
# IISE CD 데이터 자동 분류 업로드 스크립트
# 실행: bash upload_to_drive.sh

BASE="$(cd "$(dirname "$0")" && pwd)"
REMOTE="gdrive"

# ── Drive 폴더 ID ─────────────────────────────────────────────────────────
RAW_MARKET_INDICATOR="19MO59UUKULHBvz_A8FSphQXlQarq1kaE"
RAW_BOND_UNIVERSE="1UgLHLRwbNFbsEX3sCz-zerP0y4iLp98H"
RAW_MARKET_EVENT="1gTQeKWziLtFGvzmpyF1xO_6lo7pXaoVR"
RAW_CRYPTO="14wH8fH7Y8uZ8EpOjw_pAH0XY99FCvEr8"
PROCESSED_CRYPTO="1PsHXdq8XyjQkWumfjlnP1TxVLTq7hyqI"
PROCESSED_MARKET_INDICATOR="16tPG2h8pyOw9BG1DN3yKQ__woK1vek70"
INTERIM_NEWS_GEN="1TlNvqm7Z6PK61jWvhRzKgZFEDD0TC3HT"
INTERIM_NPC="1lQbBNmt5V_xZp9Cz7D0_0Sn5oPRMUv3X"
INTERIM_PIPELINE="1ck39OVPz-UtaA55okmse20zaEw48Zt01"
DONE="1SA9vXTXG6WgSRPCAemgYWMHKq8TJyr-G"

# ── 업로드 함수 ───────────────────────────────────────────────────────────
upload() {
  local label="$1"
  local src="$2"
  local folder_id="$3"

  if [ ! -e "$src" ]; then
    echo "⚠️  없음: $src (건너뜀)"
    return
  fi

  echo ""
  echo "📤 [$label]"
  echo "   $src → Drive:$folder_id"

  rclone copy "$src" \
    "${REMOTE}:" \
    --drive-root-folder-id="$folder_id" \
    --progress \
    --transfers 2 \
    --checkers 4 \
    --buffer-size 8M \
    --exclude ".DS_Store" \
    --exclude ".env" \
    --exclude "*.pyc" \
    --exclude "__pycache__/**"
}

echo "========================================"
echo " IISE CD → Google Drive 자동 분류 업로드"
echo "========================================"

# ── raw/ ─────────────────────────────────────────────────────────────────
upload "raw / bond_universe" \
  "$BASE/bond_universe/data" \
  "$RAW_BOND_UNIVERSE"

upload "raw / market_event" \
  "$BASE/data/raw/market_event" \
  "$RAW_MARKET_EVENT"

upload "raw / market_indicator" \
  "$BASE/market_indicator/data/raw" \
  "$RAW_MARKET_INDICATOR"

upload "raw / crypto_universe (coin_history 1268개, 시간 걸림)" \
  "$BASE/crypto_universe/data/raw/coin_history" \
  "$RAW_CRYPTO"

upload "raw / coin_history_pre2014" \
  "$BASE/data/raw/coin_history_pre2014" \
  "$RAW_CRYPTO"

# ── processed/ ───────────────────────────────────────────────────────────
upload "processed / crypto_universe" \
  "$BASE/crypto_universe/data/processed" \
  "$PROCESSED_CRYPTO"

upload "processed / crypto_universe (pre2014)" \
  "$BASE/data/processed" \
  "$PROCESSED_CRYPTO"

upload "processed / market_indicator" \
  "$BASE/market_indicator/data/processed" \
  "$PROCESSED_MARKET_INDICATOR"

# ── interim/ ─────────────────────────────────────────────────────────────
upload "interim / news_generator" \
  "$BASE/news_generator/data/interim" \
  "$INTERIM_NEWS_GEN"

upload "interim / news_generator (processed)" \
  "$BASE/news_generator/data/processed" \
  "$INTERIM_NEWS_GEN"

upload "interim / npc_generator" \
  "$BASE/npc_generator/data" \
  "$INTERIM_NPC"

upload "interim / news_pipeline (data/interim)" \
  "$BASE/data/interim" \
  "$INTERIM_PIPELINE"

# ── done/ ────────────────────────────────────────────────────────────────
upload "done / llm_generated_news_2018" \
  "$BASE/demo/llm_generated_news_2018.csv" \
  "$DONE"

echo ""
echo "========================================"
echo "✅ 업로드 완료!"
echo "========================================"
