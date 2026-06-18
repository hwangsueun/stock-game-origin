"""
GDELT GKG 직접 다운로드 수집기 (개선판)
-----------------------------------------
- BigQuery 없이 무료로 2021~2023년 수집
- GDELT 서버에서 15분 단위 GKG v2 파일 직접 다운로드
- 한국어 기사만 필터링
- 월별 parquet 저장 + 일별 체크포인트로 중단/재개 지원

※ GKG v2는 2015년 2월 19일부터 시작.
"""

import csv
import gzip
import io
import logging
import re
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Generator, Optional

import pandas as pd
from tqdm import tqdm

from config import (
    GKG_THEMES, DATE_START, DATE_END,
    OUTPUT_CONFIG, EXCLUDE_DOMAINS,
)

logger = logging.getLogger(__name__)

GDELT_BASE = "http://data.gdeltproject.org/gdeltv2"
GKG_V2_START = date(2015, 2, 19)  # GKG v2 시작일

# ──────────────────────────────────────────────────────────────
# GKG v2 컬럼
# ──────────────────────────────────────────────────────────────
GKG_COLUMNS = [
    "GKGRECORDID", "DATE", "SourceCollectionIdentifier",
    "SourceCommonName", "DocumentIdentifier",
    "Counts", "V2Counts", "Themes", "V2Themes",
    "Locations", "V2Locations", "Persons", "V2Persons",
    "Organizations", "V2Organizations", "V2Tone",
    "Dates", "GCAM", "SharingImage", "RelatedImages",
    "SocialImageEmbeds", "SocialVideoEmbeds", "Quotations",
    "AllNames", "Amounts", "TranslationInfo", "Extras",
]

KEEP_COLUMNS = [
    "DATE", "SourceCommonName", "DocumentIdentifier",
    "V2Themes", "V2Persons", "V2Organizations", "V2Tone",
    "TranslationInfo",
]
KEEP_INDICES = [GKG_COLUMNS.index(c) for c in KEEP_COLUMNS]

IDX_DATE      = KEEP_INDICES[0]
IDX_SOURCE    = KEEP_INDICES[1]
IDX_URL       = KEEP_INDICES[2]
IDX_THEMES    = KEEP_INDICES[3]
IDX_PERSONS   = KEEP_INDICES[4]
IDX_ORGS      = KEEP_INDICES[5]
IDX_TONE      = KEEP_INDICES[6]
IDX_TRANSINFO = KEEP_INDICES[7]
MAX_IDX       = max(KEEP_INDICES)

_EXCLUDE_RE = re.compile(
    r"blogspot\.com|wordpress\.com|me2\.do|naver\.me"
    r"|koreatimes\.com|koreaherald\.com|arirang\.com"
    r"|news\.nate\.com|news\.naver\.com|news\.daum\.net"
    r"|v\.daum\.net|n\.news\.naver\.com"
)
_DOMAIN_RE  = re.compile(r"https?://(?:(?:www|m)\.)?([^/:]+)")
_LANG_RE    = re.compile(r"srclc:([a-z]+)")
_KR_URL_RE  = re.compile(r"https?://[^/]*\.(?:co\.kr|or\.kr|ne\.kr|go\.kr|ac\.kr|kr)/")
_THEMES_SET = set(GKG_THEMES)


def _is_korean(url: str, trans_info: str) -> bool:
    """
    한국어 기사 판별.
    - 2015~2020: TranslationInfo의 srclc:kor 태그
    - 2021~    : GDELT가 태그를 붙이지 않으므로 .kr 도메인으로 판별
    """
    if trans_info:
        m = _LANG_RE.search(trans_info)
        if m:
            return m.group(1) == "kor"
    return bool(_KR_URL_RE.match(url))


# ──────────────────────────────────────────────────────────────
# URL 생성
# ──────────────────────────────────────────────────────────────

