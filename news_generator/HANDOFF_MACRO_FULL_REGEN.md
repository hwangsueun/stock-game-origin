# HANDOFF — 거시뉴스 전기간 재생성 (정책·법 레이어 반영)

작성: 2026-06-24. 이 문서는 04:00 무인 세션이 읽고 **남은 작업(전기간 재생성)**을 수행하기 위한 것.
작업 루트: `/Users/hgs/Desktop/IISE-CD/data-pipeline/news_generator/`. 모든 명령은 이 디렉터리에서.
`.env`에 `OPENAI_API_KEY`·`ASSEMBLY_API_KEY` 있음. `set -a; . ./.env; set +a`로 로드.

## 현재 상태 (전부 완료·검증됨 — 다시 만들 필요 없음)
- 국내 정책·법·정치 발표 레이어 수집 완료: 원장 `data/raw/official_policy_legal_releases_2013_2023.csv`(5,072건: 선거7·국회3,804·헌재2·공정위1,001·금감원258).
- 거시 합본 캘린더 `data/raw/official_release_calendar_combined.csv`(1,737건, 일별 정책·법 캡=2 적용). **이미 최신. 재수집/재머지 불필요.**
- 생성 레이어 수정 완료(pr04 카테고리 동사·앵글, pr05 카테고리 설명·조사·회사명 규칙, 캘린더 오버레이 프리뷰/리뷰+결과제거, beginner_explanation 결정론 폴백). 샘플 다수 검증: 정책·법 결함 0, 무작위 30일 28/30(실패 2건은 무조작 게이트가 LLM 환각숫자 차단 — 정상).

## 남은 작업 = 전기간(2,865 거래일) 거시뉴스 재생성

### 1. 입력 체인 재빌드 (결정론·무료·빠름, ~1분)
```bash
OUT=data/interim/macro_news_policy_legal_regen
mkdir -p $OUT
# (1) pr04: 합본 캘린더로 거시 입력
python3 scripts/processors/pr04_build_macro_news_generation_input.py \
  --input-path data/processed/macro_signal_daily_cleaned.csv \
  --official-release-calendar data/raw/official_release_calendar_combined.csv \
  --output-path $OUT/with_releases.jsonl --report-path $OUT/with_releases_report.csv
# (2) 캘린더 오버레이(프리뷰/리뷰)
python3 scripts/processors/build_macro_calendar_overlay.py \
  --input-jsonl $OUT/with_releases.jsonl --output-jsonl $OUT/with_calendar.jsonl
# (3) SEP(FOMC 전망) 오버레이
python3 scripts/processors/build_sep_projection_overlay.py \
  --input-jsonl $OUT/with_calendar.jsonl \
  --sep-csv data/raw/fomc_sep_projections_2013_2023.csv \
  --output-jsonl $OUT/with_sep.jsonl
# (4) WEO(IMF 전망) 오버레이 → 최종 입력
python3 scripts/processors/build_weo_projection_overlay.py \
  --input-jsonl $OUT/with_sep.jsonl \
  --weo-csv data/raw/imf_weo_projections.csv \
  --output-jsonl $OUT/with_outlook.jsonl
wc -l $OUT/with_outlook.jsonl   # 2865 기대
```

### 2. pr05 생성 — **연도별 청크 + 재개**(gpt-4o, ~2,865일, 비용 큼)
연도별로 나눠 각자 CSV로 저장(중단/재개 안전). 이미 있는 연도 CSV는 건너뜀.
```bash
set -a; . ./.env; set +a
for Y in 2013 2014 2015 2016 2017 2018 2019 2020 2021 2022 2023; do
  CSV=$OUT/gen_${Y}.csv
  if [ -f "$CSV" ]; then echo "skip $Y (exists)"; continue; fi
  echo "=== generating $Y ==="
  python3 scripts/processors/pr05_generate_macro_news_from_llm.py \
    --input-jsonl $OUT/with_outlook.jsonl \
    --output-csv $CSV \
    --model gpt-4o --max-news-per-day 5 \
    --start-date ${Y}-01-01 --end-date ${Y}-12-31 \
    > $OUT/gen_${Y}.log 2>&1
  echo "done $Y rc=$?"
done
```
**⚠ 순차 실행 필수(병렬 금지):** OpenAI org TPM 한도가 30,000 토큰/분이라 pr05를 병렬로 돌리면 전부 429 rate_limit으로 실패한다(실측: 4병렬 시 40일 중 13일 429 실패). 위 for문처럼 **한 번에 하나씩** 순차로만 돌릴 것. pr05 내장 재시도/백오프가 산발 429를 흡수한다.

**비용/한도 안전수칙:**
- 과거 `billing_hard_limit_reached`로 중단된 이력 있음. 각 연도 로그(`gen_${Y}.log`) 끝을 확인해 `billing`/`rate_limit`/`insufficient_quota` 에러가 보이면 **즉시 중단하고** 어디까지 됐는지 기록만 남길 것(이미 생성된 연도 CSV는 보존). 재개는 다음 실행에서 자동(존재 연도 skip).
- 하루 단위 실패("evidence 밖 숫자", 길이 등)는 정상(무조작 게이트). fail_log `data/processed/llm_generated_macro_news_fail_log.csv`에 누적됨 — 전체 중단 사유 아님.

### 3. 병합 + 감사
```bash
python3 - <<'PY'
import csv, glob
rows=[]; cols=None
for p in sorted(glob.glob("data/interim/macro_news_policy_legal_regen/gen_20*.csv")):
    rs=list(csv.DictReader(open(p,encoding="utf-8-sig")))
    if rs and cols is None: cols=list(rs[0].keys())
    rows+=rs
import os
out="data/interim/macro_news_policy_legal_regen/generated_macro_news_all.csv"
with open(out,"w",encoding="utf-8-sig",newline="") as f:
    w=csv.DictWriter(f,fieldnames=cols); w.writeheader(); w.writerows(rows)
print("merged",len(rows),"->",out)
PY
# 감사(입력 대비 커버리지·게이트)
python3 scripts/processors/audit_generated_macro_news.py \
  --input-jsonl $OUT/with_outlook.jsonl \
  --generated-csv $OUT/generated_macro_news_all.csv | tail -20
```

### 4. 실패일 재생성(선택, 비용 여유 시)
fail_log의 날짜를 모아 `--start-date/--end-date`로 1일씩 재생성하면 대부분 통과(LLM 변동). 비용 한도 우선.

## 성공 기준
- `gen_2013..2023.csv` 모두 존재, 병합 CSV 생성.
- audit `status=PASS`(또는 실패 일부는 fail_log에 기록되고 사유가 무조작 게이트인지 확인).
- 정책·법 기사가 정상 포함(헌재 탄핵 2017-03-13, 대선/총선/지선, 공정위/금감원 제재, 주요 입법). 헤드라인·본문에 `(주)`·HTML엔티티·`을(를)`류 플레이스홀더 없어야 함.

## 진행 로그
- 각 단계 결과를 이 파일 맨 아래 "## 실행 기록"에 append할 것(시각·연도·rc·이슈·중단지점). 사용자가 아침에 읽음.
- 원격 조종/로그 확인 가능(사용자 수면 중). 장시간 작업은 백그라운드로 돌리고 주기적으로 로그 tail.

## 실행 기록
(여기에 04:00 세션이 진행상황을 append)
