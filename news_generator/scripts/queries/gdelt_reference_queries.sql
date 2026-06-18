-- ============================================================
-- GDELT BigQuery 수동 쿼리 모음
-- gdelt-bq 공개 데이터셋 직접 조회용
-- GCP Console (console.cloud.google.com) → BigQuery에서 실행
-- ============================================================


-- ──────────────────────────────────────────────────────────────
-- 1. 한국어 경제 기사 수집 (월별 파티션 예시: 2022-01)
-- ──────────────────────────────────────────────────────────────
SELECT
  SUBSTR(CAST(DATE AS STRING), 1, 8)                       AS ref_date,
  PARSE_DATETIME(
    '%Y%m%d%H%M%S', CAST(DATE AS STRING)
  )                                                        AS published_at,
  SourceCommonName                                         AS source_name,
  REGEXP_EXTRACT(DocumentIdentifier,
    r'https?://(?:www\.)?([^/]+)')                         AS domain,
  DocumentIdentifier                                       AS url,
  REGEXP_EXTRACT(TranslationInfo, r'srclc:([a-z]+)')       AS lang_code,
  -- 테마 배열 (첫 25개만)
  ARRAY(
    SELECT TRIM(SPLIT(t, ',')[OFFSET(0)])
    FROM UNNEST(SPLIT(IFNULL(V2Themes, ''), ';')) AS t
    WHERE TRIM(t) != ''
    LIMIT 25
  )                                                        AS themes,
  -- 엔티티
  ARRAY(
    SELECT TRIM(SPLIT(p, ',')[OFFSET(0)])
    FROM UNNEST(SPLIT(IFNULL(V2Persons, ''), ';')) AS p
    WHERE TRIM(p) != ''
    LIMIT 20
  )                                                        AS persons,
  ARRAY(
    SELECT TRIM(SPLIT(o, ',')[OFFSET(0)])
    FROM UNNEST(SPLIT(IFNULL(V2Organizations, ''), ';')) AS o
    WHERE TRIM(o) != ''
    LIMIT 20
  )                                                        AS organizations,
  -- 감성 점수 (7개 값: tone, pos, neg, polarity, activity, selfrefs, wordcount)
  CAST(SPLIT(IFNULL(V2Tone,''),',')[SAFE_OFFSET(0)] AS FLOAT64)  AS tone,
  CAST(SPLIT(IFNULL(V2Tone,''),',')[SAFE_OFFSET(1)] AS FLOAT64)  AS tone_pos,
  CAST(SPLIT(IFNULL(V2Tone,''),',')[SAFE_OFFSET(2)] AS FLOAT64)  AS tone_neg,

FROM `gdelt-bq.gdeltv2.gkg`
WHERE
  -- 기간 (INTEGER 형식의 DATE 컬럼)
  DATE >= 20220101000000
  AND DATE <  20220201000000
  -- 언어: 한국어 우선
  AND (
    REGEXP_EXTRACT(TranslationInfo, r'srclc:([a-z]+)') = 'kor'
  )
  -- 테마 필터
  AND (
    V2Themes LIKE '%ECON_%'
    OR V2Themes LIKE '%ENV_OIL%'
    OR V2Themes LIKE '%ENV_GAS%'
    OR V2Themes LIKE '%ECON_INTEREST_RATES%'
    OR V2Themes LIKE '%ECON_INFLATION%'
    OR V2Themes LIKE '%ECON_STOCKMARKET%'
    OR V2Themes LIKE '%SANCTION%'
    OR V2Themes LIKE '%FINANCIAL_CRISIS%'
    OR V2Themes LIKE '%CENTRAL_BANK%'
    OR V2Themes LIKE '%WB_1925%'  -- Financial Markets
  )
  -- 저품질 도메인 제외
  AND DocumentIdentifier NOT LIKE '%blogspot.com%'
  AND DocumentIdentifier NOT LIKE '%wordpress.com%'
  AND DocumentIdentifier IS NOT NULL
ORDER BY published_at
LIMIT 50000;


-- ──────────────────────────────────────────────────────────────
-- 2. 연간 수집량 확인 (실행 전 비용 추정용)
-- ──────────────────────────────────────────────────────────────
SELECT
  SUBSTR(CAST(DATE AS STRING), 1, 6) AS year_month,
  COUNT(*) AS total_rows,
  COUNTIF(
    REGEXP_EXTRACT(TranslationInfo, r'srclc:([a-z]+)') = 'kor'
  ) AS kor_rows,
  COUNTIF(V2Themes LIKE '%ECON_%') AS econ_theme_rows,
