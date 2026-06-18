import os
import re
import json
import time
import math
import random
import pickle
import html as html_lib
import threading
from glob import glob
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import pandas as pd
import requests
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
DIVISION_DIR = os.path.join(PROJECT_ROOT, "data", "gall_division")

POLICY_CSV = os.path.join(DIVISION_DIR, "gallery_policy_decision.csv")
POSTS_CSV = os.path.join(RAW_DIR, "dci_posts_policy_reverse.csv")
COMMENTS_CSV = os.path.join(RAW_DIR, "dci_comments_policy_reverse.csv")
CHECKPOINT_JSON = os.path.join(RAW_DIR, "policy_collection_checkpoint.json")
KEY_CACHE_PATH = os.path.join(RAW_DIR, "existing_post_keys_policy_reverse.pkl")

os.makedirs(RAW_DIR, exist_ok=True)


# =========================================================
# 워커 식별
# =========================================================
WORKER_NAME = "kte_policy_reverse_01"   # ← 이 파일에서만 바꾸면 됨
DIRECTION   = "policy_reverse"          # policy_forward | policy_reverse

# =========================================================
# 설정
# =========================================================
START_DATE = "2013-01-01"
END_DATE = "2023-12-31"

START_DT = datetime.fromisoformat(START_DATE)
END_DT = datetime.fromisoformat(END_DATE)

MIN_DELAY = 1.0
MAX_DELAY = 2.2
REQUEST_TIMEOUT = 15
MAX_LIST_RETRY = 3
MAX_DETAIL_RETRY = 3
MAX_WORKERS = 8   # 상세 수집 병렬 워커
LIST_FETCH_WORKERS = 4   # 목록 페이지 병렬 수집용

# 목록 스캔 전용 딜레이 (상세 수집보다 짧게)
SCAN_MIN_DELAY = 0.3
SCAN_MAX_DELAY = 0.8
SCAN_WORKERS   = 8       # 목록 스캔 병렬 워커 수

TYPE2_MIN_RECOMMEND = 2
TYPE2_MIN_COMMENT_HINT = 2
TYPE2_MIN_MONTHLY_QUOTA = 100
TYPE2_QUOTA_RATIO = 0.30
TYPE2_MAX_MONTHLY_QUOTA = 500

TYPE2_FALLBACK_MIN_SELECTED = 100
TYPE2_FALLBACK_MIN_MONTHS = 12
TYPE2_FALLBACK_MIN_TARGET_ROWS = 50

HEADERS = {
    "Referer": "https://gall.dcinside.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
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
    "collection_policy", "collected_by"
]

COMMENT_COLUMNS = [
    "gall_id", "post_no", "cmt_no", "cmt_writer",
    "cmt_date", "cmt_body", "cmt_rcnt",
    "collection_policy", "collected_by"
]

try:
    import lxml  # noqa
    HTML_PARSER = "lxml"
except Exception:
    HTML_PARSER = "html.parser"

