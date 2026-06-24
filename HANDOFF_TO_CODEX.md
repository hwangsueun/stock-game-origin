# 뉴스 생성 파이프라인 인수인계 문서

## 0-E. 2026-06-22 — GDELT GKG 국내 사건 레이어 구축 완료 (0-D "다음 작업" item 1~5)

0-D의 "다음 작업" 5개 항목을 전부 실행했다. 모두 LLM 없이 결정론·무료·재현가능. 가격반응은 어떤 단계에서도 선별 조건으로 쓰지 않았다.

### item 2 — GDELT 원본 parquet 날짜 정합성 감사 (완료)

- 스크립트: `scripts/processors/audit_gdelt_date_consistency.py` (읽기 전용, 데이터 무변경).
- 산출: `data/interim/realworld_event_news/gdelt_date_audit/{REPORT.md, events_per_file.csv, gkg_per_file.csv}`.
- `events_*.parquet` 81파일 **2.13억 행**: `event_date`는 파일명 연월과 **0% 불일치**(파티셔닝 정상). 0-D가 의심한 어긋남은 `source_url`에 있다 — URL 게재일이 event_date보다 며칠 앞서므로(파싱가능 20.7% 중 28.7% 불일치) **URL 날짜를 event_date로 재사용 금지**. 또한 events는 글로벌(한국 geo 1.2%)·CAMEO 코드라 국내 원장 1차 소스로 부적합.
- `gkg_*.parquet` 117파일 **946,706 행**: `lang_code` 100% kor. **`ref_date == published_at`(일 단위 0% 불일치)** — `published_at`은 00:00 시각만 추가. `ref_date`=GDELT 수집일=실제 게재일 이상이라 **look-ahead 안전**. URL 경로 날짜는 59.7%만 파싱되고 차이는 대부분 ±1일(수집지연·KST/UTC 경계).
- **확정: canonical date = GKG `ref_date`**. `available_date_conservative` = `ref_date`+1캘린더일(UCDP/Wikidata 규약 일치). URL 날짜는 행별 교차검증용으로만 보존, 사건 소급 금지.
- **GKG 스키마 2종**: 68파일 `*_json`(JSON 배열), 49파일 `*_raw`(GDELT 원형 `Name,offset;…`). 양쪽 모두 ref_date/published_at/domain/url 공통. 빌더는 컬럼명 자동감지로 처리.

### item 1·3 — GKG에서 국내 개별 사건 원장 재구축 (완료)

- 스크립트: `scripts/processors/build_gdelt_domestic_event_candidates.py`.
- 기존 보도량 집계(`gdelt_context_summary`) **재사용 안 함**. 원본 기사행을 `(ref_date, 핵심 org)`로 클러스터링. 독립 도메인 수 = 동시대 corroboration 신호. **persons는 추출 품질 낮아(평균 0.45/기사, "Joongang Ilbo"를 인물로) 키에서 제외**, orgs만 사용(평균 3.5/기사). geo/통신사/외국 megacorp 스톱리스트로 노이즈 제거.
- **item 3 준수 검증 통과**: 본문/헤드라인 필드 0, 가격 필드 0, evidence는 `{url,domain,published_at}`만(≤12건/사건), 날짜 오류 0. 테마는 GKG 코드(예: 탄핵일 `IMPEACHMENT,CONSTITUTIONAL,ECON_STOCKMARKET`)만 원자적 사실로 보존.
- 산출: `data/interim/realworld_event_news/gdelt_gkg/normalized_events.jsonl` **87,793건**(≥3 도메인), corroborated(≥4) 54,731, high domestic-confidence 31,406, 2,063일. 스키마는 UCDP/Wikidata 정규화와 호환 + `contemporaneous_source=true`(UCDP/Wikidata는 retrospective였음).
- 상위 클러스터가 실제 사건을 정밀 포착: 2017-03-10 헌재(탄핵 인용), 2018-04-09/10 삼성+금감원(삼성증권 유령주식), 2016-08-31 한진(한진해운), 2015-08 롯데(경영권 분쟁), 2020-03-12 WHO(코로나).
- **핵심 한계**: GKG 수집 커버리지가 **2015~2020 집중**, 2013·2014·2021·2023 거의 비어 있음(3~11건), 2022 부분(1,199). 게임 타임라인 양 끝은 GKG로 미커버 → 별도 소스 보강 필요.

### item 4 — 출처 검증 큐 (완료)

- 스크립트: `scripts/processors/build_gdelt_domestic_review_queue.py`.
- corroborated(≥4 독립 도메인) + 일별 상한(≤6, high-confidence 우선)으로 검토 가능 규모화.
- 산출: `data/interim/realworld_event_news/gdelt_gkg/{source_verification_queue.jsonl, SOURCE_VERIFICATION_QUEUE_REPORT.md}` **11,924건**(high 11,170), 2,045일. 각 행에 `independent_domains` 목록 명시.

### item 5 — 동시대 출처 검증 사건 승격 (완료)

