#!/bin/bash
set -e

# 1. IISE-CD 상위 폴더 생성
mkdir -p ~/Desktop/IISE-CD

# 2. stock-game-sample을 먼저 분리
mv ~/Desktop/IISE\ CD/stock-game-sample ~/Desktop/IISE-CD/stock-game

# 3. IISE CD 전체를 data-pipeline으로 이동
mv ~/Desktop/IISE\ CD ~/Desktop/IISE-CD/data-pipeline

echo "완료!"
echo ""
echo "구조:"
ls ~/Desktop/IISE-CD/
