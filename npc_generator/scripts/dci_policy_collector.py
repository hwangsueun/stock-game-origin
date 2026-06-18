import os
import re
import json
import time
import math
import random
import pickle
import html as html_lib
import threading
import itertools
import concurrent.futures
from glob import glob
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from functools import lru_cache
from collections import deque

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

from shared_checkpoint import SharedCheckpointClient, CrawlJob

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# =========================================================
# 경로 설정
# =========================================================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
RAW_DIR      = os.path.join(PROJECT_ROOT, "data", "raw")
DIVISION_DIR = os.path.join(PROJECT_ROOT, "data", "gall_division")

POLICY_CSV      = os.path.join(DIVISION_DIR, "gallery_policy_decision.csv")
POSTS_CSV       = os.path.join(RAW_DIR, "dci_posts_policy.csv")
COMMENTS_CSV    = os.path.join(RAW_DIR, "dci_comments_policy.csv")
CHECKPOINT_JSON = os.path.join(RAW_DIR, "policy_collection_checkpoint.json")
KEY_CACHE_PATH  = os.path.join(RAW_DIR, "existing_post_keys_policy.pkl")

os.makedirs(RAW_DIR, exist_ok=True)


# =========================================================
# 워커 식별
# =========================================================
WORKER_NAME = "hgs_policy_forward_01"
DIRECTION   = "policy_forward"

# =========================================================
# 설정
# =========================================================
START_DATE = "2013-01-01"
END_DATE   = "2023-12-31"
START_DT   = datetime.fromisoformat(START_DATE)
END_DT     = datetime.fromisoformat(END_DATE)

REQUEST_TIMEOUT  = 15
MAX_LIST_RETRY   = 3
MAX_DETAIL_RETRY = 3

INITIAL_RATE = 6.0
MAX_RATE     = 7.5
MIN_RATE     = 1.0
SCAN_RATE    = 2.5

MAX_WORKERS  = max(6, int(MAX_RATE * 1.5))
CMT_WORKERS  = max(4, int(MAX_RATE * 0.8))

LIST_FETCH_WORKERS = 4
SCAN_WORKERS       = 10

AVG_RESPONSE_SEC = 1.0
WINDOW_BUFFER    = 3
WINDOW_SIZE      = max(12, int(MAX_RATE * AVG_RESPONSE_SEC * WINDOW_BUFFER))

FLUSH_EVERY    = 200
CONN_POOL_SIZE = 32

TYPE2_MIN_RECOMMEND        = 2
TYPE2_MIN_COMMENT_HINT     = 2
TYPE2_MIN_MONTHLY_QUOTA    = 100
TYPE2_QUOTA_RATIO          = 0.30
TYPE2_MAX_MONTHLY_QUOTA    = 500
TYPE2_FALLBACK_MIN_SELECTED    = 100
TYPE2_FALLBACK_MIN_MONTHS      = 12
TYPE2_FALLBACK_MIN_TARGET_ROWS = 50

HEADERS = {
    "Referer":                   "https://gall.dcinside.com/",
    "Accept-Language":           "ko-KR,ko;q=0.9",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding":           "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "same-origin",
    "Sec-Fetch-User":            "?1",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

POST_COLUMNS = [
    "gall_id", "gall_type", "post_no", "title", "writer", "date",
    "views", "recommend", "unrecommend", "body",
    "has_image", "image_count", "image_urls_json",
    "has_video", "video_count", "video_urls_json",
    "has_external_link", "external_link_count", "external_links_json",
    "collection_policy", "collected_by",
]

COMMENT_COLUMNS = [
    "gall_id", "post_no", "cmt_no", "cmt_writer",
    "cmt_date", "cmt_body", "cmt_rcnt",
    "collection_policy", "collected_by",
]

try:
    import lxml  # noqa
    HTML_PARSER = "lxml"
except Exception:
    HTML_PARSER = "html.parser"

# =========================================================
# 컴파일된 정규식
# =========================================================
_INT_RE       = re.compile(r"-?\d+")
_DIGITS_RE    = re.compile(r"[^\d]")
_HTML_TAG_RE  = re.compile(r"<[^>]+>")
_DATE_FULL_RE = re.compile(r"(\d{4})[.\-](\d{2})[.\-](\d{2})")
_POST_NO_RE   = re.compile(r"no=(\d+)")
_E_S_N_O_RE   = re.compile(r"e_s_n_o=([a-zA-Z0-9]+)")

_REDIRECT_SENTINEL = object()   # 리다이렉트(범위 초과) 마커

_DC_URL_PREFIXES = (
    "https://gall.dcinside", "http://gall.dcinside",
    "https://image.dcinside", "http://image.dcinside",
    "https://nstatic.dcinside", "http://nstatic.dcinside",
    "//gall.dcinside", "//image.dcinside", "//nstatic.dcinside",
    "/board/", "/mini/", "/mgallery/", "/m/",
)


# =========================================================
# 데이터 클래스
# =========================================================
@dataclass
class GalleryPolicy:
    gallery_name: str
    gall_id: str
    gall_type: str
    total_pages: int
    collection_policy: str


@dataclass
class PageSummary:
    page_no: int
    newest_dt: datetime | None
    oldest_dt: datetime | None
    row_count: int


# =========================================================
# TokenBucket
# =========================================================
class TokenBucket:
    def __init__(self, rate: float, burst: int = 1):
        self.rate    = rate
        self.burst   = burst
        self._tokens = float(burst)
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now          = time.monotonic()
                self._tokens = min(float(self.burst),
                                   self._tokens + (now - self._last) * self.rate)
                self._last   = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


# =========================================================
# AdaptiveRateLimiter
# =========================================================
class AdaptiveRateLimiter:
    def __init__(self, initial_rate: float, min_rate: float, max_rate: float):
        self._rate        = initial_rate
        self._min_rate    = min_rate
        self._max_rate    = max_rate
        self._bucket      = TokenBucket(rate=initial_rate, burst=1)
        self._none_streak = 0
        self._ok_streak   = 0
        self._lock        = threading.Lock()
        self.NONE_THRESH  = 3
        self.OK_THRESH    = 50   # 100 → 50: ConnectionError 섞여도 빠르게 복구

    def acquire(self):
        self._bucket.acquire()

    def record_none(self):
        with self._lock:
            self._none_streak += 1
            self._ok_streak    = 0
            if self._none_streak >= self.NONE_THRESH:
                new_rate = max(self._min_rate, self._rate * 0.5)
                if new_rate < self._rate:
                    print(f"    [RATE↓] {self._rate:.2f}→{new_rate:.2f}req/s "
                          f"(RESP None {self.NONE_THRESH}회 연속)")
                    self._rate   = new_rate
                    self._bucket = TokenBucket(rate=new_rate, burst=1)
                self._none_streak = 0

    def record_ok(self):
        with self._lock:
            self._none_streak = 0
            self._ok_streak  += 1
            if self._ok_streak >= self.OK_THRESH and self._rate < self._max_rate:
                new_rate = min(self._max_rate, self._rate + 0.5)
                print(f"    [RATE↑] {self._rate:.2f}→{new_rate:.2f}req/s "
                      f"(연속 {self.OK_THRESH}회 성공)")
                self._rate   = new_rate
                self._bucket = TokenBucket(rate=new_rate, burst=1)
                self._ok_streak = 0

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate


_dc_limiter  = AdaptiveRateLimiter(initial_rate=INITIAL_RATE,
                                    min_rate=MIN_RATE, max_rate=MAX_RATE)
_scan_bucket = TokenBucket(rate=SCAN_RATE, burst=2)


# =========================================================
# [FIX] 현재 갤러리 정보 전역 저장
# 워커 스레드가 세션 생성 시 이 값으로 워밍
# =========================================================
_current_gall_id   = ""
_current_gall_type = ""
_current_gall_lock = threading.Lock()


def set_current_gallery(gall_id: str, gall_type: str):
    global _current_gall_id, _current_gall_type
    with _current_gall_lock:
        _current_gall_id   = gall_id
        _current_gall_type = gall_type


def get_current_gallery() -> tuple[str, str]:
    with _current_gall_lock:
        return _current_gall_id, _current_gall_type


# =========================================================
# 스레드 로컬 세션
# =========================================================
_thread_local = threading.local()


def _make_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=CONN_POOL_SIZE,
                          pool_maxsize=CONN_POOL_SIZE,
                          max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update(HEADERS)
    s.headers["User-Agent"] = random.choice(USER_AGENTS)
    return s


def get_thread_session() -> requests.Session:
    """
    [FIX] 스레드 최초 세션 생성 시 현재 갤러리 목록 페이지로 워밍.
    모든 워커 스레드(detail_ex, cmt_ex 포함)가 쿠키를 올바르게 획득.
    """
    if not hasattr(_thread_local, "session"):
        s = _make_session()
        gall_id, gall_type = get_current_gallery()
        try:
            if gall_id and gall_type:
                warm_url = make_list_url(gall_id, gall_type, 1)
            else:
                warm_url = "https://gall.dcinside.com"
            s.get(warm_url, timeout=10)
        except Exception:
            pass
        _thread_local.session = s
    return _thread_local.session


def reset_thread_sessions():
    """
    [FIX] 갤러리 전환 시 모든 스레드 로컬 세션 무효화.
    다음 get_thread_session() 호출 시 새 갤러리로 재워밍.
    """
    # 현재 스레드 세션만 초기화 (워커 스레드는 첫 요청 시 자동 재생성)
    if hasattr(_thread_local, "session"):
        del _thread_local.session
    if hasattr(_thread_local, "cmt_headers"):
        del _thread_local.cmt_headers


def get_cmt_headers() -> dict:
    if not hasattr(_thread_local, "cmt_headers"):
        s = get_thread_session()
        _thread_local.cmt_headers = {
            **s.headers,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        }
    return _thread_local.cmt_headers


# =========================================================
# CsvBuffer
# =========================================================
class CsvBuffer:
    def __init__(self, path, columns, flush_every=500):
        self.path            = path
        self.columns         = columns
        self.flush_every     = flush_every
        self.buf: list       = []
        self._lock           = threading.Lock()
        self._header_written = os.path.exists(path)

    def add(self, rows):
        if not rows:
            return
        with self._lock:
            self.buf.extend(rows)
            if len(self.buf) >= self.flush_every:
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self.buf:
            return
        pd.DataFrame(self.buf).reindex(columns=self.columns).to_csv(
            self.path, mode="a", header=not self._header_written,
            index=False, encoding="utf-8-sig")
        self._header_written = True
        self.buf.clear()


# =========================================================
# PageCache
# =========================================================
class PageCache:
    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        return self._cache.get(key)

    def set(self, key, value):
        with self._lock:
            self._cache[key] = value

    def __contains__(self, key):
        return key in self._cache


# =========================================================
# 유틸
# =========================================================
def safe_int(v, default=0):
    if v is None:
        return default
    m = _INT_RE.search(str(v).strip().replace(",", ""))
    if not m:
        return default
    try:
        return int(m.group())
    except Exception:
        return default


def normalize_post_no(v) -> str: return str(v).strip()
def normalize_gall_id(v) -> str: return str(v).strip()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    return "https:" + url if url.startswith("//") else url


def is_external_url(url: str) -> bool:
    if not url:
        return False
    if url.startswith(_DC_URL_PREFIXES):
        return False
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("//")):
        return False
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    return bool(netloc) and "dcinside.com" not in netloc