- 스크립트: `scripts/processors/build_gdelt_domestic_promotions.py` (원장 불변, 추가 파일만 생성).
- **상시 엔티티 문제 해결**: 절대 도메인 수만으론 National Assembly·Samsung처럼 거의 매일 보도되는 엔티티가 사건으로 과승격됨(초안서 N.A. 1,497건). **엔티티별 베이스라인 대비 스파이크 탐지** 추가 — 상시 엔티티(활동일 ≥40일)는 자기 중앙값의 ≥1.5배 도메인일만, 희소(episodic) 엔티티는 출현 자체를 사건으로. 결과 N.A. 1,497→168.
- 게이트: discrete-event 스파이크 + 도메인 ≥6 + high domestic-confidence + 깔끔한 엔티티 라벨 + 테마 보유 + 일별 ≤3. 가격반응 미사용.
- 산출: `data/interim/realworld_event_news/gdelt_gkg/{promoted_events.jsonl, PROMOTED_EVENTS_REPORT.md}` **3,923건**(generation_eligible=true), 1,605일, 커버 연도 490~824/년(≈일 1~2건). 고스파이크 검증: 2019-01-31 현대중공업(ratio 8.0, 대우조선), 2019 아시아나(매각), 2015-08 롯데, 2019-11-10 공정위+LG(CJ헬로 심사).

### 엔티티 라벨 정리 (완료, 2026-06-22 추가)

- 스크립트: `scripts/processors/clean_gdelt_entity_labels.py` (원장 불변, 정리본만 추가 생성).
- GDELT 한국어 추출이 만든 라벨 오염(선행 월/junk "Aug Samsung Group"·"Korea Yeouido The National Assembly", 한국 약어 title-case "Sk Telecom"·"Lg Group", 다중 엔티티 병합 "Samsung Electronics Sk Hynix")을 큐레이션 사전(chaebol/부처/기관/정당 56엔티티, **한국어명 포함**)으로 정리.
- 핵심 기법: **정식 키워드를 라벨 어디서든 substring 매칭**(선행 junk 무시) + **longest-first 비겹침 스팬 수락**(단일 엔티티가 그룹명을 포함하는 "Hyundai Oil Bank"는 1엔티티=canonical, 진짜 병합 "Samsung Electronics Sk Hynix"만 concatenated) + **foreign-government 우선 검사**("China Ministry Of Foreign Affairs"가 외교부로 오매핑되던 버그 수정→foreign).
- 산출: `promoted_events_cleaned.jsonl`(+`entity_clean`/`entity_canonical`/`entity_ko`/`label_quality`/`label_generation_ready` 필드), `entity_label_map.csv`(raw→canonical 검토표), `ENTITY_CLEANUP_REPORT.md`.
- 결과: **generation-ready 3,853/3,923 = 98.2%**(56 정식엔티티에 한국어명 매핑). label_quality 분포: canonical 3,821·concatenated 32·clean_unmapped 14·foreign 47·garbled 9. foreign/garbled 56건만 비생성가능 플래그. 상위: 롯데그룹 403·삼성그룹 379·국회 242·새누리당 214·금융감독원 209.
- **dedup은 하지 않음**(의도): 하나의 사건이 여러 날 보도되는 것은 정상이므로 연속일 보존.

### 3소스 단일 일별 큐 통합 (완료, 2026-06-22 추가)

- `build_realworld_event_news_candidates.py`의 `newsworthiness()`에 **`GDELT_GKG` 분기** 추가(점수 = 50+독립도메인수, 상한 95·high domestic +3·`label_generation_ready` 아니면 0).
- **버그 수정**: 공유 빌더가 후보 생성 시 `generation_eligible=False`를 일괄 덮어써 이미 승격된 GKG의 True가 사라지던 문제 → `event.get("generation_eligible", False)` 보존으로 변경(retrospective UCDP/Wikidata는 False 유지, 동시대 검증된 GKG는 True 유지).
- 실행: UCDP 7,087 + Wikidata 4,953 + GKG promoted_cleaned 3,923 → `data/interim/realworld_event_news/ucdp_wikidata_gdelt/{daily_review_queue.jsonl, all_candidates.jsonl, REPORT.md}`.
- 결과: 입력 15,963 → 후보 15,843 → **일별 큐 9,671**(GKG 2,940 전부 gen=True / UCDP 5,405 / Wikidata 1,326), 3,931일, 국내 ≥1 보유일 1,601.
- **상호 보완 커버리지 검증**: GKG 공백연도(2013·2014·2021·2023)를 UCDP+Wikidata가 메움(각 636~896), 2015~2020은 GKG 국내가 주도. 샘플 2017-03-11=헌법재판소(rank1, 탄핵) 국내 우선 정상.

### 저작권 안전 이벤트 브리프 (기반 레이어, 완료, 2026-06-22 추가)

- 방침 확정: **기사 본문에 접지하지 않고(저작권), 저작권 프리 1차 사실에만 접지.** 사실(누가·언제·무엇)은 비저작물, 보호 대상은 기사의 창작적 표현뿐.
- 스크립트: `scripts/processors/build_gdelt_event_briefs.py`. GKG theme 코드(=GDELT 개방데이터)를 **한국어 사건유형 통제어휘**(탄핵·IPO·수사·규제·금리·재난·증시 등 25종)로 매핑 + entity_ko + 날짜 + N매체 검증수 + tone으로 **사실형 브리프** 생성. **무조작**(구체 사실 미창작) + 무료.
- 산출: `data/interim/realworld_event_news/gdelt_gkg/{event_briefs.jsonl 3,853건, EVENT_BRIEFS_REPORT.md}`. 예: "2017-03-10, 헌법재판소 관련 탄핵 현안이 국내 39개 매체에서 비중 있게 보도됐다. 보도 논조는 대체로 부정적이었다." 유형분포: 증시·주가 643·정치 355·에너지 222·수사 210·금리 194·IPO 154·탄핵 34 등.
- **품질 한계**: 이 브리프는 "현저성+사건유형+논조"까지만 사실접지(본문 미보유). 구체 사실은 없음.

