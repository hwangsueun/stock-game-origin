#!/bin/bash
# v4.0 배치 다운로드 + 오딧 실행
# 이 파일을 더블클릭하면 Terminal에서 자동 실행됩니다.

set -e
cd "$(dirname "$0")"

# .env에서 API 키 로드
ENV_FILE="$(dirname "$0")/../.env"
if [ -f "$ENV_FILE" ]; then
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

if [ -z "$OPENAI_API_KEY" ]; then
    echo "[error] OPENAI_API_KEY가 설정되지 않았습니다."
    exit 1
fi

echo "[start] v4.0 다운로드 + 오딧"
python3 processors/run_v4_0_download_and_audit.py "$@"

echo ""
echo "[done] 완료. 터미널을 닫아도 됩니다."
read -p "Press Enter to close..."