def parse_comment_count(text: str) -> int:
    if not text:
        return 0
    digits = _DIGITS_RE.sub("", text)
    return int(digits) if digits else 0


def clean_comment_memo(memo: str) -> str:
    return html_lib.unescape(_HTML_TAG_RE.sub("", memo)).strip()


@lru_cache(maxsize=8192)
def parse_date_str(raw: str | None):
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    now = datetime.now()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    for fmt, kw in [("%m.%d %H:%M", {}), ("%m.%d", {}),
                    ("%H:%M", {"month": now.month, "day": now.day})]:
        try:
            return datetime.strptime(text, fmt).replace(year=now.year, **kw)
        except Exception:
            pass
    m = _DATE_FULL_RE.search(text)
    if m:
        y, mm, d = m.groups()
        try:
            return datetime(int(y), int(mm), int(d))
        except Exception:
            pass
    return None


# =========================================================
# HttpClient
# =========================================================
class HttpClient:
    @property
    def session(self) -> requests.Session:
        return get_thread_session()

    def get(self, url: str):
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp   # content 비어도 반환 — 삭제 여부는 상위에서 판단
            print(f"    [HTTP {resp.status_code}] {url[:60]}")
            return None
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            # [FIX] 연결 끊김 — "conn_err" 문자열로 구분
            return "conn_err"
        except requests.RequestException as e:
            print(f"    [REQ ERR] {type(e).__name__}: {e}")
            return None

    def post(self, url: str, data: dict, headers=None):
        try:
            resp = self.session.post(url, data=data, headers=headers,
                                     timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                resp.encoding = "utf-8"
                return resp
            return None
        except requests.RequestException:
            return None


# =========================================================
# 정책 / 기존키
# =========================================================
def load_gallery_policy_table() -> list[GalleryPolicy]:
    if not os.path.exists(POLICY_CSV):
        raise FileNotFoundError(f"파일 없음: {POLICY_CSV}")
    df = pd.read_csv(POLICY_CSV)
    required = {"gallery_name", "gall_id", "gall_type", "total_pages", "collection_policy"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"누락 컬럼: {sorted(missing)}")
    rows = []
    for r in df.to_dict("records"):
        policy = str(r["collection_policy"]).strip()
        if policy == "gap_unassigned":
            continue
        rows.append(GalleryPolicy(
            gallery_name=str(r["gallery_name"]).strip(),
            gall_id=str(r["gall_id"]).strip(),
            gall_type=str(r["gall_type"]).strip(),
            total_pages=int(r["total_pages"]),
            collection_policy=policy,
        ))
    return rows


def discover_existing_post_csvs(output_dir, current_posts_csv):
    candidates = set()
    legacy = os.path.join(output_dir, "dci_posts.csv")
    if os.path.exists(legacy):
        candidates.add(legacy)
    for path in glob(os.path.join(output_dir, "dci_posts*.csv")):
        candidates.add(path)
    candidates.add(current_posts_csv)
    return sorted(candidates)


def build_file_signature(csv_paths):
    sig = {}
    for path in csv_paths:
        if not os.path.exists(path):
            continue
        st = os.stat(path)
        sig[path] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    return sig


def is_cache_valid(cache_obj, csv_paths):
    if not isinstance(cache_obj, dict):
        return False
    if "keys" not in cache_obj or "signature" not in cache_obj:
        return False
    return build_file_signature(csv_paths) == cache_obj.get("signature", {})


def save_existing_post_keys_cache(existing_post_keys, csv_paths, cache_path):
    obj = {"signature": build_file_signature(csv_paths),
           "keys": existing_post_keys, "saved_at": str(datetime.now())}
    with open(cache_path, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_existing_post_keys(csv_paths, cache_path) -> set[tuple[str, str]]:
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                obj = pickle.load(f)
            if is_cache_valid(obj, csv_paths):
                return obj["keys"]
        except Exception:
            pass
    existing_keys = set()
    for path in csv_paths:
        if not os.path.exists(path):
            continue
        try:
            for chunk in pd.read_csv(path, usecols=["gall_id", "post_no"],
                                     chunksize=200000, low_memory=False):
                chunk = chunk.dropna(subset=["gall_id", "post_no"]).copy()
                chunk["gall_id"] = chunk["gall_id"].astype(str).str.strip()
                chunk["post_no"] = chunk["post_no"].astype(str).str.strip()
                existing_keys.update(zip(chunk["gall_id"], chunk["post_no"]))
        except Exception:
            continue
    save_existing_post_keys_cache(existing_keys, csv_paths, cache_path)
    return existing_keys


# =========================================================
# URL
# =========================================================
def make_list_url(gall_id, gall_type, page, recommend_only=False):
    if gall_type == "MI":
        base = f"https://gall.dcinside.com/mini/board/lists/?id={gall_id}&page={page}"
    elif gall_type == "G":
        base = f"https://gall.dcinside.com/board/lists/?id={gall_id}&page={page}"
    else:
        base = f"https://gall.dcinside.com/mgallery/board/lists/?id={gall_id}&page={page}"
    if recommend_only:
        base += "&exception_mode=recommend"
    return base


def make_view_url(gall_id, gall_type, post_no):
    if gall_type == "MI":
        return f"https://gall.dcinside.com/mini/board/view/?id={gall_id}&no={post_no}"
    elif gall_type == "G":
        return f"https://gall.dcinside.com/board/view/?id={gall_id}&no={post_no}"
    return f"https://gall.dcinside.com/mgallery/board/view/?id={gall_id}&no={post_no}"


# =========================================================
# 목록 파싱
# =========================================================
def fetch_list_html(client, gall_id, gall_type, page, recommend_only):
    url = make_list_url(gall_id, gall_type, page, recommend_only=recommend_only)
    for _ in range(MAX_LIST_RETRY):
        resp = client.get(url)
        if resp is not None:
            if page > 1 and f"page={page}" not in resp.url:
                return _REDIRECT_SENTINEL   # ← None 대신 sentinel
            return resp.text
        time.sleep(random.uniform(1.5, 3.0))
    return None


def parse_post_list(html, gall_id, gall_type) -> list[dict]:
    soup    = BeautifulSoup(html, HTML_PARSER)
    results = []
    for row in soup.select("tr.ub-content"):
        num_tag   = row.select_one("td.gall_num")
        title_tag = row.select_one("td.gall_tit a:not(.reply_numbox)")
        date_tag  = row.select_one("td.gall_date")
        if not (num_tag and title_tag and date_tag):
            continue
        if not num_tag.get_text(strip=True).isdigit():
            continue
        href = title_tag.get("href", "")
        m = _POST_NO_RE.search(href)
        if not m:
            continue
        e          = _E_S_N_O_RE.search(href)
        writer_tag = row.select_one("td.gall_writer")
        count_tag  = row.select_one("td.gall_count")
        recom_tag  = row.select_one("td.gall_recommend")
        reply_tag  = row.select_one("a.reply_numbox")
        date_title = (date_tag.get("title") or "").strip()
        date_raw   = str(date_title if date_title else date_tag.get_text(strip=True))
        results.append({
            "gall_id":            gall_id,
            "gall_type":          gall_type,
            "post_no":            m.group(1),
            "title":              title_tag.get("title") or title_tag.get_text(strip=True),
            "writer":             writer_tag.get_text(strip=True) if writer_tag else "",
            "date_raw":           date_raw,
            "_dt":                parse_date_str(date_raw),
            "views":              count_tag.get_text(strip=True).replace(",", "") if count_tag else "0",
            "recommend":          safe_int(recom_tag.get_text(strip=True) if recom_tag else "0", 0),
            "comment_count_hint": parse_comment_count(reply_tag.get_text(strip=True) if reply_tag else ""),
            "_e_s_n_o":           e.group(1) if e else "",
            "_href":              make_view_url(gall_id, gall_type, m.group(1)),
        })
    return results


def summarize_page(rows, page_no) -> PageSummary:
    dts = [row["_dt"] for row in rows if row.get("_dt") is not None]
    return PageSummary(page_no=page_no,
                       newest_dt=max(dts) if dts else None,
                       oldest_dt=min(dts) if dts else None,
                       row_count=len(rows))


# =========================================================
# 페이지 캐시
# =========================================================
def page_cache_key(gall_id, gall_type, recommend_only, page_no):
    return (gall_id, gall_type, recommend_only, page_no)


def get_page_rows_and_summary(client, gall_id, gall_type, recommend_only, page_no, cache):
    key    = page_cache_key(gall_id, gall_type, recommend_only, page_no)
    cached = cache.get(key)
    if cached is not None:
        return cached["rows"], cached["summary"]
    html = fetch_list_html(client, gall_id, gall_type, page_no, recommend_only=recommend_only)
    # sentinel 또는 None 모두 빈 결과
    if html is None or html is _REDIRECT_SENTINEL:
        entry = {"rows": [], "summary": PageSummary(page_no, None, None, 0)}
        cache.set(key, entry); return entry["rows"], entry["summary"]
    rows    = parse_post_list(html, gall_id, gall_type)
    summary = summarize_page(rows, page_no)
    cache.set(key, {"rows": rows, "summary": summary})
    return rows, summary


def prefetch_pages_parallel(gall_id, gall_type, recommend_only, pages, cache):
    missing = [p for p in pages
               if cache.get(page_cache_key(gall_id, gall_type, recommend_only, p)) is None]
    if not missing:
        return

    def _fetch(page_no):
        c   = HttpClient()
        key = page_cache_key(gall_id, gall_type, recommend_only, page_no)
        html = fetch_list_html(c, gall_id, gall_type, page_no, recommend_only=recommend_only)
        if html is None:
            return key, [], PageSummary(page_no, None, None, 0)
        rows = parse_post_list(html, gall_id, gall_type)
        return key, rows, summarize_page(rows, page_no)

    with ThreadPoolExecutor(max_workers=LIST_FETCH_WORKERS) as ex:
        for fut in as_completed([ex.submit(_fetch, p) for p in missing]):
            try:
                key, rows, summary = fut.result()
                cache.set(key, {"rows": rows, "summary": summary})
            except Exception:
                pass


# =========================================================
# 이진탐색
# =========================================================
def find_first_page_reaching_end_date(client, policy, recommend_only, cache):
    _, s1 = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, 1, cache)
    if s1.oldest_dt is None: return None
    if s1.oldest_dt <= END_DT: return 1
    lo, hi = 1, 2
    while hi <= policy.total_pages:
        hi = min(hi, policy.total_pages)
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.oldest_dt is not None and sh.oldest_dt <= END_DT: break
        if hi == policy.total_pages: return None
        lo = hi; hi *= 2
    if hi > policy.total_pages:
        hi = policy.total_pages
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.oldest_dt is None or sh.oldest_dt > END_DT: return None
    left, right, ans = lo + 1, hi, hi
    while left <= right:
        mid = (left + right) // 2
        _, sm = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, mid, cache)
        if sm.oldest_dt is not None and sm.oldest_dt <= END_DT:
            ans = mid; right = mid - 1
        else:
            left = mid + 1
    return ans