### 품질 상향 경로 (저작권 안전, 1차 공공소스 접지) — 다음 결정

GKG 사건의 사실접지 = 저작권 프리 1차 공공소스를 (entity, date)로 조인. 티어 비율: **정부·정치 54%(2,063) / 기업 46%(1,789)**.
1. **기업 46% → DART 공시**(이미 보유, 규제공시=비저작물). 단 GKG 기업스파이크가 DART 공시와 겹치면 기존 stock 파이프라인(outputs_all 1,634)이 이미 커버했을 수 있어 **중복/순증 여부 측정 필요**. 지배구조 드라마(롯데 분쟁 등)는 공시 없어 순증이나 사실원 부족.
2. **정치 54% → 한국 정부 개방데이터**(저작권 프리): 국회 의안정보 API, 헌재 결정문, 공정위/금감원 의결·보도자료, 선거=NEC/Wikidata(CC0). 수집 필요하나 고품질.

### DART 조인 측정 (완료, 음성 결과) — `join_gdelt_corporate_to_dart.py`

기업 GKG 사건 1,787건을 (그룹→유니버스 상장사, ±3일) DART 공시에 조인해 순증/중복 측정. 산출 `corporate_dart_join.jsonl`·`CORPORATE_DART_JOIN_REPORT.md`.
- **dart_grounded 362(20%)**: 근처 공시 있음 → 기존 stock 파이프라인이 이미 커버(중복), GKG는 현저성만 확인.
- **in_universe_no_dart 1,172(66%)**: 유니버스 종목이나 공시 없음 = 지배구조·법률 드라마(롯데 358·삼성 264·현대 130). 순증이나 DART 접지 불가.
- **not_in_universe 253(14%)**: 포스코 154·아시아나 78 등 유니버스 밖. 순증이나 DART 없음.
- **결정적: DART 조인은 안전한 접지가 아님.** GKG는 그룹단위·DART는 계열사단위라 날짜창 매칭이 무관 공시를 갖다 붙임 — 2018-04 삼성그룹(삼성증권 유령주식)이 삼성SDI 지분처분으로, 2017-02 삼성그룹(이재용 뇌물재판)이 삼성생명 실적변경으로 거짓 접지됨. **자동 날짜조인 금지.** → DART 접지는 폐기, theme-브리프가 안전 표현으로 유지.

### 결론 및 남은 작업

- **저작권 안전 품질 천장**: 정밀 사건↔소스 매칭(날짜조인 아님)을 하지 않는 한 **theme-브리프(`event_briefs.jsonl`)가 GKG 레이어의 안전한 뉴스 표현**. DART 날짜조인은 거짓접지로 폐기.
- 정치 54% 등 순증 사건의 풍부한 사실접지는 **정밀 매칭 가능한 공공기록**(예: 국회 의안정보 API는 날짜·의안 정확매칭 가능)만 케이스별로 검토 — 대형 연구성 작업, 페이오프 불확실.
- 커버리지 공백(2013·2014·2021·2023 GKG 0건)은 통합 큐에서 UCDP+Wikidata가 메우나 국내 정치 공백 잔존.
- clean_unmapped 14건은 억지 매핑 안 함(검토표 수동 처리).

작성일: 2026-06-22
대상: 다음 작업 세션

---

## 0-D. 2026-06-22 — 거시 전망 확정 및 실제 정치·사회 사건 레이어 착수

### 거시 전망 파이프라인 확정

- 최종 입력 체인: `with_releases -> with_calendar -> with_sep -> with_outlook`.
- 전기간 입력: `news_generator/data/interim/macro_news_generation_v2/news_generation_input_macro_with_outlook.jsonl` 2,865일.
- 전망 소스는 연준 SEP(미국) + IMF WEO(한국·미국)로 확정. 별도 한국은행 전망은 사용하지 않는다.
- 하루 정확히 5건이며 공식발표·프리뷰·리뷰·전망은 독립 필수 기사다.
- 종합 표본 12일/60건 생성 후 자동 감사 PASS. 결과:
  - `sample_quality/generated_consolidated_12days_final.csv`
  - `sample_quality/CONSOLIDATED_OUTLOOK_12DAY_AUDIT.md`
- 생성기/감사기 보강: `projection` 필수 커버, 전망 기관·동사 강제, evidence 밖 숫자 차단.
- 기사 본문의 `~할 수 있다`, `~로 해석된다`, `~로 해석될 수 있다`는 금지한다.
  방향성 리뷰 자체는 허용하며 `약세 흐름을 보였다`, `상승세가 두드러졌다`처럼 단정적 기사체를 쓴다.

### 실제 정치·사회 사건 레이어 방향

- 사건 뉴스 포함 여부를 사후 가격반응으로 결정하지 않는다. 그렇게 하면 미래 가격을 이용한 선택 편향이 생긴다.
- `사건 뉴스 원장`과 `사후 자산반응 sidecar`를 분리한다.
- 사건 원장은 당시 공공성·중요도·공개시점으로 선별한다.
- 가격반응은 다음 거래일 후속 맥락이며 인과를 자동 주장하지 않는다.
- 국내 주식 게임이므로 날짜별 사건 검토 큐에서 국내 사건을 최소 1개 우선 보존한다.

### UCDP 수집·정규화 완료

