"""
GDELT GKG v1 수집기 (2014년 전용)
-----------------------------------
GKG v2가 시작되기 전(~2015.02.18)의 데이터를 수집합니다.

GKG v1 특징:
    - 일별 파일 (v2는 15분 단위)
    - URL: http://data.gdeltproject.org/gkg/YYYYMMDD.gkg.csv.zip
    - 언어 태그 없음 → .co.kr 도메인으로 한국어 판별
    - 엔티티(인물/기관) 정보 덜 상세
    - 감성점수 있음 (6개 값)

GKG v1 컬럼 (탭 구분):
    0: DATE         YYYYMMDD
    1: NUMARTS      기사 수
    2: COUNTS       이벤트 카운트
    3: THEMES       세미콜론 구분 테마
    4: LOCATIONS    위치
    5: PERSONS      인물
    6: ORGANIZATIONS 기관
    7: TONE         콤마 구분 (avgTone,pos,neg,polarity,activity,selfRef)
    8: CAMEOEVENTIDS
    9: SOURCES      콤마 구분 매체명
    10: SOURCEURLS  공백 구분 URL 목록
"""

import csv
import io
import logging
import re
import time
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from tqdm import tqdm

from config import GKG_THEMES, OUTPUT_CONFIG

logger = logging.getLogger(__name__)

GDELT_GKG_V1_BASE = "http://data.gdeltproject.org/gkg"
GKG_V1_END        = date(2015, 2, 18)   # v1 마지막 날

_KR_URL_RE  = re.compile(r"https?://[^/]*\.(?:co\.kr|or\.kr|ne\.kr|go\.kr|ac\.kr|kr)/")
_THEMES_SET = set(GKG_THEMES)
_EXCLUDE_RE = re.compile(
    r"blogspot\.com|wordpress\.com|me2\.do|naver\.me"
    r"|koreatimes\.com|koreaherald\.com|arirang\.com"
    r"|news\.nate\.com|news\.naver\.com|news\.daum\.net"
    r"|v\.daum\.net|n\.news\.naver\.com"
)
_DOMAIN_RE  = re.compile(r"https?://(?:(?:www|m)\.)?([^/:]+)")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


# ──────────────────────────────────────────────────────────────
# 테마 필터
# ──────────────────────────────────────────────────────────────

def _has_target_theme(themes_str: str) -> bool:
    if not themes_str:
        return False
    row_themes = {t.strip() for t in themes_str.split(";") if t.strip()}
    return bool(row_themes & _THEMES_SET)


# ──────────────────────────────────────────────────────────────
# 파일 파싱
# ──────────────────────────────────────────────────────────────