def find_first_page_before_start_date(client, policy, recommend_only, start_page, cache):
    lo, hi = start_page, min(policy.total_pages, max(start_page, 2))
    while hi <= policy.total_pages:
        hi = min(hi, policy.total_pages)
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.newest_dt is not None and sh.newest_dt < START_DT: break
        if hi == policy.total_pages: return policy.total_pages + 1
        lo = hi; hi *= 2
    if hi > policy.total_pages:
        hi = policy.total_pages
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.newest_dt is None or sh.newest_dt >= START_DT: return policy.total_pages + 1
    left, right, ans = lo + 1, hi, hi
    while left <= right:
        mid = (left + right) // 2
        _, sm = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, mid, cache)
        if sm.newest_dt is not None and sm.newest_dt < START_DT:
            ans = mid; right = mid - 1
        else:
            left = mid + 1
    return ans


def find_target_page_window(client, policy, recommend_only, cache):
    # recommend_only 일 때 실제 마지막 페이지를 먼저 확인
    if recommend_only:
        actual_last = find_actual_last_page(
            client, policy.gall_id, policy.gall_type,
            recommend_only, policy.total_pages, cache)
        # total_pages 를 실제 값으로 덮어쓴 임시 policy 객체 사용
        from dataclasses import replace
        policy = replace(policy, total_pages=actual_last)
 
    start_page = find_first_page_reaching_end_date(
        client, policy, recommend_only, cache)
    if start_page is None:
        return None, None
 
    estimated_mid = min(policy.total_pages, start_page * 4)
    step = max(1, (estimated_mid - start_page) // 8)
    prefetch_pages_parallel(
        policy.gall_id, policy.gall_type, recommend_only,
        list(range(start_page, estimated_mid + 1, step)), cache)
 
    first_old = find_first_page_before_start_date(
        client, policy, recommend_only, start_page, cache)
    end_page = min(policy.total_pages, first_old - 1)
    if end_page < start_page:
        return None, None
    return start_page, end_page


# =========================================================
# extract_post_assets
# =========================================================
_EMPTY_JSON = "[]"

def extract_post_assets(soup):
    body_tag = soup.select_one("div.write_div")
    if not body_tag:
        return {"body_text": "", "has_image": False, "image_count": 0,
                "image_urls_json": _EMPTY_JSON, "has_video": False, "video_count": 0,
                "video_urls_json": _EMPTY_JSON, "has_external_link": False,
                "external_link_count": 0, "external_links_json": _EMPTY_JSON}

    body_text = body_tag.get_text("\n", strip=True)
    imgs,  seen_i = [], set()
    vids,  seen_v = [], set()
    links, seen_l = [], set()

    for tag in body_tag.find_all(True):
        name = tag.name
        if name == "img":
            s = normalize_url(tag.get("src", ""))
            if s and s not in seen_i:
                seen_i.add(s); imgs.append(s)
        elif name in ("video", "iframe"):
            s = normalize_url(tag.get("src", ""))
            if s and s not in seen_v:
                seen_v.add(s); vids.append(s)
        elif name == "source":
            s = normalize_url(tag.get("src", ""))
            if s and s not in seen_v:
                seen_v.add(s); vids.append(s)
        elif name == "a":
            h = normalize_url(tag.get("href", ""))
            if h and is_external_url(h) and h not in seen_l:
                seen_l.add(h); links.append(h)

    return {
        "body_text": body_text,
        "has_image":  bool(imgs), "image_count":  len(imgs),
        "image_urls_json":  json.dumps(imgs, ensure_ascii=False) if imgs else _EMPTY_JSON,
        "has_video":  bool(vids), "video_count":  len(vids),
        "video_urls_json":  json.dumps(vids, ensure_ascii=False) if vids else _EMPTY_JSON,
        "has_external_link": bool(links), "external_link_count": len(links),
        "external_links_json": json.dumps(links, ensure_ascii=False) if links else _EMPTY_JSON,
    }


# =========================================================
# 댓글
# =========================================================
def _fetch_comment_page(gall_id, gall_type, post_no, e_s_n_o, gallery_no,
                        collection_policy, page_no) -> tuple[int, list, int]:
    data = {"id": gall_id, "no": post_no, "cmt_id": gall_id, "cmt_no": post_no,
            "e_s_n_o": e_s_n_o, "comment_page": str(page_no),
            "_GALLTYPE_": gall_type, "cur_cate": "finance"}
    if gallery_no:
        data["gallery_no"] = gallery_no

    _dc_limiter.acquire()
    try:
        session = get_thread_session()
        resp = session.post("https://gall.dcinside.com/board/comment/",
                            data=data,
                            headers=get_cmt_headers(),
                            timeout=REQUEST_TIMEOUT)
        if not resp.ok or not resp.content:
            _dc_limiter.record_none()
            return page_no, [], 0
        resp.encoding = "utf-8"
        result = resp.json()
    except Exception:
        _dc_limiter.record_none()
        return page_no, [], 0

    _dc_limiter.record_ok()
    total_cnt = safe_int(result.get("total_cnt"), 0)
    comments  = []
    for cmt in result.get("comments", []):
        try:
            if cmt.get("del_yn") == "Y" or cmt.get("is_delete") == "1":
                continue
            memo  = cmt.get("memo", "")
            clean = clean_comment_memo(memo) if memo else ""
            if not clean:
                continue
            reg = cmt.get("reg_date") or ""
            comments.append({
                "gall_id":    gall_id, "post_no": post_no,
                "cmt_no":     cmt.get("no", ""),
                "cmt_writer": cmt.get("name", ""),
                "cmt_date":   reg.split()[0] if reg else "",
                "cmt_body":   clean,
                "cmt_rcnt":   cmt.get("rcnt", "0"),
                "collection_policy": collection_policy,
                "collected_by":      "policy_collector",
            })
        except Exception:
            continue
    return page_no, comments, total_cnt


def get_comments(gall_id, gall_type, post_no, e_s_n_o, gallery_no,
                 collection_policy, comment_count_hint: int,
                 cmt_ex: ThreadPoolExecutor) -> list:
    CMT_PER_PAGE    = 100
    estimated_pages = max(1, math.ceil(comment_count_hint / CMT_PER_PAGE))

    all_comments: dict[int, list] = {}
    actual_total = 0

    futs = {cmt_ex.submit(_fetch_comment_page, gall_id, gall_type, post_no,
                          e_s_n_o, gallery_no, collection_policy, p): p
            for p in range(1, estimated_pages + 1)}

    try:                                                          # ← 추가
        for fut in as_completed(futs, timeout=60):               # ← timeout 추가
            pno, cmts, total_cnt = fut.result()
            all_comments[pno] = cmts
            if total_cnt > actual_total:
                actual_total = total_cnt
    except concurrent.futures.TimeoutError:                      # ← 추가
        print(f"    [CMT TIMEOUT] post_no={post_no} 1차 수집 타임아웃")

    real_pages    = math.ceil(actual_total / CMT_PER_PAGE) if actual_total > 0 else estimated_pages
    missing_pages = [p for p in range(1, real_pages + 1) if p not in all_comments]

    if missing_pages:
        futs2 = {cmt_ex.submit(_fetch_comment_page, gall_id, gall_type, post_no,
                               e_s_n_o, gallery_no, collection_policy, p): p
                 for p in missing_pages}
        try:                                                      # ← 추가
            for fut in as_completed(futs2, timeout=60):          # ← timeout 추가
                pno, cmts, _ = fut.result()
                all_comments[pno] = cmts
        except concurrent.futures.TimeoutError:                  # ← 추가
            print(f"    [CMT TIMEOUT] post_no={post_no} 누락 페이지 타임아웃")

    result = []
    for p in sorted(all_comments):
        result.extend(all_comments[p])
    return result


# =========================================================
# fetch_post_detail
# =========================================================
def fetch_post_detail(post_meta: dict, collection_policy: str,
                      cmt_ex: ThreadPoolExecutor):
    client = HttpClient()
    for attempt in range(MAX_DETAIL_RETRY):
        _dc_limiter.acquire()
        resp = client.get(post_meta["_href"])

        if resp is None:
            # HTTP 비정상 응답 (403, 503 등) → 차단 가능성 → rate 하향
            _dc_limiter.record_none()
            time.sleep(random.uniform(1.0, 2.0) * (attempt + 1))
            continue

        if resp == "conn_err":
            # [FIX] ConnectionError — 서버 연결 끊김, rate는 건드리지 않음
            time.sleep(random.uniform(0.5, 1.5) * (attempt + 1))
            continue

        # [FIX] content 없음 = 삭제된 게시물 → 재시도/rate 하향 없이 즉시 스킵
        if not resp.content:
            return None, []

        try:
            soup   = BeautifulSoup(resp.text, HTML_PARSER)
            assets = extract_post_assets(soup)

            rec_box = soup.select_one(".btn_recommend_box")
            up      = rec_box.select_one(".up_num")  if rec_box else None
            down    = rec_box.select_one(".sup_num") if rec_box else None

            et      = soup.select_one("input#e_s_n_o") or soup.select_one("input[name=e_s_n_o]")
            e_s_n_o = et.get("value", "") if et else post_meta.get("_e_s_n_o", "")

            gallery_no = ""
            for inp in soup.select("input[name=gallery_no]"):
                v = inp.get("value", "")
                if v.isdigit() and v != post_meta["post_no"]:
                    gallery_no = v; break

            dt_tag   = soup.select_one("span.gall_date")
            date_str = dt_tag.get("title", "").split()[0] if dt_tag else post_meta.get("date_raw", "")
            hint     = int(post_meta.get("comment_count_hint", 0) or 0)
            comments = [] if hint == 0 else get_comments(
                gall_id=post_meta["gall_id"], gall_type=post_meta["gall_type"],
                post_no=post_meta["post_no"], e_s_n_o=e_s_n_o,
                gallery_no=gallery_no, collection_policy=collection_policy,
                comment_count_hint=hint, cmt_ex=cmt_ex,
            )

            post_row = {
                "gall_id":             post_meta["gall_id"],
                "gall_type":           post_meta["gall_type"],
                "post_no":             post_meta["post_no"],
                "title":               post_meta.get("title", ""),
                "writer":              post_meta.get("writer", ""),
                "date":                date_str,
                "views":               safe_int(post_meta.get("views", 0), 0),
                "recommend":           safe_int(up.get_text(strip=True) if up else post_meta.get("recommend", 0), 0),
                "unrecommend":         safe_int(down.get_text(strip=True) if down else 0, 0),
                "body":                assets["body_text"],
                "has_image":           assets["has_image"],
                "image_count":         assets["image_count"],
                "image_urls_json":     assets["image_urls_json"],
                "has_video":           assets["has_video"],
                "video_count":         assets["video_count"],
                "video_urls_json":     assets["video_urls_json"],
                "has_external_link":   assets["has_external_link"],
                "external_link_count": assets["external_link_count"],
                "external_links_json": assets["external_links_json"],
                "collection_policy":   collection_policy,
                "collected_by":        "policy_collector",
            }
            _dc_limiter.record_ok()
            return post_row, comments

        except Exception as e:
            print(f"    [PARSE ERR] post_no={post_meta['post_no']} attempt={attempt}: {e}")
            time.sleep(random.uniform(0.5, 1.0))

    return None, []


# =========================================================
# 후보 수집
# =========================================================
def _collect_rows_in_window(client, policy, recommend_only, start_page, end_page,
                             cache, existing_post_keys, filter_fn=None,
                             resume_candidates=None, resume_from_page=0,
                             effective_policy="", job_id=None, shared_client=None):
    actual_start  = resume_from_page if resume_from_page > start_page else start_page
    all_pages     = list(range(actual_start, end_page + 1))
    candidates    = list(resume_candidates) if resume_candidates else []
    in_target_rows   = 0
    skipped_existing = 0
    SAVE_EVERY       = 25
    BATCH_SIZE       = SCAN_WORKERS
    total_to_scan    = len(all_pages)
    MAX_REDIRECT_BATCHES = 10
    redirect_streak  = 0
    _actual_end_page = end_page  # 리다이렉트 감지 시 동적으로 줄임

    print(f"    [스캔] {total_to_scan}p | 병렬={BATCH_SIZE} | rate≤{SCAN_RATE}req/s")

    # ── _fetch_page: (page_no, rows, is_redirect) 반환 ──────────────────
    def _fetch_page(page_no):
        _scan_bucket.acquire()
        c    = HttpClient()
        html = fetch_list_html(c, policy.gall_id, policy.gall_type,
                               page_no, recommend_only)
        if html is _REDIRECT_SENTINEL:
            return page_no, [], True    # 범위 초과 리다이렉트
        if html is None:
            return page_no, [], False   # 네트워크 오류 (재시도 소진)
        return page_no, parse_post_list(html, policy.gall_id, policy.gall_type), False

    batches    = [all_pages[i:i + BATCH_SIZE] for i in range(0, total_to_scan, BATCH_SIZE)]
    early_stop = False
    batch_idx  = 0
    executor   = ThreadPoolExecutor(max_workers=BATCH_SIZE)
    pending_map: dict[Future, int] = {}

    def _submit_batch(pages):
        return {executor.submit(_fetch_page, p): p for p in pages}

    if batches:
        pending_map = _submit_batch(batches[0])

    try:
        for batch_i, batch_pages in enumerate(batches):
            if early_stop:
                break
            next_map = _submit_batch(batches[batch_i + 1]) if batch_i + 1 < len(batches) else {}

            page_rows_map:     dict[int, list] = {}
            page_redirect_map: dict[int, bool] = {}

            for fut in as_completed(pending_map):
                pno, rows, is_redirect = fut.result()
                page_rows_map[pno]     = rows
                page_redirect_map[pno] = is_redirect

            # ── 리다이렉트 감지: 범위 초과 → 즉시 종료 ──────────────────
            redirect_pages = [p for p in batch_pages if page_redirect_map.get(p)]
            if redirect_pages:
                new_end = min(redirect_pages) - 1
                print(f"    [범위 초과] p={min(redirect_pages)} 이후 리다이렉트 감지 "
                      f"→ end_page {_actual_end_page}→{new_end}")
                _actual_end_page = new_end
                early_stop = True

            # ── 네트워크 오류 누적 체크 (서버 차단 등) ──────────────────
            else:
                error_cnt = sum(
                    1 for p in batch_pages
                    if not page_rows_map.get(p) and not page_redirect_map.get(p)
                )
                if error_cnt / max(len(batch_pages), 1) >= 0.5:
                    redirect_streak += 1
                    if redirect_streak >= MAX_REDIRECT_BATCHES:
                        print(f"    [조기 종료] {redirect_streak}배치 연속 네트워크 오류")
                        early_stop = True
                else:
                    redirect_streak = 0

            # ── 데이터 수집 (early_stop이어도 현 배치 데이터는 저장) ────
            for page in batch_pages:
                for row in page_rows_map.get(page, []):
                    dt = row.get("_dt")
                    if dt is None or dt < START_DT or dt > END_DT:
                        continue
                    in_target_rows += 1
                    key = (normalize_gall_id(row["gall_id"]), normalize_post_no(row["post_no"]))
                    if key in existing_post_keys:
                        skipped_existing += 1; continue
                    if filter_fn is not None and not filter_fn(row):
                        continue
                    candidates.append(row)

            pending_map = next_map
            batch_idx  += 1
            scanned     = min((batch_i + 1) * BATCH_SIZE, total_to_scan)
            is_last     = (scanned == total_to_scan) or early_stop

            if batch_idx % SAVE_EVERY == 0 or is_last:
                print(f"    [스캔] {scanned}/{total_to_scan}p | 후보 {len(candidates)}건")
                if shared_client and job_id:
                    try: shared_client.heartbeat_job(job_id=job_id)
                    except Exception: pass
                if job_id and effective_policy:
                    _save_candidate_cache(
                        job_id=job_id, candidates=candidates,
                        effective_policy=effective_policy, build_done=is_last,
                        last_scanned_page=batch_pages[-1],
                        start_page=start_page, end_page=_actual_end_page,
                        recommend_only=recommend_only)
    finally:
        executor.shutdown(wait=False)

    return candidates, {"window": (start_page, _actual_end_page),
                        "in_target_rows": in_target_rows,
                        "skipped_existing": skipped_existing}

def build_general_candidates(client, policy, existing_post_keys, cache, job_id=None, shared_client=None):
    sp, ep = find_target_page_window(client, policy, False, cache)
    if sp is None: print("    [general] 타깃 없음"); return [], {"window": None, "selected": 0}
    print(f"    [general window] {sp}~{ep}")
    candidates, meta = _collect_rows_in_window(
        client, policy, False, sp, ep, cache, existing_post_keys,
        effective_policy="general_full", job_id=job_id, shared_client=shared_client)
    summary = {**meta, "selected": len(candidates)}
    print(f"    [general] in_target={meta['in_target_rows']}, skip={meta['skipped_existing']}, selected={len(candidates)}")
    return candidates, summary


def build_recommend_candidates(client, policy, existing_post_keys, cache, job_id=None, shared_client=None):
    sp, ep = find_target_page_window(client, policy, True, cache)
    if sp is None: print("    [recommend] 타깃 없음"); return [], {"window": None, "selected": 0}
    print(f"    [recommend window] {sp}~{ep}")
    candidates, meta = _collect_rows_in_window(
        client, policy, True, sp, ep, cache, existing_post_keys,
        effective_policy="recommend_only", job_id=job_id, shared_client=shared_client)
    summary = {**meta, "selected": len(candidates)}
    print(f"    [recommend] in_target={meta['in_target_rows']}, skip={meta['skipped_existing']}, selected={len(candidates)}")
    return candidates, summary


def build_type2_candidates(client, policy, existing_post_keys, cache):
    sp, ep = find_target_page_window(client, policy, False, cache)
    if sp is None:
        print("    [type2] 타깃 없음")
        return [], {"window": None, "in_target_rows": 0, "skipped_existing": 0,
                    "passed_rule_rows": 0, "covered_months": 0, "selected": 0}
    print(f"    [type2 window] {sp}~{ep}")

    def _type2_filter(row):
        return (row["recommend"] >= TYPE2_MIN_RECOMMEND or
                row["comment_count_hint"] >= TYPE2_MIN_COMMENT_HINT)

    candidates, meta = _collect_rows_in_window(
        client, policy, False, sp, ep, cache, existing_post_keys, filter_fn=_type2_filter)

    for row in candidates:
        row["_views_int"] = safe_int(row["views"], 0)

    monthly_map: dict[str, list] = {}
    for row in candidates:
        dt = row.get("_dt")
        if dt: monthly_map.setdefault(dt.strftime("%Y-%m"), []).append(row)

    selected = []
    for ym, rows in sorted(monthly_map.items()):
        quota = min(max(TYPE2_MIN_MONTHLY_QUOTA, math.ceil(TYPE2_QUOTA_RATIO * len(rows))),
                    TYPE2_MAX_MONTHLY_QUOTA)
        rows_sorted = sorted(rows,
            key=lambda x: (x["recommend"], x["comment_count_hint"], x["_views_int"], x["post_no"]),
            reverse=True)
        selected.extend(rows_sorted[:quota])

    summary = {**meta, "passed_rule_rows": len(candidates),
               "covered_months": len(monthly_map), "selected": len(selected)}
    print(f"    [type2] passed={len(candidates)}, months={len(monthly_map)}, selected={len(selected)}")
    return selected, summary


def should_fallback_type2(summary):
    if summary["selected"] >= TYPE2_FALLBACK_MIN_SELECTED:
        return False   # 후보 수 충분 → 폴백 불필요
    return (summary["covered_months"] < TYPE2_FALLBACK_MIN_MONTHS or
            summary["in_target_rows"] < TYPE2_FALLBACK_MIN_TARGET_ROWS)


def update_existing_post_keys(existing_post_keys, posts):
    existing_post_keys.update(
        (normalize_gall_id(p["gall_id"]), normalize_post_no(p["post_no"]))
        for p in posts)


# =========================================================
# Supabase 후보 캐시
# =========================================================
STORAGE_BUCKET  = "iisecd-dc-candidate-cache"
_STORAGE_FIELDS = ("post_no", "_href", "_e_s_n_o", "gall_id", "gall_type",
                   "comment_count_hint", "recommend", "date_raw")
STORAGE_PART_SIZE = 200_000
MAX_STORAGE_PARTS = 20


def _upload_part(job_id, part, data, count, size_kb):
    try:
        import requests as _req
        path = f"candidates_{job_id}_part{part}.json"
        url  = f"{os.environ['SUPABASE_URL'].rstrip('/')}/storage/v1/object/{STORAGE_BUCKET}/{path}"
        hdrs = {"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
                "Content-Type": "application/json"}
        resp = _req.post(url, headers={**hdrs, "x-upsert": "true"}, data=data, timeout=120)
        if not resp.ok:
            print(f"  [Storage 실패] part{part}: {resp.status_code}")
        else:
            print(f"  [Storage 완료] part{part} | {count}건 | {size_kb:.1f}KB")
    except Exception as e:
        print(f"  [Storage 실패] part{part}: {e}")


def _save_candidates_to_storage(job_id, candidates):
    # [FIX] 기존 파트 먼저 삭제 후 새로 업로드 → 중복 적재 방지
    _delete_candidates_from_storage(job_id)

    slim  = [{k: row.get(k, "") for k in _STORAGE_FIELDS} for row in candidates]
    parts = [slim[i:i + STORAGE_PART_SIZE] for i in range(0, max(len(slim), 1), STORAGE_PART_SIZE)]
    for idx, part_data in enumerate(parts):
        data    = json.dumps(part_data, ensure_ascii=False).encode("utf-8")
        size_kb = len(data) / 1024
        threading.Thread(target=_upload_part,
                         args=(job_id, idx, data, len(part_data), size_kb),
                         daemon=True).start()
    return True


def _load_candidates_from_storage(job_id):
    import requests as _req
    hdrs     = {"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}"}
    base_url = os.environ["SUPABASE_URL"].rstrip("/")

    def _fetch_part(part):
        url = f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/candidates_{job_id}_part{part}.json"
        try:
            resp = _req.get(url, headers=hdrs, timeout=120)
            if resp.status_code == 404:
                return part, None
            if not resp.ok:
                print(f"  [Storage 로드 실패] part{part}: {resp.status_code}")
                return part, []
            return part, json.loads(resp.content)
        except Exception as e:
            print(f"  [Storage 로드 실패] part{part}: {e}")
            return part, []

    parts_data: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_part, p): p for p in range(MAX_STORAGE_PARTS)}
        for fut in as_completed(futs):
            part, data = fut.result()
            if data is not None:
                parts_data[part] = data

    if parts_data:
        all_cands = []
        for p in sorted(parts_data):
            all_cands.extend(parts_data[p])
        print(f"  [Storage 완료] 총 {len(all_cands)}건 ({len(parts_data)}파트, 병렬)")
        return all_cands

    try:
        path = f"candidates_{job_id}.json"
        url  = f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{path}"
        resp = _req.get(url, headers=hdrs, timeout=120)
        if resp.status_code == 404: return None
        if not resp.ok:
            print(f"  [Storage 구버전 실패] {resp.status_code}"); return None
        cands = json.loads(resp.content)
        print(f"  [Storage 구버전] {len(cands)}건")
        return cands
    except Exception as e:
        print(f"  [Storage 구버전 실패] {e}"); return None