- 공식 UCDP GED 26.1 CSV 다운로드:
  `news_generator/data/raw/realworld_events/ucdp/GEDEvent_v26_1.csv`.
- 2013~2023년, 날짜 정밀도 1, Clear, 일자·국가·분쟁 집계, 사망 추정 25명 이상:
  **전 세계 7,087건** 정규화.
- 출력: `news_generator/data/interim/realworld_event_reactions/ucdp_v26_1/normalized_events.jsonl`.
- 자산 사전 매핑이 있는 사건만 sidecar 반응 계산:
  - 사건-자산 14,687쌍
  - 반응 임계 통과 836쌍
  - 동일 반응일 충돌 제거 검토 큐 258쌍
- 가격 sidecar는 전부 `causal_claim_allowed=false`, `generation_eligible=false`.
- 2020년 WTI 음수 가격 전환의 무의미한 비율 수익률과 주말 복수사건 동일 월요일 귀속을 차단했다.
- 코드:
  - `scripts/processors/build_realworld_event_reaction_candidates.py`
  - `scripts/processors/audit_realworld_event_reaction_candidates.py`
- 감사 결과: PASS, failures 0.

### Wikidata 수집·통합 완료

- CC0 구조화 사건을 유형×연도 소형 SPARQL 쿼리로 수집하도록
  `scripts/processors/fetch_wikidata_realworld_events.py`를 추가했다.
- 대상 유형: 선거, 국민투표, 쿠데타, 테러, 자연재해, 대형사고, 시위, 파업.
- 대형 하위클래스 재귀 쿼리는 504로 실패했으므로 사용하지 않는다.
- 소형 쿼리는 응답 캐시를 저장한다. 영문 라벨 API는 429가 발생해 20개 배치·배치별 캐시로 보강했다.
- 국내 사건은 `build_realworld_event_news_candidates.py`에서 +20 중요도 가중 및 날짜별 1개 우선 슬롯을 받는다.
- Wikidata 정규화 결과: **4,953건**.
  출력: `news_generator/data/raw/realworld_events/wikidata/normalized_events.jsonl`.
- UCDP + Wikidata 통합 결과:
  - 입력 12,040건
  - 중요도 후보 11,990건
  - 일별 검토 큐 8,141건, 3,779일
  - 감사 PASS, failures 0
- 통합 출력:
  `news_generator/data/interim/realworld_event_news/ucdp_wikidata/`.
- Wikidata의 한국 사건은 정확 클래스 기준 **13건**이며 전부 일별 큐에 보존됐다.
  다만 국내 정치·사회 이슈 원장으로 쓰기에는 절대적으로 부족하므로 Wikidata만으로 국내 커버리지를 충족했다고 보지 않는다.
- 모든 사건은 현재 `generation_eligible=false`다. URL과 공개시점이 있는 동시대 출처를 별도로 검증하기 전에는 기사 생성 입력으로 승격하지 않는다.

### 다음 작업

1. GDELT는 기존 보도량 집계를 재사용하지 말고 원본 Events/GKG에서 **한국 국내 개별 사건·공개시점·URL**을 우선 재구축한다.
2. 기존 `events_*.parquet`는 파일명 연월과 URL 시점이 어긋난 정황이 있으므로 사용 전에 날짜 정합성을 감사한다. 우선 기준은 GKG의 `published_at`/`ref_date`다.
3. 기사 본문은 저장·복제하지 않고 URL, 공개시점, 출처 도메인, 구조화된 사건 식별자와 원자적 사실만 보존한다.
4. 같은 사건을 복수 독립 도메인이 보도했는지 확인하는 출처 검증 큐를 만든다.
5. 동시대 출처 검증을 마친 사건만 `generation_eligible=true`로 승격한다. 가격반응은 승격 조건으로 사용하지 않는다.

작성일: 2026-06-18 (2026-06-22 업데이트)
대상: 다음 작업 세션 (Codex 또는 차기 AI)

---

## 0-C. 2026-06-19 — 미사용 데이터 활용: 시장 뉴스 + 연간 실적 뉴스

기존 데이터셋(종목 공시 뉴스)에 더해, **이미 수집·가공된 미사용 데이터**로 새 뉴스 카테고리를 추가. **날짜 정합성**에 집중(아래).

### ① 시장·섹터 뉴스 (완료, LLM 0)
- 소스: `market_indicator/data/processed/{macro,sector}_event_candidates_daily.csv` (이미 한국어 서술 존재)
- 빌더: `news_generator/scripts/processors/build_market_news.py`
- 산출: `news_generator/data/interim/market_news/market_news.jsonl` **9,169건** (매크로 1,948 + 섹터 leader/laggard 7,221), 일평균 3.4건, 2013–2023 전 연도
- **날짜 정합성**:
  - 시장데이터 이벤트(금리·환율·유가·지수·스프레드) → `date`(당일 종가 공개) ✅
  - 섹터 5일 추세는 '최근 5거래일'(backward)이라 `date`에 이미 공개 ✅ (sector_daily_rank의 return_5d = cum_prior5 검증)
  - **경제지표 update(CPI·수출·생산·선행지수) 제외**: 참조월 1일로 찍혀 일부 발표일보다 이르고(look-ahead) 숫자도 원자료 → `EXCLUDE_MACRO`
  - **주말 날짜 제외**: 미국 스프레드·Dubai 유가 시계열이 주말에도 값 채워 생긴 893건 아티팩트 차단(`is_weekend`)
