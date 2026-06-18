# 뉴스 생성 파이프라인 인수인계 문서

작성일: 2026-06-18  
대상: 다음 작업 세션 (Codex 또는 차기 AI)

---

## 0. 2026-06-18 후속 실행 결과

전 종목 확장 작업을 이어서 실행했다.

- `pr05d --dart-scope all` 완료: `data/interim/pr05d_stock_event_groups_v2_all/stock_event_groups.jsonl` 9,034건
- `pr05e` 완료: `data/interim/pr05e_stock_evidence_bundles_v2_all/stock_evidence_bundles.jsonl` 9,034건
- 연간 재무 팩트 완료: `data/interim/pr05f_dart_annual_financial_facts_v2_all/dart_annual_financial_facts.csv` 7개 보고서
- 상세 공시 ZIP 다운로드/팩트 추출 완료:
  - 후보 6,770건
  - 상세 팩트 추출 5,874건
  - ZIP 캐시 5,893개
  - 산출물: `data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv`
- 전 종목 브리프 완료: `data/interim/pr05f_stock_news_briefs_v3_all_stocks/stock_news_briefs.jsonl`
  - 전체 9,034건
  - ready 3,864건
  - borderline 533건
  - insufficient_specific_facts 4,637건
- v4.2 요청 파일 생성 완료:
  - 경로: `data/interim/pr06a_full_requests_v4_2_all_stocks/stock_news_sample_requests.jsonl`
  - 요청 1,634건
  - `--max-per-stock 9999 --min-write-safe-facts 1`로 생성
  - 30건 청크 55개 생성: `data/interim/pr06a_full_requests_v4_2_all_stocks/chunks_30/`
- OpenAI Batch 순차 실행은 32개 청크(960건)까지 완료 후 중단:
  - 중단 지점: 33번 청크 제출 시도
  - 오류: `billing_hard_limit_reached`
  - 병합 출력: `data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl` 960건
  - 병합 에러: `data/interim/pr06a_full_outputs_v4_2_all_stocks/errors_all.jsonl` 0건
- v4.2 오딧 스크립트 추가: `news_generator/scripts/processors/audit_v4_2_full_outputs.py`
  - 오딧 결과: 생성된 960건은 960/960 PASS
  - 전체 요청 기준 missing output 674건
  - 오딧 리포트: `data/interim/pr06a_full_outputs_v4_2_all_stocks/audit_all/audit_report.md`

다음 작업:
1. OpenAI 결제 hard limit 해제 또는 한도 상향
2. 33번 청크(라인 961)부터 이어서 Batch 실행
3. 완료 후 `python scripts/processors/pr05b_run_openai_batch_chunks.py merge-outputs ...` 재실행
4. `python scripts/processors/audit_v4_2_full_outputs.py` 재실행

주의: 기존 순차 실행기는 재개 기능이 없으므로 그대로 `run`을 다시 실행하면 1번 청크부터 재제출된다. 이어 실행하려면 33번 이후 입력만 별도 JSONL로 만들거나, `pr05b_run_openai_batch_chunks.py`에 resume 옵션을 추가해야 한다.

---

## 0-1. 데이터 관리 체계 (2026-06-18 설정)

### 코드 관리: GitHub

로컬 폴더(`~/Desktop/IISE CD`)에 git 저장소 초기화 및 원격 연결 완료.

- 원격 저장소: `https://github.com/hwangsueun/stock-game-origin`
- `.gitignore` 설정: `.env`, `venv/`, `.venv/`, `node_modules/`, `data/`, `__pycache__/` 등 제외
- **코드만 GitHub에 올리고, 데이터는 Google Drive로 분리 관리**

첫 푸쉬 명령어 (터미널에서 실행):
```bash
cd ~/Desktop/IISE\ CD
rm .git/index.lock      # 필요 시
git add .
git commit -m "Initial commit"
git push -u origin master
```

---

### 데이터 관리: Google Drive

경로: `내 드라이브 / 2026 / 캡스톤디자인 / Data /`

#### 폴더 구조