def _delete_candidates_from_storage(job_id):
    import requests as _req
    hdrs     = {"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}"}
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    part = 0
    while True:
        url = f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/candidates_{job_id}_part{part}.json"
        try:
            resp = _req.delete(url, headers=hdrs, timeout=15)
            if resp.status_code == 404: break
            part += 1
        except Exception:
            break


def _save_candidate_cache(job_id, candidates, effective_policy, build_done=True,
                          last_scanned_page=0, start_page=0, end_page=0, recommend_only=False):
    if candidates:
        _save_candidates_to_storage(job_id, candidates)
    payload = {
        "build_done": build_done, "effective_policy": effective_policy,
        "last_scanned_page": last_scanned_page, "start_page": start_page,
        "end_page": end_page, "recommend_only": recommend_only,
        "saved_at": str(datetime.now()), "candidate_count": len(candidates),
        "has_storage": bool(candidates),
    }
    try:
        import requests as _req
        resp = _req.patch(
            f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/crawl_jobs",
            headers={"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                     "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
                     "Content-Type": "application/json"},
            params={"id": f"eq.{job_id}"},
            json={"candidate_cache": payload}, timeout=30)
        if not resp.ok:
            print(f"  [캐시 저장 실패] {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  [캐시 저장 실패] {e}")