- 품질: frame의 추측성 꼬리("…영향을 줌") 제거하고 사실만; 조사 이/가 교정(환율이·KOSDAQ이); 데이터단절(|일변동|>15%, Dubai 유가 ~30% 가짜점프) 제외; 인과·추측어 0
- 선별: strength≥4 + 매크로 (date,asset) dedup + 섹터 leader/laggard

### ② 연간 실적 뉴스 (완료)
- **사업보고서 XML 서술 추출은 실패**(회사별 표현 차이로 수율 3~16%·금액 오매칭) → 폐기
- **DART 재무제표 API(`fnlttSinglAcntAll`)로 구조화 재무 수집**: `fetch_annual_financials.py` (corp_code 117 × 2012–2023, CFS 우선·OFS 폴백) → `annual_financials_api.csv` **890건/116종목**. 단, 구조화 XBRL은 **2015년부터**라 2012–2014는 없음.
- 기사 빌더: `build_annual_earnings_news.py` → annual_news (원→조/억, 손실 라벨, 연결/별도 표기)
- **2012–2014 보강**: API XBRL이 2015~만 제공 → `build_annual_earnings_2012_2014.py`로 detail CSV의 매출액변동 공시(1~4월 = 연간 확정)에서 186건 추출, 공시 접수일 날짜. 병합 후 `annual_earnings_news.jsonl` **1,032건 (2012–2023 전 11년)**, basis: filing 845 / disclosure 186 / estimated 1
- **날짜 정합성(핵심)**: publish_date = 사업보고서 공시일(rcept_date), `dart_business_report_index.csv`((stock,year)별 접수일, JSON 1,797건에서 생성)에서 매핑. **검증: 날짜비정상 0·주말 0**.
  - 공시일=회계연도+1(통상)/+2(정정) 아니면 인덱스 오매칭으로 보고 (연도+1)-03-31 추정+다음영업일 보정. (예: 펄어비스 2017이 2017-09로 오매칭 → 2018-04-02로 교정)
  - 1억 미만 금액은 '약 0억원'으로 무의미 → 매출 0이면 행 스킵, 영익/순익 0이면 항목 생략(skipped 44).
- 값 정확성: 구조화 API라 신뢰(단위버그 불가). 기아 2022 매출 86.56조, 한전 2015 영업이익 11.3조 등 실제와 일치. 공시 실적(매출액변동) 없는 안정기업까지 커버.

### 신규 추가 합계
| 카테고리 | 건수 | 날짜 |
|---|---|---|
| ① 시장·섹터 뉴스 | 9,169 | 이벤트 거래일 (2013–2023) |
| ② 연간 실적 뉴스 | 1,032 | 공시일 (2012–2023 전 11년) |
| **신규 합계** | **10,201** | 전수 날짜 검증 통과 (비정상 0·주말 0) |

> 기존 종목공시 뉴스(outputs_all 1,634 / split 794)에 더해 **시장·연간 뉴스 1만건**이 추가됨. 게임 타임라인의 "그날 시장 분위기"와 "연간 실적 확정" 공백을 메움.

---

## 0-B. 2026-06-19 — 통합 맥락 레이어(가격·섹터·GDELT) 구축

### ✅ 409건 재생성·병합 완료 (2026-06-19 추가 실행)

코덱스가 청크 분할까지 해두고 **미제출** 상태였던 맥락 재생성 배치를 실행해 outputs_all에 반영 완료.

| 단계 | 상태 | 비고 |
|---|---|---|
| 409건 맥락 재생성 배치 (14청크 순차) | ✅ 완료 | 러너 `run_context_regen_409.py`, 409/409 출력, 에러 0 |
| v8 맥락 오딧 | ✅ 408/408 PASS (100%) | 모델 자체거부 1건(`STOCK_BUNDLE_003592`)은 base 유지 |
| outputs_all 병합 | ✅ 완료 | 408건 교체, 백업 `outputs_all.before_context_v8.jsonl` |
| 전체 1,634건 최종 v8 오딧 | ✅ **1,629/1,629 PASS (100%)** | 시장맥락 409건 전부 ctx_pass, 자체거부 5건 |
| 단위버그 무회귀 검증 | ✅ | 한솔 3,684억·KMW 6,829억 유지(조 leak 0), 맥락줄 추가됨 |

- 전용 러너: `news_generator/scripts/processors/run_context_regen_409.py`
  (코덱스 state.json의 저장소-루트 상대경로가 cwd(`news_generator/`)와 어긋나는 버그 수정 — 재개 시 경로 필드는 절대경로 유지)
- 재생성 출력: `context_layers/regeneration_409/regen_outputs.jsonl` (409건)
- 오딧: `context_layers/regeneration_409/audit/audit_v8.csv` (408 PASS)
- 병합 필터: 자체거부 1건 제외한 408건만 교체 → `requests.merge408.jsonl` / `regen_outputs.merge408.jsonl`
- 최종 오딧 리포트: `pr06a_full_outputs_v4_2_all_stocks/audit_v8_final/audit_v8.csv`
- **정합성**: 409 요청은 단위버그 정정본(`..._fixed`) 위에서 빌드돼 매출 정정값(3,684억 등)을 보존. 병합 후 ×1000 회귀 없음 확인.

