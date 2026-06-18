"""
네이버 뉴스 검색 API 수집기 (2021~2023)
-----------------------------------------
GDELT 커버리지가 부족한 2021~2023년 한국 뉴스를
네이버 뉴스 검색 API로 보완합니다.

API 제한:
    - 1일 호출 한도: 25,000회
    - 1회 최대 결과: 100건
    - 검색 결과 최대: 키워드당 1,000건 (start 파라미터)
    - 완전 무료

사전 준비:
    환경변수 설정:
        export NAVER_CLIENT_ID="발급받은_Client_ID"
        export NAVER_CLIENT_SECRET="발급받은_Client_Secret"
"""

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import requests
from requests.adapters import HTTPAdapter, Retry

from config import KR_KEYWORD_GROUPS, OUTPUT_CONFIG
from processors.dedup_processor import ArticleProcessor

logger = logging.getLogger(__name__)

NAVER_NEWS_ENDPOINT = "https://openapi.naver.com/v1/search/news.json"

# ──────────────────────────────────────────────────────────────
# HTTP 세션
# ──────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    client_id     = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        raise EnvironmentError(
            "환경변수가 설정되지 않았습니다.\n"
            "  export NAVER_CLIENT_ID='your_client_id'\n"
            "  export NAVER_CLIENT_SECRET='your_client_secret'"
        )

    session = requests.Session()
    retry = Retry(total=5, backoff_factor=2.0, status_forcelist=[429, 500, 502, 503])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "X-Naver-Client-Id":     client_id,
        "X-Naver-Client-Secret": client_secret,
        "User-Agent":            "NaverNews-Research/1.0",
    })
    return session


# ──────────────────────────────────────────────────────────────
# 날짜 파싱
# ──────────────────────────────────────────────────────────────

def _parse_pub_date(pub_date_str: str) -> Optional[datetime]:
    """
    네이버 API 날짜 형식 파싱.
    예: "Mon, 03 Jan 2022 09:00:00 +0900"
    """
    try:
        # RFC 2822 형식
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub_date_str).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.strptime(pub_date_str[:25], "%a, %d %b %Y %H:%M:%S")
        except Exception:
            return None