def _parse_v1_bytes(raw_bytes: bytes, file_date: date) -> pd.DataFrame:
    """
    GKG v1 일별 파일 파싱.
    한 행에 여러 URL이 있으므로 URL별로 행을 분리합니다.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            content = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        try:
            import gzip
            with gzip.open(io.BytesIO(raw_bytes)) as gz:
                content = gz.read().decode("utf-8", errors="replace")
        except Exception:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    date_str  = file_date.strftime("%Y%m%d")
    rows_out  = []

    for raw_row in csv.reader(io.StringIO(content), delimiter="\t"):
        if len(raw_row) < 11:
            continue

        themes_str = raw_row[3]

        # 테마 필터
        if not _has_target_theme(themes_str):
            continue

        # tone 파싱
        tone_parts = raw_row[7].split(",") if len(raw_row) > 7 else []
        try:
            tone = float(tone_parts[0])
        except (ValueError, IndexError):
            tone = None

        persons_str = raw_row[5] if len(raw_row) > 5 else ""
        orgs_str    = raw_row[6] if len(raw_row) > 6 else ""
        sources_str = raw_row[9] if len(raw_row) > 9 else ""

        # SOURCEURLS: 공백으로 구분된 URL 목록
        source_urls_str = raw_row[10] if len(raw_row) > 10 else ""
        urls = [u.strip() for u in source_urls_str.split() if u.strip()]

        # 매체명: 콤마 구분
        sources = [s.strip() for s in sources_str.split(",") if s.strip()]

        for idx, url in enumerate(urls):
            # 한국어 URL만
            if not _KR_URL_RE.match(url):
                continue

            # 제외 도메인
            if _EXCLUDE_RE.search(url):
                continue

            dm = _DOMAIN_RE.match(url)
            domain = dm.group(1) if dm else ""

            # 매체명 매핑 (순서 기반)
            source_name = sources[idx] if idx < len(sources) else domain

            rows_out.append({
                "ref_date":    date_str,
                "published_at": date_str + "000000",
                "source_name": source_name,
                "domain":      domain,
                "url":         url,
                "lang_code":   "kor",
                "themes_raw":  themes_str,
                "persons_raw": persons_str,
                "orgs_raw":    orgs_str,
                "tone_score":  tone,
            })

    return pd.DataFrame(rows_out)


# ──────────────────────────────────────────────────────────────
# 수집기
# ──────────────────────────────────────────────────────────────

class GDELTGKGV1Collector:
    """GDELT GKG v1 일별 파일 수집기 (2014년 전용)."""

    DOWNLOAD_DELAY = 1.0
    RETRY_COUNT    = 3
    RETRY_DELAY    = 5

    def __init__(self):
        self.output_dir = Path(OUTPUT_CONFIG["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def collect(
        self,
        start: date = date(2014, 1, 1),
        end: date   = date(2014, 12, 31),
        skip_existing: bool = True,
    ) -> None:
        """
        월별 parquet 파일로 저장.
        v2와 동일한 파일명(gkg_YYYYMM.parquet) 사용 → 이후 파이프라인 호환.
        """
        end = min(end, GKG_V1_END)
        months = self._month_ranges(start, end)
        logger.info("GKG v1 수집 시작: %s ~ %s (%d개월)", start, end, len(months))

        for month_start, month_end in months:
            ym       = month_start.strftime("%Y%m")
            out_path = self.output_dir / f"gkg_{ym}.parquet"

            if skip_existing and out_path.exists():
                logger.info("[%s] 스킵 (완료)", ym)
                continue

            self._collect_month(month_start, month_end, out_path)

    def _collect_month(self, start: date, end: date, out_path: Path) -> None:
        """한 달치 일별 파일 수집."""
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        accumulated: list[pd.DataFrame] = []

        for day in tqdm(days, desc=f"  {start.strftime('%Y-%m')} (v1)", leave=True):
            chunk = self._download_day(day)
            if chunk is not None and not chunk.empty:
                accumulated.append(chunk)
            time.sleep(self.DOWNLOAD_DELAY)

        if accumulated:
            df = pd.concat(accumulated, ignore_index=True)
            df = df.drop_duplicates(subset="url", keep="first")
            df.to_parquet(out_path, index=False, engine="pyarrow")
            logger.info("[%s] %d건 저장 → %s", start.strftime("%Y%m"), len(df), out_path)
        else:
            logger.warning("[%s] 수집 결과 없음", start.strftime("%Y%m"))

    def _download_day(self, day: date) -> Optional[pd.DataFrame]:
        """단일 일별 파일 다운로드 + 파싱."""
        date_str = day.strftime("%Y%m%d")
        url      = f"{GDELT_GKG_V1_BASE}/{date_str}.gkg.csv.zip"

        for attempt in range(self.RETRY_COUNT):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                return _parse_v1_bytes(raw, day)
            except Exception as e:
                err = str(e)
                if "404" in err:
                    logger.debug("파일 없음 (404): %s", date_str)
                    return None
                if attempt < self.RETRY_COUNT - 1:
                    time.sleep(self.RETRY_DELAY * (attempt + 1))
                else:
                    logger.warning("다운로드 실패: %s — %s", date_str, e)
        return None

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