| Drive 폴더 | 내용 | 로컬 출처 |
|---|---|---|
| `Data/raw/market_indicator/` | 매크로·시장 지표 원본 (KOSPI, KTB, USD/KRW 등 40+) | `market_indicator/data/raw/` |
| `Data/raw/bond_universe/` | 국채 금리 원본 (3Y/10Y 등) | `bond_universe/data/` |
| `Data/raw/market_event/` | 주가 이벤트·매크로 이벤트 캘린더 | `data/raw/market_event/` |
| `Data/raw/crypto_universe/` | 코인 히스토리 원본 (1,268개 CSV) | `crypto_universe/data/raw/coin_history/` |
| `Data/processed/crypto_universe/` | 코인 유니버스 선별·가공본 | `crypto_universe/data/processed/` |
| `Data/processed/market_indicator/` | 매크로·섹터 컨텍스트 가공본 | `market_indicator/data/processed/` |
| `Data/interim/news_generator/` | 뉴스 생성 파이프라인 중간 산출물 | `news_generator/data/interim·processed/` |
| `Data/interim/npc_generator/` | NPC 갤러리 분석 중간 산출물 | `npc_generator/data/` |
| `Data/interim/news_pipeline/` | pr05·pr06 파이프라인 버전별 산출물 | `data/interim/` |
| `Data/done/` | 게임에 들어갈 완성 데이터 | `stock-game-sample/src/data/gameData.json`, `demo/llm_generated_news_2018.csv` |

#### 업로드 방법 (rclone)

rclone 설정 remote 이름: `gdrive`

업로드 스크립트: `upload_to_drive.sh` (프로젝트 루트에 위치)

```bash
cd ~/Desktop/IISE\ CD
bash upload_to_drive.sh
```

스크립트가 위 분류 기준대로 자동 업로드함. crypto_universe raw(1,268개)는 시간이 걸리므로 백그라운드 실행 권장:

```bash
bash upload_to_drive.sh > upload_log.txt 2>&1 &
```

---

## 1. 프로젝트 개요

**목적**: 한국 주식 117개 종목에 대한 2013~2023년 10년치 종목별 금융 뉴스를 자동 생성한다.  
**방식**: DART 공시 + 주가 데이터 → 팩트 추출 → LLM(GPT-4o) 뉴스 생성 → 오딧 통과  
**최종 결과물**: 종목코드·날짜·뉴스 본문이 담긴 JSONL/CSV  

---

## 2. 완료된 작업 (이번 세션)

### 2-1. pr05f 보일러플레이트 필터링

`investment_detail`, `contract_detail` 팩트에서 불필요한 법적 고지문·환율 주석 등을 제거했다.

- **파일**: `news_generator/scripts/processors/pr05f_extract_dart_disclosure_detail_facts.py`
- `_BOILERPLATE_PREFIXES` (regex), `_BOILERPLATE_KEYWORDS` (list), `_is_boilerplate()` 메서드 완성
- 팩트 수: investment_detail 61→25건, contract_detail 48→8건

### 2-2. DART 상세 팩트를 뉴스 브리프에 연결

`earnings_change_reason`, `investment_detail`, `contract_detail`, `contract_item` 4종의 팩트가 `supporting_context_facts_ko`로 흐르도록 수정했다.

- **파일**: `news_generator/scripts/processors/pr05f_build_stock_news_briefs.py`
- 추가된 상수: `DART_DETAIL_CONTEXT_FACT_TYPES`
- 수정된 메서드: `_build_fact_layers()`, `_build_related_fact_groups()`, `_select_supporting_fact_texts()`
- 효과: supporting_context_facts_ko 보유 브리프 5건 → 147건

### 2-3. v4.1 샘플(35건) + 전체 414건 생성 완료

- 브리프 디렉토리: `data/interim/pr05f_stock_news_briefs_v2_8_dart_detail_context/`
- 요청 파일: `data/interim/pr06a_full_requests_v4_1/` (14개 청크 × 30건)
- **최종 출력**: `data/interim/pr06a_full_outputs_v4_1/outputs_all.jsonl` (414건)
- 오딧: **414/414 100% PASS**