FROM `gdelt-bq.gdeltv2.gkg`
WHERE
  DATE >= 20140101000000
  AND DATE <  20240101000000
GROUP BY year_month
ORDER BY year_month;


-- ──────────────────────────────────────────────────────────────
-- 3. GDELT Events - 경제·제재 이벤트 (2022년)
-- ──────────────────────────────────────────────────────────────
SELECT
  CAST(Day AS STRING)              AS event_date,
  GlobalEventID                    AS event_id,
  Actor1Name                       AS actor1,
  Actor1CountryCode                AS actor1_country,
  Actor2Name                       AS actor2,
  Actor2CountryCode                AS actor2_country,
  EventCode                        AS cameo_code,
  EventRootCode                    AS cameo_root,
  GoldsteinScale                   AS goldstein,
  NumArticles                      AS num_articles,
  AvgTone                          AS avg_tone,
  SOURCEURL                        AS url,
FROM `gdelt-bq.gdeltv2.events`
WHERE
  Day >= 20220101
  AND Day <  20230101
  -- 경제·제재·외교 관련 CAMEO 루트 코드
  AND EventRootCode IN ('03','04','06','07','08','10','11','12','13','15','17','18')
  -- 다수 기사로 보도된 이벤트만
  AND NumArticles >= 3
  -- 한국 관련 이벤트 우선
  AND (
    Actor1CountryCode = 'KOR'
    OR Actor2CountryCode = 'KOR'
    OR ActionGeo_CountryCode = 'KOR'
  )
ORDER BY Day, NumArticles DESC
LIMIT 100000;


-- ──────────────────────────────────────────────────────────────
-- 4. GKG + Events 조인: URL 기준으로 이벤트 정보 부착
-- ──────────────────────────────────────────────────────────────
WITH gkg_articles AS (
  SELECT
    SUBSTR(CAST(g.DATE AS STRING), 1, 8)   AS ref_date,
    g.SourceCommonName                      AS source_name,
    g.DocumentIdentifier                    AS url,
    REGEXP_EXTRACT(g.DocumentIdentifier,
      r'https?://(?:www\.)?([^/]+)')        AS domain,
    REGEXP_EXTRACT(g.TranslationInfo,
      r'srclc:([a-z]+)')                    AS lang_code,
    g.V2Themes                              AS themes_raw,
    CAST(SPLIT(IFNULL(g.V2Tone,''),',')[SAFE_OFFSET(0)] AS FLOAT64) AS tone,
  FROM `gdelt-bq.gdeltv2.gkg` g
  WHERE
    g.DATE >= 20220601000000
    AND g.DATE <  20220701000000
    AND REGEXP_EXTRACT(g.TranslationInfo, r'srclc:([a-z]+)') = 'kor'
    AND g.V2Themes LIKE '%ECON_%'
),

event_mentions AS (
  SELECT
    m.MentionIdentifier              AS url,
    e.GlobalEventID                  AS event_id,
    e.EventCode                      AS cameo_code,
    e.EventRootCode                  AS cameo_root,
    e.GoldsteinScale                 AS goldstein,
    e.NumArticles                    AS event_num_articles,
    e.AvgTone                        AS event_avg_tone,
  FROM `gdelt-bq.gdeltv2.eventmentions` m
  JOIN `gdelt-bq.gdeltv2.events` e
    ON m.GlobalEventID = e.GlobalEventID
  WHERE
    e.Day >= 20220601
    AND e.Day <  20220701
    AND e.EventRootCode IN ('03','06','07','10','13','17')
)

SELECT
  a.*,
  em.event_id,
  em.cameo_code,
  em.cameo_root,
  em.goldstein,
  em.event_num_articles,
  em.event_avg_tone,
FROM gkg_articles a
LEFT JOIN event_mentions em
  ON a.url = em.url
ORDER BY a.ref_date, a.domain
LIMIT 200000;


-- ──────────────────────────────────────────────────────────────
-- 5. 비용 추정 쿼리 (DRY RUN - 실제 실행 전)
-- BigQuery Console에서 "실행" 전에 오른쪽 상단 "쿼리 설정 > 
-- 드라이 런 결과" 확인 또는 아래 bq CLI 명령 사용:
--
--   bq query --dry_run --use_legacy_sql=false < query.sql
--
-- gdelt-bq.gdeltv2.gkg 테이블 크기:
--   2014~2023년 전체: ~20TB
--   한국어 필터 후:   ~200~500GB (추정)
--   월별:            ~1~3GB (추정)
-- BigQuery 무료 티어: 월 1TB
-- 유료: $5/TB → 전체 수집 시 $10~50 예상
-- ──────────────────────────────────────────────────────────────