def _load_candidate_cache(job, _unused):
    raw = getattr(job, "candidate_cache", None)
    if raw:
        try:
            if isinstance(raw, str): raw = json.loads(raw)
            status = "완료" if raw.get("build_done") else f"중단(p={raw.get('last_scanned_page')})"
            print(f"  [캐시 로드] {status} | {raw.get('saved_at')}")
            return raw
        except Exception as e:
            print(f"  [캐시 로드 실패] {e}")
    opposite = "policy_forward" if job.direction == "policy_reverse" else "policy_reverse"
    try:
        import requests as _req
        resp = _req.get(
            f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/crawl_jobs",
            headers={"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                     "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}"},
            params={"gall_id": f"eq.{job.gall_id}", "direction": f"eq.{opposite}",
                    "select": "candidate_cache", "limit": "1"}, timeout=15)
        if resp.ok:
            rows = resp.json()
            if rows and rows[0].get("candidate_cache"):
                other = rows[0]["candidate_cache"]
                if isinstance(other, str): other = json.loads(other)
                if other.get("build_done"):
                    print(f"  [캐시 공유] {opposite}→{job.direction}")
                    return other
    except Exception as e:
        print(f"  [캐시 공유 실패] {e}")
    return None


def _clear_candidate_cache(job_id):
    _delete_candidates_from_storage(job_id)
    try:
        import requests as _req
        _req.patch(
            f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/crawl_jobs",
            headers={"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                     "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
                     "Content-Type": "application/json"},
            params={"id": f"eq.{job_id}"},
            json={"candidate_cache": None}, timeout=15)
    except Exception:
        pass