### 2-4. 오딧 스크립트 버그 수정

- **파일**: `news_generator/scripts/processors/run_v4_1_full_chunk_download_and_audit.py`
- ISO 날짜(`2022-12-31`) → 한국어(`2022년12월31일`) 변환 추가 (`_ISO_DATE_RE`)
- 한국식 약식 날짜(`'22.4.4일`) → 한국어 변환 추가 (`_KR_ABBREV_DATE_RE`)

### 2-5. 품질 개선 및 225건 재생성

문제: 단순 공시 2줄 반복 패턴(224건, 54%), 중국어 사명 노출(2건)

**프롬프트 수정** (`pr06a_build_stock_news_sample_requests.py` `_system_prompt()`):
1. CJK 문자 처리 지시: "중국어/일본어 한자가 포함된 팩트는 영문 표기 또는 '현지 자회사'로 대체"
2. Earnings 스타일 가이드: 2줄 작성 시 동일 동사 반복 금지, 2번째 줄은 `기록했다`/`집계됐다` 사용

**재생성 결과** (225건 배치):
- 재생성 요청: `data/interim/pr06a_regen_v4_1_pattern_fix/`
- 단순 공시 2줄 반복: 54% → 21% (86건, equity_investment 등 구조적 케이스만 잔류)
- CJK 노출: 0건
- `outputs_all.jsonl`에 225건 교체 병합 완료

---

## 3. 현재 상태 (세션 종료 시점)

| 항목 | 상태 |
|---|---|
| 처리 종목 | 10개 (기아·SK하이닉스·DB하이텍·대한항공·LG·삼성전기·S-Oil·롯데케미칼·HMM·금호석유화학) |
| 브리프 | 887건 총 / 419건 ready |
| 생성 뉴스 | 414건 (100% 오딧 PASS) |
| stocklist 전체 | 117개 종목 (`/Users/hgs/Desktop/IISE CD/stocklist.txt`) |
| 미처리 종목 | 107개 — 전 종목 확장 필요 |

---

## 4. 다음에 해야 할 일 (전 종목 확장)

### 핵심 병목

현재 `pr05d`가 `--dart-scope stock_event_universe`(기본값)로 실행되어 stock_event_calendar에 등록된 10개 종목만 처리한다.  
`--dart-scope all`로 바꾸면 DART 데이터가 있는 117개 전 종목을 처리할 수 있다.

---

### Step 1: pr05d 재실행 (전 종목 이벤트 그룹 빌드)

```bash
cd "/Users/hgs/Desktop/IISE CD/news_generator"

python scripts/processors/pr05d_build_stock_event_groups_v4_fixed.py \
  --stock-event-annotations "data/interim/pr05c_stock_event_context/stock_event_context_annotations.csv" \
  --dart-evidence "npc_generator/data/processed/dart_event_evidence/dart_event_evidence_detail.csv" \
  --output-dir "data/interim/pr05d_stock_event_groups_v2_all" \
  --start-date 2013-01-01 \
  --end-date 2023-12-31 \
  --dart-scope all \
  --dart-include-families "earnings,guidance,dividend,contract,investment,treasury_stock,asset_transaction,equity_investment,capital_financing,business_transfer,trading_status,listing_risk,legal_regulatory,management_governance,major_management_matter,product_supply_chain,other_company_event"
```

**예상 출력**: 현재 887 이벤트 그룹의 ~10배 (약 9,000건)  
**소요 시간**: LLM 없음, 수 분

---

### Step 2: pr05e 재실행 (증거 번들 빌드)

```bash
python scripts/processors/pr05e_build_stock_evidence_bundles.py \
  --event-groups-jsonl "data/interim/pr05d_stock_event_groups_v2_all/stock_event_groups.jsonl" \
  --output-dir "data/interim/pr05e_stock_evidence_bundles_v2_all"
```

**소요 시간**: LLM 없음, 수 분

---