# =========================================================
# 컴파일된 정규식 (hot path 최적화)
# =========================================================
_INT_RE = re.compile(r"-?\d+")
_DIGITS_RE = re.compile(r"[^\d]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DATE_FULL_RE = re.compile(r"(\d{4})[.\-](\d{2})[.\-](\d{2})")
_POST_NO_RE = re.compile(r"no=(\d+)")
_E_S_N_O_RE = re.compile(r"e_s_n_o=([a-zA-Z0-9]+)")


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
# Adaptive Concurrency Controller
# =========================================================
class AdaptiveConcurrency:
    def __init__(self, min_workers: int = 2, max_workers: int = MAX_WORKERS):
        self.min_workers  = min_workers
        self.max_workers  = max_workers
        self._workers     = min_workers
        self._times: list = []
        self._errors      = 0
        self._ok_streak   = 0
        self._lock        = threading.Lock()
        self.SAMPLE_SIZE  = 20
        self.SCALE_UP_AT  = 15
        self.SLOW_THRESH  = 8.0

    def record(self, elapsed: float, success: bool):
        with self._lock:
            if success:
                self._times.append(elapsed)
                if len(self._times) > self.SAMPLE_SIZE:
                    self._times.pop(0)
                self._errors   = 0
                self._ok_streak += 1

                avg = sum(self._times) / len(self._times) if self._times else 0

                if avg > self.SLOW_THRESH and self._workers > self.min_workers:
                    self._workers -= 1
                    self._ok_streak = 0
                    print(f"    [ACC] 응답 느림({avg:.1f}s) → workers={self._workers}")

                elif self._ok_streak >= self.SCALE_UP_AT and self._workers < self.max_workers:
                    self._workers += 1
                    self._ok_streak = 0
                    print(f"    [ACC] 응답 양호({avg:.1f}s) → workers={self._workers}")
            else:
                self._errors += 1
                self._ok_streak = 0
                if self._errors >= 3 and self._workers > self.min_workers:
                    self._workers = max(self.min_workers, self._workers - 2)
                    print(f"    [ACC] 에러 감지 → workers={self._workers}")

    def get_workers(self) -> int:
        with self._lock:
            return self._workers

ACC = AdaptiveConcurrency(min_workers=8, max_workers=MAX_WORKERS)

# =========================================================
# CSV 버퍼
# =========================================================
class CsvBuffer:
    def __init__(self, path: str, columns: list, flush_every: int = 500):
        self.path = path
        self.columns = columns
        self.flush_every = flush_every
        self.buf: list = []
        self._lock = threading.Lock()

    def add(self, rows: list):
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
        df = pd.DataFrame(self.buf).reindex(columns=self.columns)
        df.to_csv(
            self.path,
            mode="a",
            header=not os.path.exists(self.path),
            index=False,
            encoding="utf-8-sig",
        )
        self.buf.clear()


# =========================================================
# 스레드 안전 페이지 캐시
# =========================================================
class PageCache:
    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self._cache.get(key)

    def set(self, key, value):
        with self._lock:
            self._cache[key] = value

    def __contains__(self, key):
        with self._lock:
            return key in self._cache


# =========================================================
# 유틸
# =========================================================
def sleep_brief():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def safe_int(v, default=0):
    if v is None:
        return default
    text = str(v).strip().replace(",", "")
    m = _INT_RE.search(text)
    if not m:
        return default
    try:
        return int(m.group())
    except Exception:
        return default


def normalize_post_no(v) -> str:
    return str(v).strip()


def normalize_gall_id(v) -> str:
    return str(v).strip()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    return "https:" + url if url.startswith("//") else url


def is_external_url(url: str) -> bool:
    if not url:
        return False
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    dc_domains = ["dcinside.com", "gall.dcinside.com", "image.dcinside.com", "nstatic.dcinside.com"]
    return bool(netloc) and not any(d in netloc for d in dc_domains)


def parse_comment_count(text: str) -> int:
    if not text:
        return 0
    digits = _DIGITS_RE.sub("", text)
    if not digits:
        return 0
    try:
        return int(digits)
    except Exception:
        return 0


def clean_comment_memo(memo: str) -> str:
    text = _HTML_TAG_RE.sub("", memo)
    return html_lib.unescape(text).strip()


@lru_cache(maxsize=8192)
def parse_date_str(raw: str | None):
    if not raw:
        return None

    text = raw.strip()
    if not text:
        return None

    now = datetime.now()

    full_formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in full_formats:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass

    try:
        dt = datetime.strptime(text, "%m.%d %H:%M")
        return dt.replace(year=now.year)
    except Exception:
        pass

    try:
        dt = datetime.strptime(text, "%m.%d")
        return dt.replace(year=now.year)
    except Exception:
        pass

    try:
        dt = datetime.strptime(text, "%H:%M")
        return dt.replace(year=now.year, month=now.month, day=now.day)
    except Exception:
        pass

    m = _DATE_FULL_RE.search(text)
    if m:
        y, mm, d = m.groups()
        try:
            return datetime(int(y), int(mm), int(d))
        except Exception:
            return None

    return None


def append_to_csv(rows, filepath, columns):
    if not rows:
        return
    df = pd.DataFrame(rows).reindex(columns=columns)
    df.to_csv(
        filepath,
        mode="a",
        header=not os.path.exists(filepath),
        index=False,
        encoding="utf-8-sig",
    )


# =========================================================
# 세션
# =========================================================
class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.headers["User-Agent"] = random.choice(USER_AGENTS)

    def get(self, url: str):
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 0:
                return resp
            return None
        except requests.RequestException:
            return None

    def post(self, url: str, data: dict, headers=None):
        try:
            resp = self.session.post(url, data=data, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 0:
                return resp
            return None
        except requests.RequestException:
            return None


# =========================================================
# 정책 / 체크포인트 / 기존키 로드
# =========================================================
def load_gallery_policy_table() -> list[GalleryPolicy]:
    if not os.path.exists(POLICY_CSV):
        raise FileNotFoundError(f"파일 없음: {POLICY_CSV}")

    df = pd.read_csv(POLICY_CSV)
    required = {"gallery_name", "gall_id", "gall_type", "total_pages", "collection_policy"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"gallery_policy_decision.csv 누락 컬럼: {sorted(missing)}")

    rows = []
    for r in df.to_dict("records"):
        policy = str(r["collection_policy"]).strip()
        if policy == "gap_unassigned":
            continue
        rows.append(
            GalleryPolicy(
                gallery_name=str(r["gallery_name"]).strip(),
                gall_id=str(r["gall_id"]).strip(),
                gall_type=str(r["gall_type"]).strip(),
                total_pages=int(r["total_pages"]),
                collection_policy=policy,
            )
        )
    return rows


def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_JSON):
        with open(CHECKPOINT_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "started_at": str(datetime.now()),
        "done_galleries": {},
        "last_gallery": None,
        "last_updated": None,
    }


def save_checkpoint(cp: dict):
    cp["last_updated"] = str(datetime.now())
    with open(CHECKPOINT_JSON, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)


def discover_existing_post_csvs(output_dir: str, current_posts_csv: str) -> list[str]:
    candidates = set()
    legacy_shared = os.path.join(output_dir, "dci_posts.csv")
    if os.path.exists(legacy_shared):
        candidates.add(legacy_shared)
    for path in glob(os.path.join(output_dir, "dci_posts*.csv")):
        candidates.add(path)
    candidates.add(current_posts_csv)
    return sorted(candidates)


def build_file_signature(csv_paths: list[str]) -> dict:
    signature = {}
    for path in csv_paths:
        if not os.path.exists(path):
            continue
        st = os.stat(path)
        signature[path] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    return signature


def is_cache_valid(cache_obj: dict, csv_paths: list[str]) -> bool:
    if not isinstance(cache_obj, dict):
        return False
    if "keys" not in cache_obj or "signature" not in cache_obj:
        return False
    return build_file_signature(csv_paths) == cache_obj.get("signature", {})


def save_existing_post_keys_cache(existing_post_keys: set, csv_paths: list[str], cache_path: str):
    cache_obj = {
        "signature": build_file_signature(csv_paths),
        "keys": existing_post_keys,
        "saved_at": str(datetime.now()),
    }
    with open(cache_path, "wb") as f:
        pickle.dump(cache_obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_existing_post_keys(csv_paths: list[str], cache_path: str) -> set[tuple[str, str]]:
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cache_obj = pickle.load(f)
            if is_cache_valid(cache_obj, csv_paths):
                return cache_obj["keys"]
        except Exception:
            pass

    existing_keys = set()
    for path in csv_paths:
        if not os.path.exists(path):
            continue
        try:
            chunk_iter = pd.read_csv(
                path, usecols=["gall_id", "post_no"], chunksize=200000, low_memory=False
            )
        except Exception:
            continue

        for chunk in chunk_iter:
            chunk = chunk.dropna(subset=["gall_id", "post_no"]).copy()
            chunk["gall_id"] = chunk["gall_id"].astype(str).str.strip()
            chunk["post_no"] = chunk["post_no"].astype(str).str.strip()
            existing_keys.update(zip(chunk["gall_id"], chunk["post_no"]))

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
# 목록 파싱 + 페이지 요약
# =========================================================
def fetch_list_html(client: HttpClient, gall_id: str, gall_type: str, page: int, recommend_only: bool):
    url = make_list_url(gall_id, gall_type, page, recommend_only=recommend_only)
    for _ in range(MAX_LIST_RETRY):
        resp = client.get(url)
        if resp is not None:
            # 리다이렉트 감지: 요청 페이지와 실제 응답 페이지가 다르면 None 반환
            if page > 1 and f"page={page}" not in resp.url:
                return None
            return resp.text
        time.sleep(random.uniform(1.5, 3.5))
    return None


def parse_post_list(html: str, gall_id: str, gall_type: str) -> list[dict]:
    soup = BeautifulSoup(html, HTML_PARSER)
    rows = soup.select("tr.ub-content")
    results = []

    for row in rows:
        num_tag = row.select_one("td.gall_num")
        title_tag = row.select_one("td.gall_tit a:not(.reply_numbox)")
        date_tag = row.select_one("td.gall_date")

        if not (num_tag and title_tag and date_tag):
            continue

        num_text = num_tag.get_text(strip=True)
        if not num_text.isdigit():
            continue

        href = title_tag.get("href", "")
        m = _POST_NO_RE.search(href)
        if not m:
            continue

        e = _E_S_N_O_RE.search(href)
        writer_tag = row.select_one("td.gall_writer")
        count_tag = row.select_one("td.gall_count")
        recom_tag = row.select_one("td.gall_recommend")
        reply_tag = row.select_one("a.reply_numbox")

        date_title = (date_tag.get("title") or "").strip()
        date_text = date_tag.get_text(strip=True)
        date_raw = date_title if date_title else date_text

        results.append({
            "gall_id": gall_id,
            "gall_type": gall_type,
            "post_no": m.group(1),
            "title": title_tag.get("title") or title_tag.get_text(strip=True),
            "writer": writer_tag.get_text(strip=True) if writer_tag else "",
            "date_raw": date_raw,
            "views": count_tag.get_text(strip=True).replace(",", "") if count_tag else "0",
            "recommend": safe_int(recom_tag.get_text(strip=True) if recom_tag else "0", 0),
            "comment_count_hint": parse_comment_count(reply_tag.get_text(strip=True) if reply_tag else ""),
            "_e_s_n_o": e.group(1) if e else "",
            "_href": make_view_url(gall_id, gall_type, m.group(1)),
        })

    return results


def summarize_page(rows: list[dict], page_no: int) -> PageSummary:
    dts = [dt for row in rows if (dt := parse_date_str(row["date_raw"])) is not None]
    return PageSummary(
        page_no=page_no,
        newest_dt=max(dts) if dts else None,
        oldest_dt=min(dts) if dts else None,
        row_count=len(rows),
    )


# =========================================================
# 페이지 캐시 키 / 조회
# =========================================================
def page_cache_key(gall_id: str, gall_type: str, recommend_only: bool, page_no: int) -> tuple:
    return gall_id, gall_type, recommend_only, page_no


def get_page_rows_and_summary(
    client: HttpClient,
    gall_id: str,
    gall_type: str,
    recommend_only: bool,
    page_no: int,
    cache: PageCache,
):
    key = page_cache_key(gall_id, gall_type, recommend_only, page_no)
    cached = cache.get(key)
    if cached is not None:
        return cached["rows"], cached["summary"]

    html = fetch_list_html(client, gall_id, gall_type, page_no, recommend_only=recommend_only)
    if html is None:
        entry = {"rows": [], "summary": PageSummary(page_no, None, None, 0)}
        cache.set(key, entry)
        return entry["rows"], entry["summary"]

    rows = parse_post_list(html, gall_id, gall_type)
    summary = summarize_page(rows, page_no)
    cache.set(key, {"rows": rows, "summary": summary})
    return rows, summary


def prefetch_pages_parallel(
    gall_id: str,
    gall_type: str,
    recommend_only: bool,
    pages: list[int],
    cache: PageCache,
):
    missing = [
        p for p in pages
        if cache.get(page_cache_key(gall_id, gall_type, recommend_only, p)) is None
    ]
    if not missing:
        return

    def _fetch(page_no):
        client = HttpClient()
        key = page_cache_key(gall_id, gall_type, recommend_only, page_no)
        html = fetch_list_html(client, gall_id, gall_type, page_no, recommend_only=recommend_only)
        if html is None:
            return key, [], PageSummary(page_no, None, None, 0)
        rows = parse_post_list(html, gall_id, gall_type)
        summary = summarize_page(rows, page_no)
        return key, rows, summary

    with ThreadPoolExecutor(max_workers=LIST_FETCH_WORKERS) as ex:
        futures = [ex.submit(_fetch, p) for p in missing]
        for future in as_completed(futures):
            try:
                key, rows, summary = future.result()
                cache.set(key, {"rows": rows, "summary": summary})
            except Exception:
                pass


# =========================================================
# 타깃 기간 페이지 범위 탐색
# =========================================================
def find_first_page_reaching_end_date(
    client: HttpClient,
    policy: GalleryPolicy,
    recommend_only: bool,
    cache: PageCache,
):
    _, s1 = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, 1, cache)

    if s1.oldest_dt is None:
        return None
    if s1.oldest_dt <= END_DT:
        return 1

    lo = 1
    hi = 2

    while hi <= policy.total_pages:
        hi = min(hi, policy.total_pages)
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.oldest_dt is not None and sh.oldest_dt <= END_DT:
            break
        if hi == policy.total_pages:
            return None
        lo = hi
        hi *= 2

    if hi > policy.total_pages:
        hi = policy.total_pages
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.oldest_dt is None or sh.oldest_dt > END_DT:
            return None

    left, right, ans = lo + 1, hi, hi
    while left <= right:
        mid = (left + right) // 2
        _, sm = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, mid, cache)
        if sm.oldest_dt is not None and sm.oldest_dt <= END_DT:
            ans = mid
            right = mid - 1
        else:
            left = mid + 1

    return ans