def _run_fresh_build(list_client, policy, existing_post_keys, list_cache,
                     effective_policy, job_id, shared_client=None):
    if policy.collection_policy == "general_full":
        candidates, summary = build_general_candidates(
            list_client, policy, existing_post_keys, list_cache,
            job_id=job_id, shared_client=shared_client)
    elif policy.collection_policy == "general_monthly_stratified":
        candidates, summary = build_type2_candidates(
            list_client, policy, existing_post_keys, list_cache)
        if should_fallback_type2(summary):
            candidates, summary = build_recommend_candidates(
                list_client, policy, existing_post_keys, list_cache,
                job_id=job_id, shared_client=shared_client)
    elif policy.collection_policy == "recommend_only":
        candidates, summary = build_recommend_candidates(
            list_client, policy, existing_post_keys, list_cache,
            job_id=job_id, shared_client=shared_client)
    else:
        return [], {}

    if job_id and candidates:
        _window = summary.get("window") or (0, 0)
        _rec    = effective_policy in ("recommend_only", "recommend_only_fallback")
        threading.Thread(
            target=_save_candidate_cache,
            kwargs=dict(
                job_id=job_id, candidates=candidates,
                effective_policy=effective_policy, build_done=True,
                start_page=_window[0], end_page=_window[1],
                recommend_only=_rec),
            daemon=True
        ).start()

    return candidates, summary