### Step 3: pr05f — DART 연간 재무 팩트 추출 (전 종목)

```bash
python scripts/processors/pr05f_extract_dart_annual_financial_facts.py \
  --bundles-jsonl "data/interim/pr05e_stock_evidence_bundles_v2_all/stock_evidence_bundles.jsonl" \
  --output-dir "data/interim/pr05f_dart_annual_financial_facts_v2_all"
```

---

### Step 4: pr05f — DART 공시 상세 팩트 추출 (전 종목, 가장 오래 걸림)

```bash
python scripts/processors/pr05f_extract_dart_disclosure_detail_facts.py \
  --bundles-jsonl "data/interim/pr05e_stock_evidence_bundles_v2_all/stock_evidence_bundles.jsonl" \
  --output-dir "data/interim/pr05f_dart_disclosure_detail_facts_v2_all" \
  --document-dir "news_generator/data/raw/dart/disclosure_documents" \
  --download \
  --sleep-sec 0.3
```

**주의**: DART 공시 문서를 HTTP로 다운로드함. 약 1만 건 대상이면 수 시간 소요 가능.  
현재 10개 종목 문서는 이미 캐시됨 (`--overwrite-zip` 없이 실행하면 기존 캐시 재사용).

---

### Step 5: pr05f — 브리프 빌드 (전 종목, 새 버전 디렉토리 지정)

```bash
python scripts/processors/pr05f_build_stock_news_briefs.py \
  --bundles-jsonl "data/interim/pr05e_stock_evidence_bundles_v2_all/stock_evidence_bundles.jsonl" \
  --output-dir "data/interim/pr05f_stock_news_briefs_v3_all_stocks" \
  --dart-annual-financial-facts-csv "data/interim/pr05f_dart_annual_financial_facts_v2_all/dart_annual_financial_facts.csv" \
  --dart-disclosure-detail-facts-csv "data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv"
```

**예상 출력**: ~9,000건 브리프, ~4,300건 ready

---

### Step 6: pr06a — LLM 요청 파일 빌드

```bash
python scripts/processors/pr06a_build_stock_news_sample_requests.py \
  --briefs-jsonl "data/interim/pr05f_stock_news_briefs_v3_all_stocks/stock_news_briefs.jsonl" \
  --output-dir "data/interim/pr06a_full_requests_v4_2_all_stocks" \
  --model gpt-4o \
  --include-readiness ready \
  --include-news-types "stock_event_trigger,corporate_action_disclosure,sparse_disclosure" \
  --max-total-requests 9999 \
  --temperature 0.25 \
  --max-tokens 700
```

**주의**: `--max-total-requests`를 충분히 크게 설정해야 전체가 포함된다.

---

### Step 7: 청크 분할 + 순차 배치 제출

OpenAI Batch API의 동시 제출 토큰 한도는 **90,000 enqueued tokens**이다.  
30건 × ~2,700 tokens = ~81,000 tokens이므로 **반드시 1청크씩 순차 제출**해야 한다.

기존 스크립트 참조: `news_generator/scripts/processors/run_v4_1_full_chunk_download_and_audit.py`  
새 청크 디렉토리와 배치 ID를 반영한 v4_2 버전을 만들어 사용할 것.

**청크 분할 패턴**:
```python
CHUNK_SIZE = 30
chunks = [rows[i:i+CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
```

예상 청크 수: 4,300건 ÷ 30 ≈ 143청크 → 약 4~5시간 소요

---

### Step 8: 오딧

오딧 로직은 `run_v4_1_full_chunk_download_and_audit.py`에 완성되어 있다.  
특히 아래 두 날짜 정규식이 모두 포함되어 있어야 한다 (이미 수정됨):

```python
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_KR_ABBREV_DATE_RE = re.compile(r"'(\d{2})\.(\d{1,2})\.(\d{1,2})일")
```

---

### Step 9: 최종 출력 파일 생성

오딧 통과 후 `outputs_all.jsonl`과 브리프 메타데이터를 조인하여 최종 데이터셋 생성:

필드: `stock_code`, `stock_name`, `anchor_date`, `event_family`, `action_type`, `news_text` (news_lines joined)

---

## 5. 핵심 파일 경로

| 파일 | 경로 |
|---|---|
| stocklist | `/Users/hgs/Desktop/IISE CD/stocklist.txt` |
| DART 공시 데이터 | `/Users/hgs/Desktop/IISE CD/news_generator/dart_collector/dart_results_2013_2024.json` |
| DART 이벤트 증거 | `/Users/hgs/Desktop/IISE CD/npc_generator/data/processed/dart_event_evidence/dart_event_evidence_detail.csv` |
| stock_event_calendar | `/Users/hgs/Desktop/IISE CD/data/raw/market_event/stock_event_calendar_2013_2023.csv` |
| 주가/거래량 데이터 | `/Users/hgs/Desktop/IISE CD/data/raw/stock/stock_price-volume_npq.xlsx` |
| pr05c 어노테이션 | `data/interim/pr05c_stock_event_context/stock_event_context_annotations.csv` |
| 현재 최종 브리프 (10종목) | `data/interim/pr05f_stock_news_briefs_v2_8_dart_detail_context/stock_news_briefs.jsonl` |
| 현재 최종 출력 (10종목) | `data/interim/pr06a_full_outputs_v4_1/outputs_all.jsonl` (414건) |
| 오딧 스크립트 | `news_generator/scripts/processors/run_v4_1_full_chunk_download_and_audit.py` |
| 프롬프트 빌드 스크립트 | `news_generator/scripts/processors/pr06a_build_stock_news_sample_requests.py` |
| OpenAI API 키 | `/Users/hgs/Desktop/IISE CD/news_generator/.env` (`OPENAI_API_KEY=...`) |

---

## 6. 주의사항 / 학습된 제약

### OpenAI Batch API
- **동시 제출 한도**: 조직당 enqueued tokens 90,000 이하
- 30건 청크 = ~81,000 tokens → 반드시 1청크 완료 후 다음 청크 제출
- 병렬 제출 시 chunks 3~14 전부 `failed` — 반드시 순차 제출

### 오딧 규칙 (strict gate)
- 금지어: `최근`, `것으로 나타났다`, `것으로 분석된다`, `이는`, `이에 따라`, `전망된다` 등
- `no_market_claim`일 때 `주가`, `거래량`, `급등`, `급락` 등 시장 반응 표현 금지
- numeric_leak: 뉴스 본문에 출처 팩트에 없는 숫자 불허
  - ISO 날짜 `2022-12-31` → `2022년12월31일` 변환 필요 (오딧 스크립트에 구현됨)
  - 약식 날짜 `'22.4.4일` → `2022년4월4일` 변환 필요 (오딧 스크립트에 구현됨)

### 보일러플레이트 필터
`pr05f_extract_dart_disclosure_detail_facts.py`의 `_BOILERPLATE_PREFIXES` + `_BOILERPLATE_KEYWORDS`로 DART 공시 내 법적 고지·환율 주석 등을 필터링. 새 종목 추가 시 새로운 보일러플레이트 패턴이 나올 수 있으므로 샘플 확인 후 필요 시 패턴 추가.

### 프롬프트 (최신 버전 반영됨)
`_system_prompt()`에 아래가 추가됨:
1. CJK 문자 처리 (중국어 사명 → 영문 또는 '현지 자회사')
2. Earnings 스타일: 2줄 작성 시 동일 동사 반복 금지, 2번째 줄 `기록했다`/`집계됐다` 사용

---

## 7. 예상 규모 (전 종목 기준)

| 항목 | 예상치 |
|---|---|
| 브리프 총 | ~9,000건 |
| ready 브리프 | ~4,300건 |
| LLM 요청 | ~4,300건 |
| 청크 수 (30건 기준) | ~143개 |
| 배치 제출 소요 시간 | 약 4~6시간 (순차) |
| DART 문서 다운로드 | 수 시간 (캐시 있는 10개 종목 제외) |
