"""
GDELT DOC API v2 수집기
-----------------------
키워드 기반 API 검색으로 보완적 수집에 활용합니다.
※ DOC API는 무료이지만 최근 3개월 데이터만 지원 → 주로 증분 수집 및 테스트용

2014~2023 전체 수집은 bigquery_collector.py 사용 권장.
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlencode, quote

import requests
from requests.adapters import HTTPAdapter, Retry

from config import (
    KR_KEYWORD_GROUPS, EN_KEYWORD_GROUPS,
    OUTPUT_CONFIG, DATE_START, DATE_END,
    LANG_PRIMARY, LANG_SECONDARY,
)

logger = logging.getLogger(__name__)

GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# DOC API 응답 필드
ARTICLE_FIELDS = [
    "url", "url_mobile", "title", "seendate",
    "socialimage", "domain", "language", "sourcecountry",
]


# ──────────────────────────────────────────────────────────────
# HTTP 세션
# ──────────────────────────────────────────────────────────────

def _make_session(retries: int = 5) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "GDELT-Research-Collector/1.0"})
    return session


SESSION = _make_session()


# ──────────────────────────────────────────────────────────────
# 쿼리 생성
# ──────────────────────────────────────────────────────────────

def build_query_string(keywords: list[str], lang: str = "kor") -> str:
    """
    키워드 리스트를 GDELT DOC API 쿼리 문자열로 변환.
    - 공백 포함 키워드는 따옴표 처리
    - lang 파라미터로 언어 필터
    """
    terms = []
    for kw in keywords:
        if " " in kw:
            terms.append(f'"{kw}"')
        else:
            terms.append(kw)
    query = " OR ".join(terms)
    return f"({query}) sourcelang:{lang}"


def _dt_fmt(d: date) -> str:
    """GDELT API datetime 형식: YYYYMMDDHHMMSS"""
    return d.strftime("%Y%m%d") + "000000"


# ──────────────────────────────────────────────────────────────
# 수집기
# ──────────────────────────────────────────────────────────────

class GDELTDocAPICollector:
    """GDELT DOC API v2 키워드 기반 수집기."""

    MAX_RECORDS_PER_CALL = 250       # API 상한
    CALL_INTERVAL_SEC    = 5.0       # 호출 간격 (rate limit 준수)
    WINDOW_DAYS          = 3         # 1회 API 호출 기간 창

    def __init__(self):
        self.output_dir = Path(OUTPUT_CONFIG["output_dir"]) / "docapi"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def collect_by_keyword_group(
        self,
        group_name: str,
        keywords: list[str],
        lang: str = "kor",
        start: date = DATE_START,
        end: date = DATE_END,
    ) -> list[dict]:
        """
        단일 키워드 그룹에 대해 전체 기간을 WINDOW_DAYS 창으로 슬라이딩하며 수집.
        결과를 JSONL 파일로 저장하고 리스트로 반환.
        """
        all_articles: list[dict] = []
        out_path = self.output_dir / f"{group_name}_{lang}.jsonl"

        # 이미 수집한 URL 로드 (증분 수집)
        seen_urls: set[str] = set()
        if out_path.exists():
            with open(out_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        seen_urls.add(json.loads(line)["url"])
                    except Exception:
                        pass

        query = build_query_string(keywords, lang)
        windows = list(self._date_windows(start, end))

        logger.info(
            "[%s/%s] 수집 시작: %d개 창, 쿼리=%s",
            group_name, lang, len(windows), query[:80]
        )

        with open(out_path, "a", encoding="utf-8") as fout:
            for win_start, win_end in windows:
                articles = self._fetch_articles(query, win_start, win_end)

                new_count = 0
                for art in articles:
                    url = art.get("url", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    # 메타 추가
                    art["keyword_group"] = group_name
                    art["lang_filter"] = lang
                    fout.write(json.dumps(art, ensure_ascii=False) + "\n")
                    all_articles.append(art)
                    new_count += 1

                if new_count:
                    logger.debug(
                        "  [%s~%s] +%d건 (누적 %d)",
                        win_start, win_end, new_count, len(all_articles),
                    )
                time.sleep(self.CALL_INTERVAL_SEC)

        logger.info("[%s/%s] 완료: 총 %d건 → %s", group_name, lang, len(all_articles), out_path)
        return all_articles

    def collect_all_groups(
        self,
        include_english: bool = True,
        start: date = DATE_START,
        end: date = DATE_END,
    ) -> None:
        """모든 키워드 그룹 수집."""
        # 한국어
        for group, keywords in KR_KEYWORD_GROUPS.items():
            self.collect_by_keyword_group(group, keywords, lang="kor", start=start, end=end)

        # 영어 (글로벌 보완)
        if include_english:
            for group, keywords in EN_KEYWORD_GROUPS.items():
                self.collect_by_keyword_group(
                    f"en_{group}", keywords, lang="eng", start=start, end=end
                )

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _fetch_articles(
        self,
        query: str,
        start: date,
        end: date,
    ) -> list[dict]:
        """DOC API 단일 호출, 최대 250건 반환."""
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": self.MAX_RECORDS_PER_CALL,
            "startdatetime": _dt_fmt(start),
            "enddatetime": _dt_fmt(end),
            "format": "json",
            "sort": "DateDesc",
        }
        try:
            resp = SESSION.get(GDELT_DOC_ENDPOINT, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("articles") or []
            return [self._normalize_article(a) for a in articles]
        except requests.HTTPError as e:
            logger.warning("HTTP 오류 [%s~%s]: %s", start, end, e)
            return []
        except Exception as e:
            logger.error("수집 실패 [%s~%s]: %s", start, end, e)
            return []

    @staticmethod
    def _normalize_article(raw: dict) -> dict:
        """API 응답 → 표준 스키마 변환."""
        return {
            "url":         raw.get("url", ""),
            "title":       raw.get("title", ""),
            "domain":      raw.get("domain", ""),
            "language":    raw.get("language", ""),
            "source_country": raw.get("sourcecountry", ""),
            "published_at": raw.get("seendate", ""),   # "YYYYMMDDTHHMMSSZ"
            "social_image": raw.get("socialimage", ""),
        }

    def _date_windows(
        self, start: date, end: date
    ) -> Generator[tuple[date, date], None, None]:
        """start~end를 WINDOW_DAYS 간격으로 슬라이딩."""
        cur = start
        while cur <= end:
            win_end = min(cur + timedelta(days=self.WINDOW_DAYS - 1), end)
            yield cur, win_end
            cur = win_end + timedelta(days=1)


# ──────────────────────────────────────────────────────────────
# JSONL → Parquet 변환 유틸리티
# ──────────────────────────────────────────────────────────────

def jsonl_to_parquet(jsonl_path: str | Path, out_path: Optional[str | Path] = None) -> Path:
    """여러 JSONL 파일을 읽어 단일 Parquet으로 저장."""
    import pandas as pd

    jsonl_path = Path(jsonl_path)
    out_path = Path(out_path) if out_path else jsonl_path.with_suffix(".parquet")

    rows = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    df = pd.DataFrame(rows)
    df.to_parquet(out_path, index=False, engine="pyarrow")
    logger.info("변환 완료: %d건 → %s", len(df), out_path)
    return out_path