def find_actual_last_page(client, gall_id, gall_type, recommend_only,
                          total_pages: int, cache) -> int:
    """
    recommend_only=True 일 때 DCInside가 리다이렉트하지 않는
    실제 마지막 페이지를 이진탐색으로 반환한다.
    recommend_only=False 이면 total_pages를 그대로 반환.
    """
    if not recommend_only:
        return total_pages
 
    # total_pages 자체가 유효한지 먼저 확인
    html = fetch_list_html(client, gall_id, gall_type, total_pages, recommend_only)
    if html is not None and html is not _REDIRECT_SENTINEL:
        return total_pages  # 리다이렉트 없음 → 전 페이지 유효
 
    # 이진탐색: 리다이렉트가 시작되는 첫 페이지를 찾는다
    lo, hi = 1, total_pages
    while lo < hi:
        mid = (lo + hi) // 2
        html = fetch_list_html(client, gall_id, gall_type, mid, recommend_only)
        if html is _REDIRECT_SENTINEL or html is None:
            hi = mid
        else:
            lo = mid + 1
 
    actual_last = lo - 1
    print(f"    [추천 실제 끝] recommend 유효 페이지: 1~{actual_last} "
          f"(total_pages={total_pages})")
    return max(1, actual_last)
 

# =========================================================
# collect_gallery
# =========================================================
def collect_gallery(policy, existing_post_keys, posts_buf, comments_buf,
                    shared_client=None, job_id=None, job=None):
    print(f"\n[COLLECT] {policy.gallery_name} ({policy.gall_id}/{policy.gall_type}) "
          f"| policy={policy.collection_policy}")

    set_current_gallery(policy.gall_id, policy.gall_type)
    reset_thread_sessions()

    list_client      = HttpClient()
    list_cache       = PageCache()
    effective_policy = policy.collection_policy
    candidates: list = []
    cached = _load_candidate_cache(job, {}) if (job_id and job is not None) else None

    if cached is not None and cached.get("has_storage"):
        start_page          = cached.get("start_page", 0)
        end_page            = cached.get("end_page", 0)
        recommend_only_flag = cached.get("recommend_only", True)
        build_done_flag     = cached.get("build_done", False)
        last_scanned        = cached.get("last_scanned_page", 0)
        stored = _load_candidates_from_storage(job_id) if job_id else None
        if stored is not None:
            # [FIX] Storage 중복 제거
            seen_nos = set()
            deduped  = []
            for row in stored:
                no = str(row["post_no"]).strip()
                if no not in seen_nos:
                    seen_nos.add(no)
                    deduped.append(row)
            print(f"  [중복제거] {len(stored)}→{len(deduped)}건")
            stored = deduped

            gall_id_norm = policy.gall_id.strip()
            resume_candidates = [
                row for row in stored
                if (gall_id_norm, str(row["post_no"]).strip()) not in existing_post_keys
            ]
            for row in resume_candidates:
                row.setdefault("gall_id", policy.gall_id)
                row.setdefault("gall_type", policy.gall_type)
                if "_dt" not in row:
                    row["_dt"] = parse_date_str(str(row.get("date_raw") or ""))
            if build_done_flag:
                candidates = resume_candidates
                print(f"  [Storage 캐시 완료] 스캔 생략 | 잔여: {len(candidates)}건 "
                      f"(스킵: {len(stored)-len(candidates)}건)")
            else:
                print(f"  [Storage 캐시 재개] p={last_scanned+1}~{end_page} | 기수집: {len(resume_candidates)}건")
                candidates, _ = _collect_rows_in_window(
                    list_client, policy, recommend_only_flag,
                    start_page, end_page, list_cache, existing_post_keys,
                    resume_candidates=resume_candidates, resume_from_page=last_scanned + 1,
                    effective_policy=effective_policy, job_id=job_id, shared_client=shared_client)
        else:
            print("  [Storage 로드 실패] → 일반 빌드")
            candidates, _ = _run_fresh_build(list_client, policy, existing_post_keys,
                                             list_cache, effective_policy, job_id,
                                             shared_client=shared_client)

    elif cached is not None and cached.get("build_done"):
        start_page          = cached.get("start_page", 0)
        end_page            = cached.get("end_page", 0)
        recommend_only_flag = cached.get("recommend_only", True)
        if start_page > 0 and end_page > 0:
            print(f"  [캐시 구버전] 재스캔 p={start_page}~{end_page}")
            candidates, _ = _collect_rows_in_window(
                list_client, policy, recommend_only_flag, start_page, end_page,
                list_cache, existing_post_keys, effective_policy=effective_policy,
                job_id=job_id, shared_client=shared_client)
        else:
            candidates, _ = _run_fresh_build(list_client, policy, existing_post_keys,
                                             list_cache, effective_policy, job_id,
                                             shared_client=shared_client)

    elif cached is not None and not cached.get("build_done"):
        cached_post_nos   = set(cached.get("post_nos", []))
        already_collected = {k[1] for k in existing_post_keys
                             if k[0] == normalize_gall_id(policy.gall_id)}
        remaining_nos     = cached_post_nos - already_collected
        print(f"  [캐시 재개] p={cached['last_scanned_page']+1}~ | 기수집: {len(remaining_nos)}건")
        candidates, _ = _collect_rows_in_window(
            list_client, policy, cached["recommend_only"],
            cached["start_page"], cached["end_page"], list_cache, existing_post_keys,
            resume_from_page=cached["last_scanned_page"] + 1,
            effective_policy=effective_policy, job_id=job_id, shared_client=shared_client)
        existing_nos = {k[1] for k in existing_post_keys
                        if k[0] == normalize_gall_id(policy.gall_id)}
        extra_nos = remaining_nos - {row["post_no"] for row in candidates} - existing_nos
        if extra_nos:
            print(f"  [중단 전 재스캔] {len(extra_nos)}건")
            extra, _ = _collect_rows_in_window(
                list_client, policy, cached["recommend_only"],
                cached["start_page"], cached["last_scanned_page"], list_cache, existing_post_keys,
                filter_fn=lambda row: row["post_no"] in extra_nos,
                effective_policy=effective_policy, job_id=None)
            candidates = candidates + extra
        _save_candidate_cache(
            job_id=job_id, candidates=candidates, effective_policy=effective_policy,
            build_done=True, start_page=cached["start_page"], end_page=cached["end_page"],
            recommend_only=cached["recommend_only"])
    else:
        if policy.collection_policy not in ("general_full", "general_monthly_stratified", "recommend_only"):
            print("  [SKIP] unsupported policy"); return 0, 0
        candidates, _ = _run_fresh_build(list_client, policy, existing_post_keys,
                                         list_cache, effective_policy, job_id,
                                         shared_client=shared_client)

    total = len(candidates)
    print(f"  상세 수집 대상: {total} | effective_policy={effective_policy}")

    if total == 0:
        return 0, 0   # _clear_candidate_cache 호출 없이 바로 리턴


    total_saved_posts    = 0
    total_saved_comments = 0
    total_done           = 0
    page_posts:    list  = []
    page_comments: list  = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as detail_ex, \
         ThreadPoolExecutor(max_workers=CMT_WORKERS)  as cmt_ex:

        def _task(meta):
            return fetch_post_detail(meta, effective_policy, cmt_ex)

        cand_iter = iter(candidates)
        pending: dict[Future, dict] = {}

        for meta in itertools.islice(cand_iter, WINDOW_SIZE):
            pending[detail_ex.submit(_task, meta)] = meta

        flush_count = 0

        # collect_gallery 내부 while pending 루프 교체

        while pending:
            done, _ = concurrent.futures.wait(
                pending, timeout=120,                          # ← timeout 추가
                return_when=concurrent.futures.FIRST_COMPLETED)

            if not done:
                # 120초 동안 완료된 태스크 없음 → 걸린 태스크 강제 취소 후 스킵
                print(f"    [경고] {len(pending)}개 태스크 120s 무응답 → 강제 취소")
                for fut in list(pending):
                    fut.cancel()
                    meta = pending.pop(fut)
                    total_done += 1
                    print(f"      [TIMEOUT SKIP] post_no={meta['post_no']}")
                continue

            for fut in done:
                meta = pending.pop(fut)
                try:
                    post_row, comments = fut.result(timeout=5)   # ← result에도 timeout
                    if post_row:
                        page_posts.append(post_row)
                        page_comments.extend(comments)
                        update_existing_post_keys(existing_post_keys, [post_row])
                except concurrent.futures.TimeoutError:
                    print(f"    [RESULT TIMEOUT] post_no={meta['post_no']} 스킵")
                except Exception as e:
                    print(f"    [DETAIL ERROR] post_no={meta['post_no']} → {e}")

                total_done  += 1
                flush_count += 1

                try:
                    next_meta = next(cand_iter)
                    pending[detail_ex.submit(_task, next_meta)] = next_meta
                except StopIteration:
                    pass

            if flush_count >= FLUSH_EVERY:
                total_saved_posts    += len(page_posts)
                total_saved_comments += len(page_comments)
                posts_buf.add(page_posts)
                comments_buf.add(page_comments)
                page_posts.clear()
                page_comments.clear()
                flush_count = 0
                print(f"    진행: {total_done}/{total} | rate={_dc_limiter.rate:.2f}req/s")
                if shared_client and job_id:
                    try: shared_client.heartbeat_job(job_id=job_id, last_page=total_done)
                    except Exception as e: print(f"    [HEARTBEAT 실패] {e}")

        total_saved_posts    += len(page_posts)
        total_saved_comments += len(page_comments)
        posts_buf.add(page_posts)
        comments_buf.add(page_comments)

    if job_id:
        _clear_candidate_cache(job_id)

    print(f"  저장 완료: posts={total_saved_posts}, comments={total_saved_comments}")
    return total_saved_posts, total_saved_comments


