# GDELT 뉴스 수집 파이프라인 (2014~2023)

거시경제·금리·환율·원자재·증시·기업 실적 관련 뉴스를 GDELT에서 수집하는 파이프라인입니다.

---

## 디렉터리 구조

```
gdelt_collector/
├── main.py                          # 메인 실행 진입점
├── config.py                        # 수집 설정 (키워드·테마·날짜 등)
├── requirements.txt
├── collectors/
│   ├── bigquery_collector.py        # ★ 1순위: BigQuery GKG + Events
│   ├── docapi_collector.py          # 2순위: DOC API (키워드 검색)
│   └── bulk_downloader.py           # 3순위: GKG 파일 직접 다운로드
├── processors/
│   └── dedup_processor.py           # 중복 제거 + 연관성 판정
├── queries/
│   └── gdelt_reference_queries.sql  # BigQuery 수동 쿼리 참고용
├── output/                          # 수집 결과 저장 (자동 생성)
│   ├── gkg_202201.parquet           # BigQuery 수집 결과
│   ├── events_202201.parquet
│   ├── docapi/                      # DOC API 수집 결과
│   └── processed/                   # 후처리 완료 파일
└── logs/                            # 실행 로그 (자동 생성)
```

---

## 수집 방법 비교

| 방법 | 기간 커버리지 | 설정 복잡도 | 속도 | 비용 | 권장 용도 |
|------|-------------|-----------|------|------|----------|
| **BigQuery** | 2014~현재 | 중간 (GCP 계정) | 매우 빠름 | ~$10~50 | ★ 전체 수집 |
| DOC API | 최근 3개월만 | 낮음 | 느림 | 무료 | 테스트·증분 |
| GKG 직접 다운로드 | 2014~현재 | 낮음 | 매우 느림 | 무료+트래픽 | BigQuery 불가 시 단기간만 |

---

## 설치

```bash
pip install -r requirements.txt

# BigQuery 사용 시 GCP 인증
gcloud auth application-default login
```

---

## 실행 방법

### 방법 1: BigQuery (권장)

```bash
# 전체 기간 (2014~2023)
python main.py \
  --method bigquery \
  --start 2014-01-01 \
  --end 2023-12-31 \
  --project-id your-gcp-project-id

# 연도별로 나눠서 실행 (안전)
python main.py --method bigquery --start 2014-01-01 --end 2014-12-31 --project-id ...
python main.py --method bigquery --start 2015-01-01 --end 2015-12-31 --project-id ...
# ... (2023까지 반복)
```

**config.py에서 `BQ_CONFIG["project_id"]`를 본인 GCP 프로젝트 ID로 변경하면 `--project-id` 생략 가능.**

### 방법 2: DOC API (테스트용)

```bash
python main.py \
  --method docapi \
  --start 2023-10-01 \
  --end 2023-12-31
```

### 방법 3: 직접 다운로드 (단기간만)

```bash
# 3개월 이내 권장
python main.py \
  --method bulk \
  --start 2020-01-01 \
  --end 2020-03-31
```

### 후처리만 실행

```bash
python main.py \
  --method process \
  --input-dir ./output \
  --output-dir ./output/processed
```

---

## 출력 스키마 (Parquet)

### GKG 수집 결과 (`gkg_YYYYMM.parquet`)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `ref_date` | str (YYYYMMDD) | 기준 일자 |
| `published_at` | datetime | 발행 시각 |
| `lang_code` | str | 언어 코드 (`kor`/`eng`) |
| `source_name` | str | 매체명 |
| `domain` | str | 도메인 |
| `url` | str | 기사 URL |
| `themes_json` | str (JSON) | GKG 테마 배열 |
| `persons_json` | str (JSON) | 인물 엔티티 배열 |
| `orgs_json` | str (JSON) | 기관 엔티티 배열 |
| `tone_score` | float | 감성 점수 (-100~+100, 음수=부정) |