def find_first_page_before_start_date(
    client: HttpClient,
    policy: GalleryPolicy,
    recommend_only: bool,
    start_page: int,
    cache: PageCache,
):
    lo = start_page
    hi = min(policy.total_pages, max(start_page, 2))

    while hi <= policy.total_pages:
        hi = min(hi, policy.total_pages)
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.newest_dt is not None and sh.newest_dt < START_DT:
            break
        if hi == policy.total_pages:
            return policy.total_pages + 1
        lo = hi
        hi *= 2

    if hi > policy.total_pages:
        hi = policy.total_pages
        _, sh = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, hi, cache)
        if sh.newest_dt is None or sh.newest_dt >= START_DT:
            return policy.total_pages + 1

    left, right, ans = lo + 1, hi, hi
    while left <= right:
        mid = (left + right) // 2
        _, sm = get_page_rows_and_summary(client, policy.gall_id, policy.gall_type, recommend_only, mid, cache)
        if sm.newest_dt is not None and sm.newest_dt < START_DT:
            ans = mid
            right = mid - 1
        else:
            left = mid + 1

    return ans


def find_target_page_window(
    client: HttpClient,
    policy: GalleryPolicy,
    recommend_only: bool,
    cache: PageCache,
):
    start_page = find_first_page_reaching_end_date(client, policy, recommend_only, cache)
    if start_page is None:
        return None, None

    first_old_page = find_first_page_before_start_date(client, policy, recommend_only, start_page, cache)
    end_page = min(policy.total_pages, first_old_page - 1)

    if end_page < start_page:
        return None, None

    return start_page, end_page