def _clean_html(text: str) -> str:
    """HTML 태그 및 엔티티 제거."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&quot;", '"').replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&apos;", "'").replace("&#39;", "'")
    return text.strip()


def _extract_domain(url: str) -> str:
    m = re.match(r"https?://(?:(?:www|m)\.)?([^/:]+)", url)
    return m.group(1) if m else ""


# ──────────────────────────────────────────────────────────────
# 수집기
# ──────────────────────────────────────────────────────────────

class NaverNewsCollector:
    """네이버 뉴스 검색 API 수집기."""

    MAX_DISPLAY   = 100    # 1회 최대 결과 수
    MAX_START     = 1000   # API 최대 start 값 (1,000건 제한)
    CALL_DELAY    = 0.12   # 호출 간격 (초) → 25,000회/일 여유있게 유지

    # 제외 도메인
    EXCLUDE_DOMAINS = {
        "news.naver.com", "n.news.naver.com",  # 네이버 자체 (원본 아님)
        "news.daum.net", "v.daum.net",
        "news.nate.com",
        "koreatimes.com", "koreaherald.com",
    }

    def __init__(self):
        self.session    = _make_session()
        self.output_dir = Path(OUTPUT_CONFIG["output_dir"]) / "naver"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.processor  = ArticleProcessor()

    def collect_period(
        self,
        start: date,
        end: date,
        skip_existing: bool = True,
    ) -> None:
        """
        지정 기간을 월 단위로 나눠 키워드 그룹별 수집.
        결과를 월별 parquet로 저장.
        """
        months = self._month_ranges(start, end)
        logger.info("네이버 뉴스 수집: %s ~ %s (%d개월)", start, end, len(months))

        for month_start, month_end in months:
            ym = month_start.strftime("%Y%m")
            out_path = self.output_dir / f"naver_{ym}.parquet"

            if skip_existing and out_path.exists():
                logger.info("[%s] 스킵 (완료)", ym)
                continue

            all_articles: list[dict] = []
            seen_urls: set[str] = set()

            for group_name, keywords in KR_KEYWORD_GROUPS.items():
                logger.info("[%s] 그룹 수집 중: %s", ym, group_name)
                for keyword in keywords:
                    articles = self._search_keyword(
                        keyword, month_start, month_end
                    )
                    for art in articles:
                        url = art.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            art["keyword_group"] = group_name
                            art["keyword"]       = keyword
                            all_articles.append(art)
                    time.sleep(self.CALL_DELAY)

            if not all_articles:
                logger.warning("[%s] 수집 결과 없음", ym)
                continue

            # DataFrame 변환
            df = pd.DataFrame(all_articles)

            # 후처리 (연관성 판정 + 중복 제거)
            df = self.processor.process(df)

            if df.empty:
                logger.warning("[%s] 처리 후 결과 없음", ym)
                continue

            df.to_parquet(out_path, index=False, engine="pyarrow")
            logger.info("[%s] %d건 저장 → %s", ym, len(df), out_path)

    def _search_keyword(
        self,
        keyword: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """키워드로 네이버 뉴스 검색 (최대 1,000건)."""
        articles = []
        start_idx = 1

        while start_idx <= self.MAX_START:
            params = {
                "query":   keyword,
                "display": self.MAX_DISPLAY,
                "start":   start_idx,
                "sort":    "date",   # 날짜순
            }

            try:
                resp = self.session.get(
                    NAVER_NEWS_ENDPOINT, params=params, timeout=15
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("[%s] API 오류: %s", keyword, e)
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                pub_dt = _parse_pub_date(item.get("pubDate", ""))

                # 날짜 필터
                if pub_dt:
                    pub_date = pub_dt.date()
                    if pub_date < start_date or pub_date > end_date:
                        continue

                url = item.get("link") or item.get("originallink", "")

                # 제외 도메인
                domain = _extract_domain(url)
                if domain in self.EXCLUDE_DOMAINS:
                    # originallink로 대체
                    orig = item.get("originallink", "")
                    orig_domain = _extract_domain(orig)
                    if orig and orig_domain not in self.EXCLUDE_DOMAINS:
                        url    = orig
                        domain = orig_domain
                    else:
                        continue

                title = _clean_html(item.get("title", ""))
                desc  = _clean_html(item.get("description", ""))

                articles.append({
                    "ref_date":    pub_dt.strftime("%Y%m%d") if pub_dt else "",
                    "published_at": pub_dt.strftime("%Y%m%d%H%M%S") if pub_dt else "",
                    "title":       title,
                    "description": desc,
                    "url":         url,
                    "domain":      domain,
                    "source_name": domain,
                    "lang_code":   "kor",
                    "themes_raw":  "",   # 네이버 API는 테마 없음
                    "persons_raw": "",
                    "orgs_raw":    "",
                    "tone_score":  None,
                })

            # 다음 페이지
            total = data.get("total", 0)
            start_idx += self.MAX_DISPLAY
            if start_idx > min(total, self.MAX_START):
                break

            time.sleep(self.CALL_DELAY)

        return articles

    @staticmethod
    def _month_ranges(start: date, end: date) -> list[tuple[date, date]]:
        result = []
        cur = start.replace(day=1)
        while cur <= end:
            if cur.month == 12:
                nxt = cur.replace(year=cur.year + 1, month=1)
            else:
                nxt = cur.replace(month=cur.month + 1)
            last = nxt - timedelta(days=1)
            result.append((cur, min(last, end)))
            cur = nxt
        return result


# ──────────────────────────────────────────────────────────────
# 네이버 + GDELT 병합 유틸리티
# ──────────────────────────────────────────────────────────────

def merge_naver_gdelt(
    gdelt_dir: str | Path = "./output",
    naver_dir: str | Path = "./output/naver",
    output_dir: str | Path = "./output/merged",
) -> None:
    """
    GDELT parquet + 네이버 parquet를 월별로 병합.
    중복 URL 제거 후 저장.
    """
    gdelt_dir  = Path(gdelt_dir)
    naver_dir  = Path(naver_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    processor = ArticleProcessor()

    # 모든 연월 목록
    all_yms = set()
    for f in gdelt_dir.glob("gkg_*.parquet"):
        all_yms.add(f.stem.replace("gkg_", ""))
    for f in naver_dir.glob("naver_*.parquet"):
        all_yms.add(f.stem.replace("naver_", ""))

    for ym in sorted(all_yms):
        out_path   = output_dir / f"merged_{ym}.parquet"
        gdelt_path = gdelt_dir / f"gkg_{ym}.parquet"
        naver_path = naver_dir / f"naver_{ym}.parquet"

        frames = []
        if gdelt_path.exists():
            frames.append(pd.read_parquet(gdelt_path))
        if naver_path.exists():
            frames.append(pd.read_parquet(naver_path))

        if not frames:
            continue

        combined = pd.concat(frames, ignore_index=True)
        # URL 기준 중복 제거
        combined = combined.drop_duplicates(subset="url", keep="first")
        combined.to_parquet(out_path, index=False, engine="pyarrow")
        logger.info("[%s] 병합 완료: %d건 → %s", ym, len(combined), out_path)

    logger.info("병합 완료: %s", output_dir)