### Events 수집 결과 (`events_YYYYMM.parquet`)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `event_id` | int | GDELT 이벤트 ID |
| `event_date` | str (YYYYMMDD) | 이벤트 발생일 |
| `actor1_name` | str | 행위자 1 |
| `actor1_country` | str | 행위자 1 국가 코드 |
| `actor2_name` | str | 행위자 2 |
| `cameo_code` | str | CAMEO 이벤트 코드 (4자리) |
| `cameo_root` | str | CAMEO 루트 코드 (2자리) |
| `goldstein_scale` | float | 골드스타인 척도 (-10~+10) |
| `num_articles` | int | 해당 이벤트 보도 기사 수 |
| `avg_tone` | float | 평균 감성 점수 |
| `source_url` | str | 원본 기사 URL |

### 후처리 결과 (`processed/gkg_YYYYMM.parquet`)

위 컬럼에 추가:

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `relevance` | str | `direct` / `indirect` |
| `event_ids` | list | 매핑된 GDELT 이벤트 ID |
| `cameo_codes` | list | 매핑된 CAMEO 코드 |
| `avg_goldstein` | float | 평균 골드스타인 척도 |
| `max_mentions` | int | 최대 멘션 수 |

---

## 필터링 기준

### 포함 테마 (GKG_THEMES, config.py)
- `ECON_*`: 거시경제 (금리, 물가, 외환, 증시, 재정, 무역 등)
- `ENV_OIL`, `ENV_GAS`, `ENV_COAL`, `ENV_METALS`: 원자재·에너지
- `SANCTION`, `WB_*`: 제재·세계은행 경제 테마
- `CENTRAL_BANK`, `MONETARY_POLICY`, `FISCAL_POLICY`: 통화·재정 정책

### 포함 CAMEO 루트 코드
- `07`: 경제 지원 제공
- `10`: 요구
- `13`: 위협 (제재 위협)
- `17`: 강제·제재
- `15`: 군사력 과시 (지정학 리스크)
- 기타 외교·갈등 관련 코드

### 연관성 판정
- **직접 연관**: 금리, 환율, 유가, CPI, 증시 급락 등 자산 가격에 즉각 영향
- **간접 연관**: 무역정책, 지정학, 기업실적, 공급망 등 중장기 영향
- **비연관**: 연예·스포츠·생활·지역 단신 → 제외

### 중복 제거
1. URL 정규화 후 정확 중복 제거
2. TF-IDF 코사인 유사도 ≥ 0.85 + 48시간 이내 → 후행 기사 제거

---

## 예상 수집량

| 연도 | 예상 기사 수 (처리 후) |
|------|----------------------|
| 2014 | ~30,000건 |
| 2015~2019 | ~40,000~60,000건/년 |
| 2020 (코로나) | ~80,000건 |
| 2021~2023 | ~60,000~70,000건/년 |
| **합계** | **약 50~70만건** |

---

## BigQuery 비용 추정

```
gdelt-bq.gdeltv2.gkg 테이블:
  2014~2023 전체 스캔: ~20TB → $100 (무조건 필터 먼저 적용)
  WHERE DATE >= ... AND DATE < ... 파티션 사용: ~2TB/년
  언어 + 테마 필터 적용 후 실제 처리량: ~100~300GB/년

BigQuery 무료 쿼리: 월 1TB
유료: $5/TB

월별로 나눠 실행 시: ~5~15GB/월 → 무료 티어 내 가능 (120개월)
연별로 실행 시: ~100~300GB/년 → 유료 $0.5~1.5/년 × 10년 = ~$5~15
```

---

## 주의사항

1. **BigQuery 파티션 활용**: `DATE` 컬럼은 INTEGER 형식 (`YYYYMMDDHHMMSS`). 반드시 `WHERE DATE >= ...` 조건을 포함해야 파티션 프루닝이 적용되어 비용이 절감됩니다.

2. **GKG 기사 제목 없음**: GDELT GKG에는 기사 제목이 포함되어 있지 않습니다. 실제 제목이 필요하다면 `url`을 이용해 별도 크롤링 필요 (robots.txt 준수).

3. **한국어 기사 식별**: `TranslationInfo` 컬럼의 `srclc:kor` 패턴으로 한국어 기사를 식별합니다. GDELT가 번역한 기사는 `translatedtitle` 필드에 번역 제목이 포함될 수 있습니다.

4. **DOC API 3개월 제한**: GDELT DOC API (artlist 모드)는 무료이지만 최근 3개월 데이터만 검색 가능합니다. 2014~2023년 전체는 반드시 BigQuery 또는 GKG 직접 다운로드를 사용하세요.