# =========================================================
# Supabase job → GalleryPolicy
# =========================================================
def job_to_gallery_policy(job: CrawlJob) -> GalleryPolicy:
    return GalleryPolicy(
        gallery_name=job.gall_name, gall_id=job.gall_id,
        gall_type=job.gall_type, total_pages=job.page_end,
        collection_policy=job.collection_policy or "")


# =========================================================
# 메인
# =========================================================
def main():
    shared_client = SharedCheckpointClient(claimed_by=WORKER_NAME)
    existing_post_csv_paths = discover_existing_post_csvs(RAW_DIR, POSTS_CSV)
    existing_post_keys      = load_existing_post_keys(existing_post_csv_paths, KEY_CACHE_PATH)

    print("=" * 90)
    print(f"[POLICY SHARED] worker={WORKER_NAME} | direction={DIRECTION}")
    print(f"[rate] 초기={INITIAL_RATE} | 최소={MIN_RATE} | 최대={MAX_RATE} (자동조절)")
    print(f"[workers] detail={MAX_WORKERS} | cmt={CMT_WORKERS} | window={WINDOW_SIZE}")
    print(f"[기존 post key 수] {len(existing_post_keys):,}")
    print(f"[출력 posts]    {POSTS_CSV}")
    print(f"[출력 comments] {COMMENTS_CSV}")
    print("=" * 90)

    posts_buf    = CsvBuffer(POSTS_CSV,    POST_COLUMNS,    flush_every=500)
    comments_buf = CsvBuffer(COMMENTS_CSV, COMMENT_COLUMNS, flush_every=500)
    total_posts = total_comments = 0

    try:
        while True:
            job = shared_client.claim_next_job(direction=DIRECTION, stale_minutes=30)
            if job is None:
                print("[종료] claim 가능한 작업 없음"); break
            policy = job_to_gallery_policy(job)
            print(f"\n[CLAIM] {policy.gallery_name} ({policy.gall_id}/{policy.gall_type})"
                  f" | policy={policy.collection_policy} | job_id={job.id}")
            try:
                p_cnt, c_cnt = collect_gallery(
                    policy, existing_post_keys, posts_buf, comments_buf,
                    shared_client=shared_client, job_id=job.id, job=job)
                total_posts    += p_cnt
                total_comments += c_cnt
                shared_client.complete_job(job.id)
                print(f"  [완료] job_id={job.id} | posts={p_cnt} | comments={c_cnt}")
                save_existing_post_keys_cache(
                    existing_post_keys, existing_post_csv_paths + [POSTS_CSV], KEY_CACHE_PATH)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"  [실패] {policy.gallery_name} → {msg}")
                shared_client.fail_job(job.id, msg)
                time.sleep(random.uniform(3, 8))
    finally:
        posts_buf.flush()
        comments_buf.flush()
        save_existing_post_keys_cache(
            existing_post_keys, existing_post_csv_paths + [POSTS_CSV], KEY_CACHE_PATH)

    print("\n" + "=" * 90)
    print(f"[전체 종료] posts={total_posts}, comments={total_comments}")
    print(f"posts csv:    {POSTS_CSV}")
    print(f"comments csv: {COMMENTS_CSV}")
    print("=" * 90)


if __name__ == "__main__":
    main()