# =========================================================
# 상세 파싱
# =========================================================
def extract_post_assets(soup):
    body_tag = soup.select_one("div.write_div")
    if not body_tag:
        return {
            "body_text": "",
            "has_image": False,
            "image_count": 0,
            "image_urls_json": "[]",
            "has_video": False,
            "video_count": 0,
            "video_urls_json": "[]",
            "has_external_link": False,
            "external_link_count": 0,
            "external_links_json": "[]",
        }

    body_text = body_tag.get_text("\n", strip=True)

    imgs, seen_i = [], set()
    for t in body_tag.select("img"):
        s = normalize_url(t.get("src", ""))
        if s and s not in seen_i:
            seen_i.add(s)
            imgs.append(s)

    vids, seen_v = [], set()
    for t in body_tag.select("video"):
        s = normalize_url(t.get("src", ""))
        if s and s not in seen_v:
            seen_v.add(s)
            vids.append(s)
        for src in t.select("source"):
            s = normalize_url(src.get("src", ""))
            if s and s not in seen_v:
                seen_v.add(s)
                vids.append(s)
    for t in body_tag.select("iframe"):
        s = normalize_url(t.get("src", ""))
        if s and s not in seen_v:
            seen_v.add(s)
            vids.append(s)

    links, seen_l = [], set()
    for t in body_tag.select("a[href]"):
        h = normalize_url(t.get("href", ""))
        if h and is_external_url(h) and h not in seen_l:
            seen_l.add(h)
            links.append(h)

    return {
        "body_text": body_text,
        "has_image": bool(imgs),
        "image_count": len(imgs),
        "image_urls_json": json.dumps(imgs, ensure_ascii=False),
        "has_video": bool(vids),
        "video_count": len(vids),
        "video_urls_json": json.dumps(vids, ensure_ascii=False),
        "has_external_link": bool(links),
        "external_link_count": len(links),
        "external_links_json": json.dumps(links, ensure_ascii=False),
    }