def generate_file_urls(start: date, end: date) -> Generator[tuple[datetime, str], None, None]:
    """15분 간격 GKG 파일 URL 생성."""
    effective_start = max(start, GKG_V2_START)
    if start < GKG_V2_START:
        logger.warning("GKG v2는 2015-02-19부터. %s → %s 조정", start, GKG_V2_START)

    cur = datetime.combine(effective_start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    while cur <= end_dt:
        ts = cur.strftime("%Y%m%d%H%M%S")
        yield cur, f"{GDELT_BASE}/{ts}.gkg.csv.zip"
        cur += timedelta(minutes=15)


def count_files(start: date, end: date) -> int:
    effective_start = max(start, GKG_V2_START)
    delta = datetime.combine(end, datetime.max.time()) - \
            datetime.combine(effective_start, datetime.min.time())
    return max(0, int(delta.total_seconds() / 900))


# ──────────────────────────────────────────────────────────────
# 파싱
# ──────────────────────────────────────────────────────────────

def _has_target_theme(v2themes: str) -> bool:
    if not v2themes:
        return False
    row_themes = {t.split(",")[0].strip() for t in v2themes.split(";") if t.strip()}
    return bool(row_themes & _THEMES_SET)


def _parse_gkg_bytes(raw_bytes: bytes) -> pd.DataFrame:
    """GKG zip 바이트 → 한국어 경제 기사 DataFrame."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            content = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        try:
            with gzip.open(io.BytesIO(raw_bytes)) as gz:
                content = gz.read().decode("utf-8", errors="replace")
        except Exception:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    rows = []
    for raw_row in csv.reader(io.StringIO(content), delimiter="\t"):
        if len(raw_row) <= MAX_IDX:
            continue

        url = raw_row[IDX_URL]
        if not url or not _is_korean(url, raw_row[IDX_TRANSINFO]):
            continue

        v2themes = raw_row[IDX_THEMES]
        if not _has_target_theme(v2themes):
            continue

        if _EXCLUDE_RE.search(url):
            continue

        dm = _DOMAIN_RE.match(url)
        domain = dm.group(1) if dm else ""

        try:
            tone = float(raw_row[IDX_TONE].split(",")[0])
        except (ValueError, IndexError):
            tone = None

        date_str = raw_row[IDX_DATE]
        rows.append({
            "ref_date":     date_str[:8],
            "published_at": date_str,
            "source_name":  raw_row[IDX_SOURCE],
            "domain":       domain,
            "url":          url,
            "lang_code":    "kor",
            "themes_raw":   v2themes,
            "persons_raw":  raw_row[IDX_PERSONS],
            "orgs_raw":     raw_row[IDX_ORGS],
            "tone_score":   tone,
        })

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────
# 수집기
# ──────────────────────────────────────────────────────────────

class GDELTBulkDownloader:
    """GDELT GKG 직접 다운로드 수집기 (비용 0원)."""

    DOWNLOAD_DELAY_SEC = 0.3
    RETRY_COUNT        = 3
    RETRY_DELAY_SEC    = 5

    def __init__(self):
        self.output_dir     = Path(OUTPUT_CONFIG["output_dir"])
        self.checkpoint_dir = self.output_dir / "checkpoint"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # ── 공개 API ────────────────────────────────────────────────

    def collect(
        self,
        start: date = DATE_START,
        end: date = DATE_END,
        skip_existing: bool = True,
    ) -> None:
        """
        월별 parquet 파일로 수집.
        - 완료된 월: 자동 스킵
        - 월 중간 중단 시: 일별 체크포인트에서 재개 (완료 일자 재다운로드 없음)
        """
        effective_start = max(start, GKG_V2_START)
        months = self._month_ranges(effective_start, end)
        logger.info("GKG 다운로드 시작: %s ~ %s (%d개월)", effective_start, end, len(months))

        for month_start, month_end in months:
            ym = month_start.strftime("%Y%m")
            out_path = self.output_dir / f"gkg_{ym}.parquet"

            if skip_existing and out_path.exists():
                logger.info("[%s] 스킵 (완료)", ym)
                continue

            self._collect_month(month_start, month_end, out_path)

    # ── 월 수집 ─────────────────────────────────────────────────

    def _collect_month(self, month_start: date, month_end: date, out_path: Path) -> None:
        """
        하루씩 체크포인트 parquet으로 저장.
        월 완료 시 합쳐서 최종 parquet 생성 → 체크포인트 삭제.
        재실행 시 완료된 날짜 자동 스킵.
        """
        ym = month_start.strftime("%Y%m")
        days = self._day_ranges(month_start, month_end)

        for day in days:
            day_str = day.strftime("%Y%m%d")
            ckpt_path = self.checkpoint_dir / f"ckpt_{day_str}.parquet"

            if ckpt_path.exists():
                logger.info("  [%s] 체크포인트 있음, 스킵", day_str)
                continue

            self._collect_day(day, ckpt_path)

        # 월 전체 체크포인트 합치기
        self._merge_checkpoints(ym, days, out_path)

    def _collect_day(self, day: date, ckpt_path: Path) -> None:
        """하루치 15분 파일 수집 → 체크포인트 저장."""
        day_end = day  # 하루
        total = count_files(day, day_end)
        accumulated: list[pd.DataFrame] = []

        for _, url in tqdm(
            generate_file_urls(day, day_end),
            total=total,
            desc=f"    {day.strftime('%Y-%m-%d')}",
            leave=False,
        ):
            chunk = self._download_and_parse(url)
            if chunk is not None and not chunk.empty:
                accumulated.append(chunk)
            time.sleep(self.DOWNLOAD_DELAY_SEC)

        if accumulated:
            df = pd.concat(accumulated, ignore_index=True)
            df = df.drop_duplicates(subset="url", keep="first")
            df.to_parquet(ckpt_path, index=False, engine="pyarrow")
            logger.debug("[%s] %d건 → 체크포인트 저장", day.strftime("%Y%m%d"), len(df))
        else:
            # 데이터 없어도 빈 파일 생성 (스킵 마커)
            pd.DataFrame().to_parquet(ckpt_path, index=False, engine="pyarrow")
            logger.debug("[%s] 수집 결과 없음 (빈 체크포인트)", day.strftime("%Y%m%d"))

    def _merge_checkpoints(self, ym: str, days: list[date], out_path: Path) -> None:
        """월별 체크포인트 → 최종 parquet 병합 후 체크포인트 삭제."""
        frames: list[pd.DataFrame] = []

        for day in days:
            ckpt_path = self.checkpoint_dir / f"ckpt_{day.strftime('%Y%m%d')}.parquet"
            if not ckpt_path.exists():
                logger.warning("  체크포인트 누락: %s", ckpt_path)
                continue
            df = pd.read_parquet(ckpt_path)
            if not df.empty:
                frames.append(df)

        if frames:
            merged = pd.concat(frames, ignore_index=True)
            merged = merged.drop_duplicates(subset="url", keep="first")
            merged.to_parquet(out_path, index=False, engine="pyarrow")
            logger.info("[%s] 최종 %d건 저장 → %s", ym, len(merged), out_path)
        else:
            logger.warning("[%s] 수집 결과 없음 (parquet 미생성)", ym)

        # 체크포인트 삭제
        for day in days:
            ckpt_path = self.checkpoint_dir / f"ckpt_{day.strftime('%Y%m%d')}.parquet"
            if ckpt_path.exists():
                ckpt_path.unlink()
        logger.debug("[%s] 체크포인트 정리 완료", ym)

    # ── 다운로드 ────────────────────────────────────────────────

    def _download_and_parse(self, url: str) -> Optional[pd.DataFrame]:
        for attempt in range(self.RETRY_COUNT):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "GDELT-Research/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                return _parse_gkg_bytes(raw)
            except Exception as e:
                err = str(e)
                if "404" in err or "HTTP Error 404" in err:
                    return None  # 정상적인 누락 파일
                if attempt < self.RETRY_COUNT - 1:
                    time.sleep(self.RETRY_DELAY_SEC * (attempt + 1))
                else:
                    logger.debug("다운로드 실패: %s", url)
        return None

    # ── 유틸 ────────────────────────────────────────────────────

    @staticmethod
    def _month_ranges(start: date, end: date) -> list[tuple[date, date]]:
        result = []
        cur = start.replace(day=1)
        while cur <= end:
            nxt = cur.replace(month=cur.month % 12 + 1, year=cur.year + (cur.month // 12))
            last = nxt - timedelta(days=1)
            result.append((cur, min(last, end)))
            cur = nxt
        return result

    @staticmethod
    def _day_ranges(start: date, end: date) -> list[date]:
        days = []
        cur = start
        while cur <= end:
            days.append(cur)
            cur += timedelta(days=1)
        return days