**병합 명령 (재현용):**
```
python scripts/processors/merge_context_regeneration_outputs.py \
  --base-outputs-jsonl data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl \
  --context-outputs-jsonl data/interim/context_layers/regeneration_409/regen_outputs.merge408.jsonl \
  --context-requests-jsonl data/interim/context_layers/regeneration_409/requests.merge408.jsonl \
  --semantic-audit-csv data/interim/context_layers/regeneration_409/audit/audit_v8.csv \
  --out <임시파일> && mv <임시파일> data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl
```

> **현재 최종 데이터셋**: `outputs_all.jsonl` (1,634건) = 단위버그 전량 정정 + 맥락 레이어 409건 반영. 남은 작업은 기사 분리 전량 확장(아래 프로토타입) 또는 Step 9 최종 출력 조인.

### 최종 정책

- 가격: 1일 5%, 5일 8%, 거래량 3배 중 하나를 넘는 사건에만 비인과 주가 문장 허용.
- 섹터: 117종목 명시적 KRX coarse 업종 매핑. 가격 재료성 사건에만 동일 기간 업종지수 수익률을 인접 사실로 허용.
- GDELT: 생성 문장에 직접 노출하지 않음. `관련 보도 N건, 평균 톤 X`는 부자연스럽고 독자 해석 가치가 낮아 sidecar 내부 피처로만 보존.
- 인과·감정·전망어는 모든 맥락 문장에서 금지.

### 산출물

- `news_generator/scripts/processors/build_price_reaction_layer.py`
- `news_generator/scripts/processors/build_sector_reaction_layer.py`
- `news_generator/scripts/processors/build_gdelt_event_layer.py`
- `news_generator/scripts/processors/inject_context_layers.py`
- `news_generator/scripts/processors/audit_v8_context.py`
- `news_generator/data/interim/context_layers/requests_context_v8_integrated.jsonl`: 1,634건, 가격·섹터 맥락 409건
- `news_generator/data/interim/context_layers/gdelt_event_context.csv`: GDELT 보수적 매칭 84건(sidecar, writer payload 0건)

### 검증

- GDELT 월별 parquet 117개, 946,706행 전량 재가공. 기존 가공기의 `KOREA_TEXT.search` 문자열 버그를 수정했고 파일 예외를 성공처럼 skip하지 않고 전체 실패로 처리함.
- 통합 검증 60건: accepted 59건 `audit_v8_context.py` **59/59 PASS**, 모델 자체 거부 1건.
- 가격·섹터 30건 실물 문장 점검: 업종지수 표현 30건, 방향 불일치 0건. GDELT 메타 문구 노출 0건.

### ✅ 기사 분리 전량 확장 완료 (2026-06-19 추가 실행)

프로토타입(8쌍)을 material 전 사건으로 확장 완료. **LLM 없이 결정적 템플릿**이라 무료·재현가능.

| 항목 | 결과 |
|---|---|
| 빌더 | `build_split_articles_all.py` (프로토타입 카피 로직 재사용, SAMPLE_KEYS 제거) |
| 입력 | 정정 요청 1,634건 중 material 409건 |
| 산출 | **397쌍 = 794 기사** (공시 397 + 반응 397) |
| 제외 | 비material 1,225 · 결함(금액/contract_item 누락) 5 · 반응 dedup 3 · 동일연도 잠정→확정 dedup 4 |
| 오딧 | `audit_split_articles.py` → **794 기사 / 397쌍 / 실패 0** |
| 반응 horizon | 1일 93 · 5일 257 · 거래량 47 |
| 사건군 | earnings 239 · contract 101 · dividend 40 · investment 17 |

- 산출물: `context_layers/split_articles_all/split_articles_all.jsonl` (802행) + `.md`(집계+샘플 30쌍)
- dedup: 같은 `(stock_code, reaction_publish_date, horizon)` 겹치면 1건만 — 나머지 사건은 split 세트 제외(outputs_all 단일기사로 잔존)
- 단위정정 보존: 비현실 매출(≥200조) leak 0건. 업종지수는 구체업종만(광의 제조·금융·일반서비스 제외).
- **샘플 품질 보정 (2026-06-19)**: 공유 모듈 `build_split_article_prototype.py` 정규화 강화 — (1) 라틴문자 끝 종목명 조사 교정(`DL은`·`GS은`·`HMM은`·`S-Oil은`·`삼성SDI는`, `_NAME_JONGSEONG` 음독 받침 맵), (2) 상대방 사명 정리(`주식회사`/`(주)`·중복 영문 괄호·잔여 `]`·영문 법인꼬리표 `Co.,Ltd/Inc./GmbH` 제거, 콤마 다중법인 세그먼트별 처리 → `현대해상화재보험`·`하나은행`·`사우디전력청`·`노바백스`).