def _fetch_comment_page(
    gall_id, gall_type, post_no, e_s_n_o, gallery_no, collection_policy, page_no
) -> tuple[int, list, int]:
    client = HttpClient()
    cmt_headers = {
        **client.session.headers,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    data = {
        "id": gall_id, "no": post_no,
        "cmt_id": gall_id, "cmt_no": post_no,
        "e_s_n_o": e_s_n_o,
        "comment_page": str(page_no),
        "_GALLTYPE_": gall_type,
        "cur_cate": "finance",
    }
    if gallery_no:
        data["gallery_no"] = gallery_no

    time.sleep(random.uniform(0.1, 0.2))
    resp = client.post("https://gall.dcinside.com/board/comment/", data=data, headers=cmt_headers)
    if resp is None:
        return page_no, [], 0

    try:
        result = resp.json()
    except Exception:
        return page_no, [], 0

    total_cnt = safe_int(result.get("total_cnt"), 0)
    comments = []
    for cmt in result.get("comments", []):
        try:
            if cmt.get("del_yn") == "Y" or cmt.get("is_delete") == "1":
                continue
            memo = cmt.get("memo", "")
            if not memo:
                continue
            clean = clean_comment_memo(memo)
            if not clean:
                continue
            reg = cmt.get("reg_date") or ""
            comments.append({
                "gall_id": gall_id, "post_no": post_no,
                "cmt_no": cmt.get("no", ""),
                "cmt_writer": cmt.get("name", ""),
                "cmt_date": reg.split()[0] if reg else "",
                "cmt_body": clean,
                "cmt_rcnt": cmt.get("rcnt", "0"),
                "collection_policy": collection_policy,
                "collected_by": "policy_collector",
            })
        except Exception:
            continue
    return page_no, comments, total_cnt


def get_comments(client: HttpClient, gall_id, gall_type, post_no, e_s_n_o, gallery_no, collection_policy):
    _, page1_comments, total_cnt = _fetch_comment_page(
        gall_id, gall_type, post_no, e_s_n_o, gallery_no, collection_policy, 1
    )
    if not page1_comments or total_cnt == 0:
        return page1_comments

    CMT_PER_PAGE = 100
    total_pages  = math.ceil(total_cnt / CMT_PER_PAGE)

    if total_pages <= 1:
        return page1_comments

    all_comments = list(page1_comments)
    remaining = list(range(2, total_pages + 1))

    with ThreadPoolExecutor(max_workers=min(4, len(remaining))) as ex:
        futures = {
            ex.submit(
                _fetch_comment_page,
                gall_id, gall_type, post_no, e_s_n_o, gallery_no, collection_policy, p
            ): p for p in remaining
        }
        for future in as_completed(futures):
            _, cmts, _ = future.result()
            all_comments.extend(cmts)

    return all_comments


def fetch_post_detail(client: HttpClient, post_meta: dict, collection_policy: str):
    for _ in range(MAX_DETAIL_RETRY):
        t0 = time.time()
        resp = client.get(post_meta["_href"])
        sleep_brief()

        if resp is None:
            ACC.record(time.time() - t0, success=False)
            time.sleep(random.uniform(1.5, 3.5))
            continue

        try:
            soup = BeautifulSoup(resp.text, HTML_PARSER)
            assets = extract_post_assets(soup)

            up = soup.select_one(".btn_recommend_box .up_num")
            down = soup.select_one(".btn_recommend_box .sup_num")
            et = soup.select_one("input#e_s_n_o") or soup.select_one("input[name=e_s_n_o]")
            e_s_n_o = et.get("value", "") if et else post_meta.get("_e_s_n_o", "")

            gallery_no = ""
            for inp in soup.select("input[name=gallery_no]"):
                v = inp.get("value", "")
                if v.isdigit() and v != post_meta["post_no"]:
                    gallery_no = v
                    break

            dt = soup.select_one("span.gall_date")
            date_str = dt.get("title", "").split()[0] if dt else post_meta["date_raw"]

            if int(post_meta.get("comment_count_hint", 0) or 0) == 0:
                comments = []
            else:
                comments = get_comments(
                    client=client,
                    gall_id=post_meta["gall_id"],
                    gall_type=post_meta["gall_type"],
                    post_no=post_meta["post_no"],
                    e_s_n_o=e_s_n_o,
                    gallery_no=gallery_no,
                    collection_policy=collection_policy,
                )

            post_row = {
                "gall_id": post_meta["gall_id"],
                "gall_type": post_meta["gall_type"],
                "post_no": post_meta["post_no"],
                "title": post_meta["title"],
                "writer": post_meta["writer"],
                "date": date_str,
                "views": safe_int(post_meta["views"], 0),
                "recommend": safe_int(up.get_text(strip=True) if up else post_meta["recommend"], 0),
                "unrecommend": safe_int(down.get_text(strip=True) if down else 0, 0),
                "body": assets["body_text"],
                "has_image": assets["has_image"],
                "image_count": assets["image_count"],
                "image_urls_json": assets["image_urls_json"],
                "has_video": assets["has_video"],
                "video_count": assets["video_count"],
                "video_urls_json": assets["video_urls_json"],
                "has_external_link": assets["has_external_link"],
                "external_link_count": assets["external_link_count"],
                "external_links_json": assets["external_links_json"],
                "collection_policy": collection_policy,
                "collected_by": "policy_collector",
            }

            ACC.record(time.time() - t0, success=True)
            return post_row, comments

        except Exception:
            ACC.record(time.time() - t0, success=False)
            time.sleep(random.uniform(1.0, 2.0))

    return None, []


# =========================================================
# 정책별 후보 빌드
# =========================================================
def _collect_rows_in_window(
    client: HttpClient,
    policy: GalleryPolicy,
    recommend_only: bool,
    start_page: int,
    end_page: int,
    cache: PageCache,
    existing_post_keys: set,
    filter_fn=None,
    resume_candidates: list | None = None,
    resume_from_page: int = 0,
    effective_policy: str = "",
    job_id: int | None = None,
    shared_client=None,
) -> tuple[list[dict], dict]:
    actual_start = resume_from_page if resume_from_page > start_page else start_page
    all_pages = list(range(actual_start, end_page + 1))

    candidates = list(resume_candidates) if resume_candidates else []
    in_target_rows = 0
    skipped_existing = 0
    SAVE_EVERY   = 25
    BATCH_SIZE   = SCAN_WORKERS
    total_pages_to_scan = len(all_pages)

    print(f"    [페이지 스캔 시작] {total_pages_to_scan}페이지 | 병렬={BATCH_SIZE}워커 | 딜레이={SCAN_MIN_DELAY}~{SCAN_MAX_DELAY}s")

    def _fetch_page(page_no: int) -> tuple[int, list]:
        scan_client = HttpClient()
        time.sleep(random.uniform(SCAN_MIN_DELAY, SCAN_MAX_DELAY))
        html = fetch_list_html(scan_client, policy.gall_id, policy.gall_type, page_no, recommend_only)
        if html is None:
            return page_no, []
        return page_no, parse_post_list(html, policy.gall_id, policy.gall_type)

    batch_idx = 0
    for batch_start in range(0, total_pages_to_scan, BATCH_SIZE):
        batch_pages = all_pages[batch_start: batch_start + BATCH_SIZE]
        page_rows_map: dict[int, list] = {}

        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            futures = {ex.submit(_fetch_page, p): p for p in batch_pages}
            for future in as_completed(futures):
                page_no, rows = future.result()
                page_rows_map[page_no] = rows

        for page in batch_pages:
            for row in page_rows_map.get(page, []):
                dt = parse_date_str(row["date_raw"])
                if dt is None or dt < START_DT or dt > END_DT:
                    continue
                in_target_rows += 1

                key = (normalize_gall_id(row["gall_id"]), normalize_post_no(row["post_no"]))
                if key in existing_post_keys:
                    skipped_existing += 1
                    continue

                if filter_fn is not None and not filter_fn(row):
                    continue

                candidates.append(row)

        batch_idx += 1
        scanned_so_far = min(batch_start + BATCH_SIZE, total_pages_to_scan)
        is_last_batch = (scanned_so_far == total_pages_to_scan)  # ← 추가
        if batch_idx % SAVE_EVERY == 0 or is_last_batch:
            print(f"    [스캔 진행] {scanned_so_far}/{total_pages_to_scan}페이지 | 후보 {len(candidates)}건")
            if shared_client and job_id:
                try:
                    shared_client.heartbeat_job(job_id=job_id)
                except Exception:
                    pass
            if job_id and effective_policy:
                _save_candidate_cache(
                    job_id=job_id,
                    candidates=candidates,
                    effective_policy=effective_policy,
                    build_done=is_last_batch,   # ← False → is_last_batch
                    last_scanned_page=batch_pages[-1],
                    start_page=start_page,
                    end_page=end_page,
                    recommend_only=recommend_only,
                )

    return candidates, {
        "window": (start_page, end_page),
        "in_target_rows": in_target_rows,
        "skipped_existing": skipped_existing,
    }


def build_general_candidates(client: HttpClient, policy: GalleryPolicy, existing_post_keys: set, cache: PageCache, job_id: int | None = None, shared_client=None):
    start_page, end_page = find_target_page_window(client, policy, recommend_only=False, cache=cache)
    if start_page is None:
        print("    [general] 타깃 기간 페이지 없음")
        return [], {"window": None, "selected": 0}

    print(f"    [general window] {start_page} ~ {end_page}")

    candidates, meta = _collect_rows_in_window(
        client, policy, False, start_page, end_page, cache, existing_post_keys,
        effective_policy="general_full",
        job_id=job_id,
        shared_client=shared_client,
    )

    summary = {**meta, "selected": len(candidates)}
    print(
        f"    [general summary] window={start_page}~{end_page}, "
        f"in_target_rows={meta['in_target_rows']}, "
        f"skipped_existing={meta['skipped_existing']}, "
        f"selected={len(candidates)}"
    )
    return candidates, summary


def build_recommend_candidates(client: HttpClient, policy: GalleryPolicy, existing_post_keys: set, cache: PageCache, job_id: int | None = None, shared_client=None):
    start_page, end_page = find_target_page_window(client, policy, recommend_only=True, cache=cache)
    if start_page is None:
        print("    [recommend] 타깃 기간 페이지 없음")
        return [], {"window": None, "selected": 0}

    print(f"    [recommend window] {start_page} ~ {end_page}")

    candidates, meta = _collect_rows_in_window(
        client, policy, True, start_page, end_page, cache, existing_post_keys,
        effective_policy="recommend_only",
        job_id=job_id,
        shared_client=shared_client,
    )

    summary = {**meta, "selected": len(candidates)}
    print(
        f"    [recommend summary] window={start_page}~{end_page}, "
        f"in_target_rows={meta['in_target_rows']}, "
        f"skipped_existing={meta['skipped_existing']}, "
        f"selected={len(candidates)}"
    )
    return candidates, summary


def build_type2_candidates(client: HttpClient, policy: GalleryPolicy, existing_post_keys: set, cache: PageCache):
    start_page, end_page = find_target_page_window(client, policy, recommend_only=False, cache=cache)
    if start_page is None:
        print("    [type2] 타깃 기간 페이지 없음")
        return [], {
            "window": None,
            "in_target_rows": 0,
            "skipped_existing": 0,
            "passed_rule_rows": 0,
            "covered_months": 0,
            "selected": 0,
        }

    print(f"    [type2 window] {start_page} ~ {end_page}")

    def _type2_filter(row):
        return (
            row["recommend"] >= TYPE2_MIN_RECOMMEND or
            row["comment_count_hint"] >= TYPE2_MIN_COMMENT_HINT
        )

    candidates, meta = _collect_rows_in_window(
        client, policy, False, start_page, end_page, cache, existing_post_keys, filter_fn=_type2_filter
    )

    monthly_map: dict[str, list] = {}
    for row in candidates:
        dt = parse_date_str(row["date_raw"])
        if dt:
            monthly_map.setdefault(dt.strftime("%Y-%m"), []).append(row)

    selected = []
    for ym, rows in sorted(monthly_map.items()):
        quota = min(
            max(TYPE2_MIN_MONTHLY_QUOTA, math.ceil(TYPE2_QUOTA_RATIO * len(rows))),
            TYPE2_MAX_MONTHLY_QUOTA,
        )
        rows_sorted = sorted(
            rows,
            key=lambda x: (x["recommend"], x["comment_count_hint"], safe_int(x["views"], 0), x["post_no"]),
            reverse=True,
        )
        selected.extend(rows_sorted[:quota])

    summary = {
        **meta,
        "passed_rule_rows": len(candidates),
        "covered_months": len(monthly_map),
        "selected": len(selected),
    }
    print(
        f"    [type2 summary] window={start_page}~{end_page}, "
        f"in_target_rows={meta['in_target_rows']}, "
        f"skipped_existing={meta['skipped_existing']}, "
        f"passed_rule_rows={len(candidates)}, "
        f"covered_months={len(monthly_map)}, "
        f"selected={len(selected)}"
    )
    return selected, summary


def should_fallback_type2(summary: dict) -> bool:
    return (
        summary["selected"] < TYPE2_FALLBACK_MIN_SELECTED or
        summary["covered_months"] < TYPE2_FALLBACK_MIN_MONTHS or
        summary["in_target_rows"] < TYPE2_FALLBACK_MIN_TARGET_ROWS
    )


# =========================================================
# 정책 단위 수집
# =========================================================
def update_existing_post_keys(existing_post_keys: set, posts: list[dict]):
    existing_post_keys.update(
        (normalize_gall_id(p["gall_id"]), normalize_post_no(p["post_no"]))
        for p in posts
    )


# =========================================================
# 후보 목록 Supabase 캐시
# =========================================================
STORAGE_BUCKET = "iisecd-dc-candidate-cache"
_STORAGE_FIELDS = ("post_no", "_href", "_e_s_n_o", "gall_id", "gall_type", "comment_count_hint", "recommend", "date_raw")


def _storage_path(job_id: int) -> str:
    return f"candidates_{job_id}.json"


STORAGE_PART_SIZE = 200_000


def _upload_part(job_id: int, part: int, data: bytes, count: int, size_kb: float):
    try:
        import requests as _req
        path = f"candidates_{job_id}_part{part}.json"
        url = f"{os.environ['SUPABASE_URL'].rstrip('/')}/storage/v1/object/{STORAGE_BUCKET}/{path}"
        headers = {
            "apikey":        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
            "Content-Type":  "application/json",
        }
        resp = _req.post(url, headers={**headers, "x-upsert": "true"}, data=data, timeout=120)
        if not resp.ok:
            print(f"  [Storage 저장 실패] part{part}: {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"  [Storage 저장 완료] part{part} | {count}건 | {size_kb:.1f}KB")
    except Exception as e:
        print(f"  [Storage 저장 실패] part{part}: {e}")


def _save_candidates_to_storage(job_id: int, candidates: list) -> bool:
    import json as _json
    slim = [{k: row.get(k, "") for k in _STORAGE_FIELDS} for row in candidates]

    parts = [slim[i:i + STORAGE_PART_SIZE] for i in range(0, max(len(slim), 1), STORAGE_PART_SIZE)]
    for part_idx, part_data in enumerate(parts):
        data = _json.dumps(part_data, ensure_ascii=False).encode("utf-8")
        size_kb = len(data) / 1024
        t = threading.Thread(
            target=_upload_part,
            args=(job_id, part_idx, data, len(part_data), size_kb),
            daemon=True,
        )
        t.start()
    return True


def _load_candidates_from_storage(job_id: int) -> list | None:
    import json as _json
    import requests as _req
    headers = {
        "apikey":        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
    }
    base_url = os.environ["SUPABASE_URL"].rstrip("/")

    all_candidates = []
    part = 0
    while True:
        path = f"candidates_{job_id}_part{part}.json"
        url = f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{path}"
        try:
            resp = _req.get(url, headers=headers, timeout=120)
            if resp.status_code == 404:
                break
            if not resp.ok:
                print(f"  [Storage 로드 실패] part{part}: {resp.status_code}")
                break
            part_data = _json.loads(resp.content)
            all_candidates.extend(part_data)
            print(f"  [Storage 로드] part{part} | {len(part_data)}건")
            part += 1
        except Exception as e:
            print(f"  [Storage 로드 실패] part{part}: {e}")
            break

    if all_candidates:
        print(f"  [Storage 로드 완료] 총 {len(all_candidates)}건 ({part}파트)")
        return all_candidates

    path = f"candidates_{job_id}.json"
    url = f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    try:
        resp = _req.get(url, headers=headers, timeout=120)
        if resp.status_code == 404:
            return None
        if not resp.ok:
            print(f"  [Storage 로드 실패] 구버전: {resp.status_code}")
            return None
        candidates = _json.loads(resp.content)
        print(f"  [Storage 로드 완료] 구버전 | {len(candidates)}건")
        return candidates
    except Exception as e:
        print(f"  [Storage 로드 실패] 구버전: {e}")
        return None


def _delete_candidates_from_storage(job_id: int):
    import requests as _req
    headers = {
        "apikey":        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
    }
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    part = 0
    while True:
        path = f"candidates_{job_id}_part{part}.json"
        url = f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{path}"
        try:
            resp = _req.delete(url, headers=headers, timeout=15)
            if resp.status_code == 404:
                break
            part += 1
        except Exception:
            break


def _save_candidate_cache(
    job_id: int,
    candidates: list,
    effective_policy: str,
    build_done: bool = True,
    last_scanned_page: int = 0,
    start_page: int = 0,
    end_page: int = 0,
    recommend_only: bool = False,
):
    if candidates:
        _save_candidates_to_storage(job_id, candidates)

    payload = {
        "build_done":        build_done,
        "effective_policy":  effective_policy,
        "last_scanned_page": last_scanned_page,
        "start_page":        start_page,
        "end_page":          end_page,
        "recommend_only":    recommend_only,
        "saved_at":          str(datetime.now()),
        "candidate_count":   len(candidates),
        "has_storage":       bool(candidates),
    }
    try:
        import requests as _req
        resp = _req.patch(
            f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/crawl_jobs",
            headers={
                "apikey":        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
                "Content-Type":  "application/json",
            },
            params={"id": f"eq.{job_id}"},
            json={"candidate_cache": payload},
            timeout=30,
        )
        if not resp.ok:
            print(f"  [후보 캐시 저장 실패] {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  [후보 캐시 저장 실패] {e}")


def _load_candidate_cache(job: "CrawlJob", candidates_by_post_no: dict) -> dict | None:
    raw = getattr(job, "candidate_cache", None)
    if raw:
        try:
            if isinstance(raw, str):
                import json as _json
                raw = _json.loads(raw)
            status = "완료" if raw.get("build_done") else f"중단(p={raw.get('last_scanned_page')})"
            print(f"  [후보 캐시 로드] post_nos={len(raw.get('post_nos', []))}건 | {status} | 저장: {raw.get('saved_at')}")
            return raw
        except Exception as e:
            print(f"  [후보 캐시 로드 실패] {e}")

    opposite = "policy_forward" if job.direction == "policy_reverse" else "policy_reverse"
    try:
        import requests as _req
        resp = _req.get(
            f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/crawl_jobs",
            headers={
                "apikey":        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
            },
            params={
                "gall_id":   f"eq.{job.gall_id}",
                "direction": f"eq.{opposite}",
                "select":    "candidate_cache",
                "limit":     "1",
            },
            timeout=15,
        )
        if resp.ok:
            rows = resp.json()
            if rows and rows[0].get("candidate_cache"):
                other = rows[0]["candidate_cache"]
                if isinstance(other, str):
                    import json as _json
                    other = _json.loads(other)
                if other.get("build_done"):
                    print(f"  [후보 캐시 공유] {opposite} → {job.direction} | post_nos={len(other.get('post_nos', []))}건")
                    return other
    except Exception as e:
        print(f"  [반대 direction 캐시 조회 실패] {e}")

    return None


def _clear_candidate_cache(job_id: int):
    _delete_candidates_from_storage(job_id)
    try:
        import requests as _req
        _req.patch(
            f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/crawl_jobs",
            headers={
                "apikey":        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
                "Content-Type":  "application/json",
            },
            params={"id": f"eq.{job_id}"},
            json={"candidate_cache": None},
            timeout=15,
        )
    except Exception:
        pass


def _run_fresh_build(list_client, policy, existing_post_keys, list_cache, effective_policy, job_id, shared_client=None):
    if policy.collection_policy == "general_full":
        candidates, summary = build_general_candidates(list_client, policy, existing_post_keys, list_cache, job_id=job_id, shared_client=shared_client)
    elif policy.collection_policy == "general_monthly_stratified":
        candidates, summary = build_type2_candidates(list_client, policy, existing_post_keys, list_cache)
        if should_fallback_type2(summary):
            candidates, summary = build_recommend_candidates(list_client, policy, existing_post_keys, list_cache, job_id=job_id, shared_client=shared_client)
    elif policy.collection_policy == "recommend_only":
        candidates, summary = build_recommend_candidates(list_client, policy, existing_post_keys, list_cache, job_id=job_id, shared_client=shared_client)
    else:
        return [], {}
    if job_id and candidates:
        _window = summary.get("window") or (0, 0)
        _recommend_only = (effective_policy in ("recommend_only", "recommend_only_fallback"))
        _save_candidate_cache(
            job_id=job_id, candidates=candidates,
            effective_policy=effective_policy, build_done=True,
            start_page=_window[0], end_page=_window[1],
            recommend_only=_recommend_only,
        )
    return candidates, summary


def collect_gallery(
    policy: GalleryPolicy,
    existing_post_keys: set,
    posts_buf: CsvBuffer,
    comments_buf: CsvBuffer,
    shared_client=None,
    job_id: int | None = None,
    job=None,
):
    print(
        f"\n[COLLECT] {policy.gallery_name} ({policy.gall_id}/{policy.gall_type}) "
        f"| policy={policy.collection_policy}"
    )

    list_client = HttpClient()
    list_cache = PageCache()
    effective_policy = policy.collection_policy

    candidates: list = []
    summary: dict = {}
    cached = _load_candidate_cache(job, {}) if (job_id and job is not None) else None

    if cached is not None and cached.get("has_storage"):
        start_page          = cached.get("start_page", 0)
        end_page            = cached.get("end_page", 0)
        recommend_only_flag = cached.get("recommend_only", True)
        build_done_flag     = cached.get("build_done", False)
        last_scanned        = cached.get("last_scanned_page", 0)

        stored = _load_candidates_from_storage(job_id) if job_id else None
        if stored is not None:
            resume_candidates = [
                row for row in stored
                if (normalize_gall_id(row.get("gall_id", policy.gall_id)),
                    normalize_post_no(row["post_no"]))
                not in existing_post_keys
            ]
            for row in resume_candidates:
                row.setdefault("gall_id", policy.gall_id)
                row.setdefault("gall_type", policy.gall_type)

            if build_done_flag:
                candidates = resume_candidates
                summary = {"window": (start_page, end_page), "in_target_rows": len(stored), "skipped_existing": len(stored) - len(candidates)}
                print(f"  [Storage 캐시 사용 - 빌드 완료] 스캔 생략 | 잔여: {len(candidates)}건")
            else:
                print(f"  [Storage 캐시 사용 - 빌드 재개] p={last_scanned+1}~{end_page} | 기수집: {len(resume_candidates)}건")
                candidates, summary = _collect_rows_in_window(
                    list_client, policy, recommend_only_flag,
                    start_page, end_page, list_cache, existing_post_keys,
                    resume_candidates=resume_candidates,
                    resume_from_page=last_scanned + 1,
                    effective_policy=effective_policy,
                    job_id=job_id,
                    shared_client=shared_client,
                )
        else:
            print(f"  [Storage 로드 실패] → 일반 빌드")
            cached = None
            candidates, summary = _run_fresh_build(
                list_client, policy, existing_post_keys, list_cache, effective_policy, job_id, shared_client=shared_client
            )

    elif cached is not None and cached.get("build_done"):
        start_page          = cached.get("start_page", 0)
        end_page            = cached.get("end_page", 0)
        recommend_only_flag = cached.get("recommend_only", True)
        print(f"  [캐시 사용 - 구버전] 페이지 재스캔 p={start_page}~{end_page}")
        if start_page > 0 and end_page > 0:
            candidates, summary = _collect_rows_in_window(
                list_client, policy, recommend_only_flag,
                start_page, end_page, list_cache, existing_post_keys,
                effective_policy=effective_policy,
                job_id=job_id,
                shared_client=shared_client,
            )
        else:
            cached = None
            candidates, summary = _run_fresh_build(
                list_client, policy, existing_post_keys, list_cache, effective_policy, job_id, shared_client=shared_client
            )

    elif cached is not None and not cached.get("build_done"):
        cached_post_nos = set(cached.get("post_nos", []))
        already_collected = {
            k[1] for k in existing_post_keys
            if k[0] == normalize_gall_id(policy.gall_id)
        }
        remaining_nos = cached_post_nos - already_collected
        print(f"  [캐시 사용 - 빌드 재개] p={cached['last_scanned_page']+1}부터 | 기수집 post_no: {len(remaining_nos)}건 보유")
        candidates, summary = _collect_rows_in_window(
            list_client, policy,
            cached["recommend_only"],
            cached["start_page"],
            cached["end_page"],
            list_cache,
            existing_post_keys,
            resume_from_page=cached["last_scanned_page"] + 1,
            effective_policy=effective_policy,
            job_id=job_id,
            shared_client=shared_client,
        )
        existing_nos_for_gall = {
            k[1] for k in existing_post_keys
            if k[0] == normalize_gall_id(policy.gall_id)
        }
        extra_nos = remaining_nos - {row["post_no"] for row in candidates} - existing_nos_for_gall
        if extra_nos:
            print(f"  [중단 전 후보 재스캔] {len(extra_nos)}건 추가 스캔")
            extra_candidates, _ = _collect_rows_in_window(
                list_client, policy,
                cached["recommend_only"],
                cached["start_page"],
                cached["last_scanned_page"],
                list_cache,
                existing_post_keys,
                filter_fn=lambda row: row["post_no"] in extra_nos,
                effective_policy=effective_policy,
                job_id=None,
            )
            candidates = candidates + extra_candidates
        _save_candidate_cache(
            job_id=job_id, candidates=candidates,
            effective_policy=effective_policy, build_done=True,
            start_page=cached["start_page"], end_page=cached["end_page"],
            recommend_only=cached["recommend_only"],
        )

    else:
        if policy.collection_policy not in ("general_full", "general_monthly_stratified", "recommend_only"):
            print("  [SKIP] unsupported policy")
            return 0, 0
        candidates, summary = _run_fresh_build(
            list_client, policy, existing_post_keys, list_cache, effective_policy, job_id, shared_client=shared_client
        )

    print(f"  최종 상세 수집 대상: {len(candidates)} | effective_policy={effective_policy}")

    total_saved_posts = 0
    total_saved_comments = 0
    total_done = 0
    CHUNK_SIZE = 200

    page_posts = []
    page_comments = []

    def _task(meta):
        local_client = HttpClient()
        return fetch_post_detail(local_client, meta, effective_policy)

    for chunk_start in range(0, len(candidates), CHUNK_SIZE):
        chunk = candidates[chunk_start: chunk_start + CHUNK_SIZE]

        with ThreadPoolExecutor(max_workers=ACC.get_workers()) as ex:
            future_map = {ex.submit(_task, meta): meta for meta in chunk}

            for future in as_completed(future_map):
                meta = future_map[future]
                try:
                    post_row, comments = future.result()
                    if post_row:
                        page_posts.append(post_row)
                        page_comments.extend(comments)
                        update_existing_post_keys(existing_post_keys, [post_row])
                    else:
                        ACC.record(0, success=False)
                except Exception as e:
                    ACC.record(0, success=False)
                    print(f"    [DETAIL ERROR] post_no={meta['post_no']} -> {e}")

        total_done += len(chunk)
        print(f"    상세 진행: {total_done}/{len(candidates)} | workers={ACC.get_workers()}")

        total_saved_posts += len(page_posts)
        total_saved_comments += len(page_comments)
        posts_buf.add(page_posts)
        comments_buf.add(page_comments)
        page_posts.clear()
        page_comments.clear()

        if shared_client and job_id:
            try:
                shared_client.heartbeat_job(job_id=job_id, last_page=total_done)
            except Exception as hb_err:
                print(f"    [HEARTBEAT 실패] job_id={job_id} -> {hb_err}")

    # 남은 배치 flush
    print(f"    상세 진행: {total_done}/{len(candidates)} | workers={ACC.get_workers()}")
    total_saved_posts += len(page_posts)
    total_saved_comments += len(page_comments)
    posts_buf.add(page_posts)
    comments_buf.add(page_comments)

    if job_id:
        _clear_candidate_cache(job_id)

    # ← 수정: 누적 카운터로 출력
    print(f"  저장 완료: posts={total_saved_posts}, comments={total_saved_comments}")
    return total_saved_posts, total_saved_comments


# =========================================================
# Supabase job → GalleryPolicy 변환
# =========================================================
def job_to_gallery_policy(job: CrawlJob) -> GalleryPolicy:
    return GalleryPolicy(
        gallery_name=job.gall_name,
        gall_id=job.gall_id,
        gall_type=job.gall_type,
        total_pages=job.page_end,
        collection_policy=job.collection_policy or "",
    )


# =========================================================
# 메인
# =========================================================
def main():
    shared_client = SharedCheckpointClient(claimed_by=WORKER_NAME)

    existing_post_csv_paths = discover_existing_post_csvs(RAW_DIR, POSTS_CSV)
    existing_post_keys = load_existing_post_keys(existing_post_csv_paths, KEY_CACHE_PATH)

    print("=" * 90)
    print(f"[POLICY SHARED] worker={WORKER_NAME}")
    print(f"direction={DIRECTION}")
    print(f"[기존 post key 수] {len(existing_post_keys):,}")
    print(f"[출력 posts]    {POSTS_CSV}")
    print(f"[출력 comments] {COMMENTS_CSV}")
    print("=" * 90)

    posts_buf    = CsvBuffer(POSTS_CSV, POST_COLUMNS, flush_every=500)
    comments_buf = CsvBuffer(COMMENTS_CSV, COMMENT_COLUMNS, flush_every=500)

    total_posts    = 0
    total_comments = 0

    try:
        while True:
            job = shared_client.claim_next_job(direction=DIRECTION, stale_minutes=30)
            if job is None:
                print("[종료] claim 가능한 작업이 더 없음")
                break

            policy = job_to_gallery_policy(job)
            print(f"\n[CLAIM] {policy.gallery_name} ({policy.gall_id}/{policy.gall_type})"
                  f" | collection_policy={policy.collection_policy} | job_id={job.id}")

            try:
                p_cnt, c_cnt = collect_gallery(policy, existing_post_keys, posts_buf, comments_buf, shared_client=shared_client, job_id=job.id, job=job)
                total_posts    += p_cnt
                total_comments += c_cnt

                shared_client.complete_job(job.id)
                print(f"  [완료] job_id={job.id} | posts={p_cnt} | comments={c_cnt}")

                save_existing_post_keys_cache(
                    existing_post_keys,
                    existing_post_csv_paths + [POSTS_CSV],
                    KEY_CACHE_PATH,
                )

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                print(f"  [실패] {policy.gallery_name} -> {error_msg}")
                shared_client.fail_job(job.id, error_msg)
                time.sleep(random.uniform(3, 8))

    finally:
        posts_buf.flush()
        comments_buf.flush()

        save_existing_post_keys_cache(
            existing_post_keys,
            existing_post_csv_paths + [POSTS_CSV],
            KEY_CACHE_PATH,
        )

    print("\n" + "=" * 90)
    print("[전체 종료]")
    print(f"추가 저장 posts={total_posts}, comments={total_comments}")
    print(f"posts csv:    {POSTS_CSV}")
    print(f"comments csv: {COMMENTS_CSV}")
    print("=" * 90)


if __name__ == "__main__":
    main()