- **금액 교차검증 + 2차 보정 (2026-06-19)**: split 전 금액을 사건군별 최대값 스캔으로 검증.
  - **earnings 금액 정확 확인**: 신한지주 61.8조·기아 59.1조(2020 실제 59.2조)·한국가스공사 37.2조·삼성생명 32.8조 등 실제와 부합 → 단위버그 잔존 0 재확인.
  - **잡단어 상대방 제거**: 계약 변경·정정 공시에서 상대방 칸에 흘러든 문구(`변경`/`정정`/`종료` 등)를 `_JUNK_PARTY_KEYWORDS` 부분일치로 폐기 → 상대방 없이 서술(예: `삼성물산은 약 1조1,554억원 규모 공급계약을 체결했다고…`).
  - **자회사 귀속 교정 (35건)**: `…(자회사의 주요경영사항)` 공시는 실적·계약·투자 주체가 자회사(공시 주체=지주). 지주 본인 활동으로 오귀속되던 것을 `자회사` 프레이밍으로 교정. 예: 옛 `한진칼은 2015년 매출 11.5조를 기록` → `한진칼은 자회사의 2015년 매출 약 11조5,448억원 … 실적을 공시`(한진칼 단독 매출은 ~1,110억으로 별도 존재). `한진칼은 자회사의 약 8조7,098억원 규모 신규 시설투자를 공시`(대한항공 항공기 발주, 단위 정상).
  - **동일연도 잠정→확정 dedup**: 같은 (종목·회계연도) 실적 재공시(잠정→확정, 매출 ±5% 이내)는 확정치(최신일)만 유지 → 4건 제거(대한항공·KMW·우리기술투자·DB하이텍). 한진칼 2019는 자회사 12.7조·단독 1,110억·1조2,035억으로 값이 달라 별개 주체로 보존.
  - **금액 교차검증(웹 포함)**: 종목내 max/min>20x 이상치 전수 확인 → 전부 실재. 우리기술투자 6,194억/4,309억 = **두나무(업비트) 평가손익**(웹확인, 단위버그 아님), 씨젠 1.1조 = 코로나 진단키트 붐. 자세한 내용은 메모리 [[dart-disclosure-unit-bug]].
  - **최종**: **397쌍/794 기사**, audit_split_articles **실패 0**, 알려진 결함 패턴(주식회사·잡상대방·`]`·영문꼬리표·이중공백·조사오류) **전수 0건**.
- **데이터셋 의미**: outputs_all(1,634, 인라인) 대비 split은 별도 표현 — material 사건만 공시일/반응 2기사로 분리. 게임 반영 시 비material 1,225(outputs_all) + 401쌍 = 약 2,035 기사 인스턴스.

**재현 명령:**
```
python scripts/processors/build_split_articles_all.py \
  --requests-jsonl data/interim/pr06a_full_requests_v4_2_all_stocks_fixed/stock_news_sample_requests.jsonl \
  --price-csv data/interim/context_layers/price_reaction.csv \
  --sector-csv data/interim/context_layers/sector_reaction.csv \
  --dart-detail-csv data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv \
  --out data/interim/context_layers/split_articles_all/split_articles_all.md \
  --jsonl-out data/interim/context_layers/split_articles_all/split_articles_all.jsonl
python scripts/processors/audit_split_articles.py \
  --articles-jsonl data/interim/context_layers/split_articles_all/split_articles_all.jsonl \
  --price-csv data/interim/context_layers/price_reaction.csv
```

### 기사 분리 프로토타입(최신 방향)

- 하나의 사건을 `공시일 기사` + `t+1 또는 t+5 반응 기사`로 분리한다.
- 반응 기사에서 `공시 후`라고 쓰지 않고, `공급계약 체결`, `실적 발표`, `배당 결정`, `지분 취득` 등 구체적 사건을 다시 밝힌다.
- 계약·배당·실적 팩트는 반드시 동일 `rcept_no` 단위로 묶어 서로 다른 공시의 상대방·금액이 섞이지 않게 한다.
- 1~4월 `매출액또는손익구조` 공시는 전년도 실적 연도를 명시한다. 손실 부호는 `영업손실`·`당기순손실` 라벨로 보존한다.
- 광의 업종(`제조`, `금융`, `일반서비스`)은 반응 기사 비교에서 제외하고, 구체적 업종이며 종목과 2%p 이상 차이 날 때만 사용한다.
- 프로토타입: `context_layers/validation_60/split_article_prototype.md` / `.jsonl` (8쌍, 16건).
- 오딧: `audit_split_articles.py` → **16/16 PASS**.
- 정정공시는 신규 계약/투자로 쓰지 않는다. 상세공시 중 계약 정정 391건을 확인했으며 `is_correction`, `correction_reason`을 별도 추출한다.
- 현재 종목명을 과거에 소급하지 않고 `issuer_name_as_filed`를 우선한다. 예: 2013년 `000210`은 `DL`이 아니라 `대림산업`.
- 계약명(`contract_item`)과 재무제표 범위(`statement_scope`: 연결/별도)를 기사 소재에 추가한다.
- KRX 가격제한폭의 복리 상한/하한을 넘는 수익률은 원자료 단절로 간주해 제외한다. HMM 2016-04-29 `ret_5d=542.5%` 1건이 제외됨(재료성 840→839).
- 전량 확장 시 같은 종목의 여러 공시가 같은 반응 구간에 겹치면 후속 기사를 `(stock_code, reaction_publish_date, horizon)` 기준 1건으로 병합한다.

---

## 0-A. 2026-06-19 — DART 공시 단위 버그 수정 및 전 종목 배치 완료

### ✅ 현재 상태 한눈에 보기 (2026-06-19 단위 버그 전량 정정 완료)

| 단계 | 상태 | 비고 |
|---|---|---|
| v4.2 전 종목 배치 (55청크) | ✅ 완료 | `outputs_all.jsonl` 1,634건 / 누락·에러 0 |
| 단위 버그 코드 수정 (pr05f) | ✅ 완료 | `_detect_statement_unit`(윈도우=변동내용 섹션 전체) + `_sanity_adjust_unit`(크기 가드) + `_format_krw_amount` |
| 상세 팩트 CSV 재생성 + 브리프 재빌드 | ✅ 완료 | `pr05f_stock_news_briefs_v3_all_stocks` |
| 정정 요청 빌드 | ✅ 완료 | `..._all_stocks_fixed/` 1,634건 |
| 버그영향 273건 LLM 재생성 | ✅ 완료 | 271건 배치(`regen_outputs.jsonl`) + 잔존 2건 동기(`regen_outputs_extra.jsonl`) |
| 273건 → outputs_all 병합 | ✅ 완료 | `merge_regen_unit_fix_into_outputs.py` 실행, 백업 `outputs_all.before_unit_fix.jsonl` 생성 |
| 정정 요청 기준 최종 오딧 | ✅ 완료 | `audit_final` **1,634/1,634 PASS (100%)**, accepted 1,629 / rejected 5(모델 자체거부) |
| 비현실 수치(200조+) 잔존 스캔 | ✅ 0건 | 한솔 3,684억·KMW 6,829억 등 정상 확인 |

> **최종 데이터셋**: `data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl` (1,634건, 단위 버그 전량 정정). 다음 단계는 Step 9(최종 출력 파일 조인) 또는 게임 데이터 반영.

### 배치 완료
- v4.2 전 종목 배치 55청크 전량 완료 → `pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl` **1,634건** (누락 0, 에러 0)
- 재개 중 chunk 45 출력이 비어 있던 문제는 OpenAI batch에서 재다운로드해 복구
- 주의: chunks_30의 일부 state.json에 옛 경로(`/Users/hgs/Desktop/IISE CD/...`)가 박혀 있어 병합에서 누락됐었음. 전체 state 경로를 현재 경로(`IISE-CD/data-pipeline/...`)로 정정함.

### 매출액 단위 버그 (중요)
`pr05f_extract_dart_disclosure_detail_facts.py`의 재무 수치 포매터가 '매출액또는손익구조변동' 공시를 **항상 천원 단위로 가정**했으나, 실제 단위는 종목마다 다름(천원 989건 / **원 284건**). 원 단위 42개 종목은 매출·이익이 ×1000 부풀려짐(예: 서울반도체 953조 → 실제 9,538억).

**코드 수정 (완료):**
1. `_detect_statement_unit()` — 단위 표기(`단위: 원/천원/백만원`)를 `변동내용`~`대규모법인여부` **섹션 전체**에서 검색(이전엔 60자 윈도우라 표기가 멀리 있는 공시 누락). 표기 없으면 다수인 천원 가정.
2. `_sanity_adjust_unit(sales_value, unit)` — 헤더 단위로 계산한 매출액이 비현실적으로 크면(>300조, 합법 최대 현대차 143조) ÷1000 단계 보정. 헤더가 아예 틀린 케이스(한솔: 천원이라 쓰고 값은 원) 대응. 단위를 키우는 방향으론 절대 안 바꿈 → 합법 대형주(현대차 142조·SK 134조) 보존.
3. `_format_krw_amount(value, unit_to_won)` — 원→억(÷10^8) 변환.

**전파 (무료, 완료):** 공시 상세 팩트 CSV 재생성 → 브리프 재빌드 완료. 원본 대비 `detail_source_facts`가 바뀐 요청은 **273건**.

**재생성 (LLM, 완료):**
- 271건(1차 원-헤더 정정분): 배치 러너 `scripts/processors/run_v4_2_regen_unit_fix.py` → `pr06a_regen_v4_2_unit_fix/regen_outputs.jsonl`
- 잔존 2건(`STOCK_BUNDLE_002113` 한솔 368조→3,684억, `STOCK_BUNDLE_003122` KMW 682조→6,829억): 크기 가드 적용 후 새로 검출되어 동기 API로 생성 → `regen_outputs_extra.jsonl` (regen_outputs.jsonl에 합쳐 273건)

**병합 (완료):** `scripts/processors/merge_regen_unit_fix_into_outputs.py` 실행 → 273건 교체, 백업 `outputs_all.before_unit_fix.jsonl` 생성.

**오딧 주의:** 재생성분은 정정 수치를 담으므로 **반드시 정정된 요청 파일** 기준으로 오딧해야 함(numeric_leak는 요청의 detail_source_facts와 비교). 정정 요청: `pr06a_full_requests_v4_2_all_stocks_fixed/stock_news_sample_requests.jsonl` (1,634건, 동일 custom_id).
```
python scripts/processors/audit_v4_2_full_outputs.py \
  --requests-jsonl data/interim/pr06a_full_requests_v4_2_all_stocks_fixed/stock_news_sample_requests.jsonl \
  --outputs-jsonl data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl \
  --output-dir data/interim/pr06a_full_outputs_v4_2_all_stocks/audit_final
```

### 오딧 이력 (참고)
| 오딧 디렉토리 | 대상 출력 | PASS | FAIL | pass_rate |
|---|---|---|---|---|
| `audit_all` | 960 (32청크 시점) | 960 | 674(미생성 포함) | — |
| `audit_completed_so_far` | 1,560 | 1,560 | 74(미생성) | 95.5% |
| `audit_premerge_baseline` | 1,634 | 1,370 | 264 | 83.8% |
| **`audit_final`** | **1,634 (병합 후)** | **1,634** | **0** | **100.0%** |

> `audit_final`이 최종 결과: 273건 정정 병합 + 정정 요청 파일(`..._fixed`) 기준으로 1,634/1,634 PASS. accepted 1,629 / rejected 5(모델 자체거부).

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
