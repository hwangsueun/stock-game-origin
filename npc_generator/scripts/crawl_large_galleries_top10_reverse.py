import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import random
import os
import json
import re
import warnings
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from urllib.parse import urlparse
from bs4 import MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# =========================================================
# 설정
# =========================================================
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT    = os.path.dirname(SCRIPT_DIR)
BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "raw")

FWD_RUN_NAME = "large_top10_2013_2023"
REV_RUN_NAME = "large_top10_2013_2023_reverse"

PAGE_COUNT_CSV       = os.path.join(BASE_OUTPUT_DIR, "dci_gallery_page_counts.csv")
MAIN_CHECKPOINT_FILE = os.path.join(BASE_OUTPUT_DIR, "dci_crawl_checkpoint.json")
FWD_CHECKPOINT_FILE  = os.path.join(BASE_OUTPUT_DIR, f"dci_crawl_checkpoint_{FWD_RUN_NAME}.json")
RANK11_CHECKPOINT_FILE = os.path.join(BASE_OUTPUT_DIR, "dci_crawl_checkpoint_large_rank11_20_2013_2023.json")

# 순방향과 같은 CSV 공유
POSTS_CSV    = os.path.join(BASE_OUTPUT_DIR, f"dci_posts_{FWD_RUN_NAME}.csv")
COMMENTS_CSV = os.path.join(BASE_OUTPUT_DIR, f"dci_comments_{FWD_RUN_NAME}.csv")

# 역방향 전용 체크포인트
CHECKPOINT_FILE       = os.path.join(BASE_OUTPUT_DIR, f"dci_crawl_checkpoint_{REV_RUN_NAME}.json")
TARGET_GALLERIES_JSON = os.path.join(BASE_OUTPUT_DIR, f"dci_target_galleries_{REV_RUN_NAME}.json")

BASE_MIN_DELAY        = 2.0
BASE_MAX_DELAY        = 4.5
TOP_N_REMAINING       = 10
MAX_PAGES_PER_GALLERY = None

START_DATE = datetime.strptime("2013-01-01", "%Y-%m-%d").date()
END_DATE   = datetime.strptime("2023-12-31", "%Y-%m-%d").date()

MAX_WORKERS = 2  # 순방향(3) + 역방향(2) = 총 5

LONG_REST_EVERY_N_PAGES = 50
LONG_REST_MIN = 10
LONG_REST_MAX = 30

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Referer": "https://gall.dcinside.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

POST_COLUMNS = [
    "gall_id", "gall_type", "post_no", "title", "writer", "date",
    "views", "recommend", "unrecommend", "body",
    "has_image", "image_count", "image_urls_json",
    "has_video", "video_count", "video_urls_json",
    "has_external_link", "external_link_count", "external_links_json",
]
COMMENT_COLUMNS = [
    "gall_id", "post_no", "cmt_no", "cmt_writer",
    "cmt_date", "cmt_body", "cmt_rcnt",
]


# =========================================================
# 어댑티브 딜레이
# =========================================================
class AdaptiveDelay:
    def __init__(self, base_min=BASE_MIN_DELAY, base_max=BASE_MAX_DELAY):
        self.base_min = base_min; self.base_max = base_max
        self.current_min = base_min; self.current_max = base_max
        self.consecutive_ok = 0; self._lock = threading.Lock()

    def sleep(self):
        time.sleep(random.uniform(self.current_min, self.current_max))

    def on_success(self):
        with self._lock:
            self.consecutive_ok += 1
            if self.consecutive_ok >= 20:
                new_min = max(self.base_min, self.current_min * 0.85)
                new_max = max(self.base_max, self.current_max * 0.85)
                if new_min < self.current_min or new_max < self.current_max:
                    self.current_min = new_min; self.current_max = new_max
                    print(f"    [딜레이 완화] {self.current_min:.1f}~{self.current_max:.1f}s")
                self.consecutive_ok = 0

    def on_blocked(self):
        with self._lock:
            self.current_min = min(self.current_min * 1.5, 15.0)
            self.current_max = min(self.current_max * 1.5, 25.0)
            self.consecutive_ok = 0
            print(f"    [딜레이 증가] {self.current_min:.1f}~{self.current_max:.1f}s")


DELAY = AdaptiveDelay()


# =========================================================
# 차단 감지 및 재시도 관리
# =========================================================
class BlockManager:
    def __init__(self, max_workers=MAX_WORKERS):
        self.max_workers  = max_workers
        self.workers      = max_workers
        self._block_count = 0
        self._streak      = 0
        self._times: list[float] = []
        self._lock        = threading.Lock()
        self._wait_schedule = [30, 60, 120, 300]

    def record_response_time(self, elapsed: float):
        with self._lock:
            self._times.append(elapsed)
            if len(self._times) > 30: self._times.pop(0)
            self._streak += 1
            avg = sum(self._times) / len(self._times)
            if self._streak >= 50 and self.workers < self.max_workers:
                self.workers += 1; self._streak = 0
                print(f"    [워커 증가] 평균 응답 {avg:.1f}s → workers={self.workers}")

    def on_blocked(self):
        with self._lock:
            self.workers      = 1
            self._streak      = 0
            self._block_count += 1
            idx      = min(self._block_count - 1, len(self._wait_schedule) - 1)
            wait_sec = self._wait_schedule[idx] + random.uniform(0, 10)
            print(f"    [차단 감지 #{self._block_count}] 워커→1, {wait_sec:.0f}s 대기 중...")
            DELAY.on_blocked()
        time.sleep(wait_sec)
        print("    [재시도] 대기 완료")

    def on_success_streak(self):
        with self._lock:
            if self._block_count > 0 and self._streak >= 100:
                self._block_count = 0

    def get_workers(self) -> int:
        return self.workers


BLOCK_MGR = BlockManager()
_cp_lock  = threading.Lock()


# =========================================================
# 스레드 로컬 Session
# =========================================================
_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({**BASE_HEADERS, "User-Agent": random.choice(USER_AGENTS)})
        _thread_local.session = s
    return _thread_local.session


def reset_session():
    if hasattr(_thread_local, "session"):
        try: _thread_local.session.close()
        except: pass
        del _thread_local.session
    get_session()


# =========================================================
# 유틸
# =========================================================
def safe_div(a, b):
    return a / b if b else 0.0


def format_seconds(s):
    if s is None: return "N/A"
    s = int(max(0, s))
    d, r = divmod(s, 86400); h, r = divmod(r, 3600); m, s = divmod(r, 60)
    if d > 0: return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h > 0: return f"{h:02d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"


def parse_post_date(date_str: str):
    if not date_str: return None
    date_str = str(date_str).strip()
    for fmt in ["%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%y.%m.%d", "%y/%m/%d"]:
        try:
            p = datetime.strptime(date_str, fmt).date()
            if p.year <= datetime.now().year: return p
        except ValueError:
            continue
    return None


def print_eta_log(run_start_ts, gallery_start_ts, total_galleries, galleries_done,
                  total_pages_in_range, pages_done, current_page, start_page, end_page):
    now             = time.time()
    run_elapsed     = now - run_start_ts
    gallery_elapsed = now - gallery_start_ts
    remaining_pages = max(0, current_page - start_page)
    gallery_eta     = (gallery_elapsed / pages_done * remaining_pages) if pages_done > 0 else None
    run_remaining   = max(0, total_galleries - galleries_done)
    run_eta         = safe_div(run_elapsed, galleries_done) * run_remaining if galleries_done > 0 else None

    print(f"    [ETA @ {datetime.now().strftime('%H:%M:%S')}] [역방향]")
    print(f"      현재 갤러리 경과:      {format_seconds(gallery_elapsed)}")
    print(f"      현재 갤러리 남은 예상: {format_seconds(gallery_eta)}  (남은 페이지 {remaining_pages:,})")
    print(f"      전체 경과:             {format_seconds(run_elapsed)}")
    print(f"      전체 남은 예상:        {format_seconds(run_eta)}")
    print(f"      현재 워커 수:          {BLOCK_MGR.get_workers()}")


# =========================================================
# URL 생성
# =========================================================
def make_list_url(gall_id, gall_type, page):
    if gall_type == "MI": return f"https://gall.dcinside.com/mini/board/lists/?id={gall_id}&page={page}"
    if gall_type == "G":  return f"https://gall.dcinside.com/board/lists/?id={gall_id}&page={page}"
    return f"https://gall.dcinside.com/mgallery/board/lists/?id={gall_id}&page={page}"


def make_view_url(gall_id, gall_type, post_no):
    if gall_type == "MI": return f"https://gall.dcinside.com/mini/board/view/?id={gall_id}&no={post_no}"
    if gall_type == "G":  return f"https://gall.dcinside.com/board/view/?id={gall_id}&no={post_no}"
    return f"https://gall.dcinside.com/mgallery/board/view/?id={gall_id}&no={post_no}"


# =========================================================
# 차단 여부 확인
# =========================================================
def is_blocked_response(resp) -> bool:
    if len(resp.text) == 0: return True
    if resp.status_code in (429, 403): return True
    return False


# =========================================================
# 체크포인트
# =========================================================
def default_checkpoint():
    return {
        "started_at": str(datetime.now()), "last_updated": None,
        "completed_galleries": [],
        "current_gallery_name": None, "current_gallery_id": None,
        "current_gallery_type": None, "current_gallery_total_pages": 0,
        "current_gallery_start_page": 1, "current_gallery_end_page": 1,
        "last_page_completed": 0, "next_page_to_crawl": 1, "last_post_no": None,
        "stats": {"galleries_completed": 0, "pages_completed": 0,
                  "posts_saved": 0, "comments_saved": 0},
    }


def load_checkpoint(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cp = json.load(f)
        base = default_checkpoint(); base.update(cp)
        if "stats" not in base: base["stats"] = default_checkpoint()["stats"]
        return base
    return default_checkpoint()


def save_checkpoint(cp, path):
    cp["last_updated"] = str(datetime.now())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)


def _sync_completed_to(gall_name: str, target_path: str):
    """역방향 완료 갤러리를 대상 체크포인트의 completed_galleries에도 기록."""
    if not os.path.exists(target_path): return
    with _cp_lock:
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                target_cp = json.load(f)
            completed = set(target_cp.get("completed_galleries", []))
            if gall_name in completed: return
            completed.add(gall_name)
            target_cp["completed_galleries"] = sorted(list(completed))
            target_cp["last_updated"] = str(datetime.now())
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(target_cp, f, ensure_ascii=False, indent=2)
            print(f"    [CP 동기화] {gall_name} → {os.path.basename(target_path)}")
        except Exception as e:
            print(f"    [CP 동기화 실패] {target_path} -> {e}")


def mark_gallery_started(cp, gall_name, gall_id, gall_type, total_pages, start_page, end_page):
    cp.update({
        "current_gallery_name": gall_name, "current_gallery_id": gall_id,
        "current_gallery_type": gall_type, "current_gallery_total_pages": total_pages,
        "current_gallery_start_page": start_page, "current_gallery_end_page": end_page,
        "last_page_completed": end_page + 1,
        "next_page_to_crawl": end_page,
        "last_post_no": None,
    })
    save_checkpoint(cp, CHECKPOINT_FILE)


def mark_page_completed(cp, page, posts_cnt, comments_cnt):
    cp["last_page_completed"] = page
    cp["next_page_to_crawl"]  = page - 1
    cp["last_post_no"]        = None
    cp["stats"]["pages_completed"]  += 1
    cp["stats"]["posts_saved"]      += posts_cnt
    cp["stats"]["comments_saved"]   += comments_cnt
    save_checkpoint(cp, CHECKPOINT_FILE)


def mark_gallery_completed(cp, gall_name):
    # 1) 역방향 자체 체크포인트
    completed = set(cp.get("completed_galleries", []))
    completed.add(gall_name)
    cp["completed_galleries"] = sorted(list(completed))
    cp.update({
        "current_gallery_name": None, "current_gallery_id": None,
        "current_gallery_type": None, "current_gallery_total_pages": 0,
        "current_gallery_start_page": 1, "current_gallery_end_page": 1,
        "last_page_completed": 0, "next_page_to_crawl": 1, "last_post_no": None,
    })
    cp["stats"]["galleries_completed"] = len(completed)
    save_checkpoint(cp, CHECKPOINT_FILE)

    # 2) ★ 순방향 체크포인트들에도 완료 동기화
    _sync_completed_to(gall_name, FWD_CHECKPOINT_FILE)
    _sync_completed_to(gall_name, MAIN_CHECKPOINT_FILE)
    _sync_completed_to(gall_name, RANK11_CHECKPOINT_FILE)


def load_completed_galleries_from_main():
    return set(load_checkpoint(MAIN_CHECKPOINT_FILE).get("completed_galleries", []))


def load_completed_from_fwd():
    return set(load_checkpoint(FWD_CHECKPOINT_FILE).get("completed_galleries", []))


def get_fwd_next_page(gall_name: str):
    fwd_cp = load_checkpoint(FWD_CHECKPOINT_FILE)
    if fwd_cp.get("current_gallery_name") == gall_name:
        return fwd_cp.get("next_page_to_crawl")
    return None


# =========================================================
# 대상 갤러리 선정
# =========================================================
def select_top_remaining_galleries(csv_path, top_n):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV 없음: {csv_path}")
    df = pd.read_csv(csv_path)
    df["total_pages"] = pd.to_numeric(df["total_pages"], errors="coerce").fillna(0).astype(int)
    df = df[df["total_pages"] > 0].copy()
    df = df[~df["gallery_name"].isin(load_completed_galleries_from_main())].copy()
    df = df.sort_values(["total_pages", "gallery_name"], ascending=[False, True]).head(top_n)
    rows = df.to_dict("records")
    with open(TARGET_GALLERIES_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return rows


# =========================================================
# 목록 파싱
# =========================================================
def get_post_list(gall_id, gall_type, page, max_retries=3):
    for attempt in range(1, max_retries + 1):
        session = get_session()
        try:
            resp = session.get(make_list_url(gall_id, gall_type, page), timeout=15)
        except requests.RequestException as e:
            print(f"    [목록 요청 실패] page={page}, attempt={attempt} -> {e}")
            if attempt < max_retries: time.sleep(random.uniform(3, 8))
            continue

        if is_blocked_response(resp):
            print(f"    [차단 감지] 목록 page={page}, attempt={attempt}, len={len(resp.text)}")
            reset_session(); BLOCK_MGR.on_blocked()
            if attempt >= max_retries: return []
            continue

        soup = BeautifulSoup(resp.text, HTML_PARSER)
        rows = soup.select("tr.ub-content")
        if not rows:
            print(f"    [차단 감지] 목록 page={page} 게시글 행 없음, attempt={attempt}")
            reset_session(); BLOCK_MGR.on_blocked()
            if attempt >= max_retries: return []
            continue

        posts = []
        for row in rows:
            num_tag   = row.select_one("td.gall_num")
            title_tag = row.select_one("td.gall_tit a:not(.reply_numbox)")
            date_tag  = row.select_one("td.gall_date")
            if not (num_tag and title_tag and date_tag): continue
            if not num_tag.get_text(strip=True).isdigit(): continue
            href = title_tag.get("href", "")
            m = re.search(r"no=(\d+)", href)
            if not m: continue
            e = re.search(r"e_s_n_o=([a-zA-Z0-9]+)", href)
            d = (date_tag.get("title") or date_tag.get_text(strip=True)).split()[0]
            writer_tag = row.select_one("td.gall_writer")
            count_tag  = row.select_one("td.gall_count")
            recom_tag  = row.select_one("td.gall_recommend")
            posts.append({
                "gall_id": gall_id, "gall_type": gall_type, "post_no": m.group(1),
                "title": title_tag.get("title") or title_tag.get_text(strip=True),
                "writer": writer_tag.get_text(strip=True) if writer_tag else "",
                "date": d,
                "views": count_tag.get_text(strip=True).replace(",", "") if count_tag else "0",
                "recommend": recom_tag.get_text(strip=True) if recom_tag else "0",
                "_e_s_n_o": e.group(1) if e else "",
                "_href": make_view_url(gall_id, gall_type, m.group(1)),
            })
        return posts
    return []


def get_page_date_summary(post_list):
    dates = [d for d in (parse_post_date(p.get("date", "")) for p in post_list) if d]
    if not dates: return {"newest_date": None, "oldest_date": None, "parsed_count": 0}
    return {"newest_date": dates[0], "oldest_date": dates[-1], "parsed_count": len(dates)}


def filter_posts_by_date_range(post_list, start_date, end_date):
    filtered, older, newer, unparsable = [], 0, 0, 0
    for p in post_list:
        d = parse_post_date(p.get("date", ""))
        if d is None: unparsable += 1
        elif d > end_date: newer += 1
        elif d < start_date: older += 1
        else: filtered.append(p)
    parsed = len(post_list) - unparsable
    should_stop = False
    if parsed > 0:
        s = get_page_date_summary(post_list)
        if s["newest_date"] and s["newest_date"] < start_date:
            should_stop = True
    return {"filtered_posts": filtered, "should_stop_gallery": should_stop,
            "older_count": older, "newer_count": newer,
            "unparsable_count": unparsable, "parsed_count": parsed}


# =========================================================
# 이진탐색
# =========================================================
def find_start_page_for_end_date(gall_id, gall_type, total_pages, end_date):
    low, high, answer, step = 1, total_pages, None, 0
    print(f"  [시작 페이지 탐색] END_DATE={end_date}")
    while low <= high:
        step += 1; mid = (low + high) // 2
        pl = get_post_list(gall_id, gall_type, mid)
        s  = get_page_date_summary(pl)
        print(f"    [step {step:>2}] page={mid} | parsed={s['parsed_count']} | newest={s['newest_date']} | oldest={s['oldest_date']}")
        if s["parsed_count"] == 0: low = mid + 1
        elif s["oldest_date"] > end_date: low = mid + 1
        else: answer = mid; high = mid - 1
        time.sleep(random.uniform(0.5, 1.2))
    print(f"  [결과] {f'page={answer}' if answer else '해당 없음'}")
    return answer


def find_end_page_for_start_date(gall_id, gall_type, total_pages, start_date):
    low, high, answer, step = 1, total_pages, None, 0
    print(f"  [끝 페이지 탐색] START_DATE={start_date}")
    while low <= high:
        step += 1; mid = (low + high) // 2
        pl = get_post_list(gall_id, gall_type, mid)
        s  = get_page_date_summary(pl)
        print(f"    [step {step:>2}] page={mid} | parsed={s['parsed_count']} | newest={s['newest_date']} | oldest={s['oldest_date']}")
        if s["parsed_count"] == 0: high = mid - 1
        elif s["newest_date"] >= start_date: answer = mid; low = mid + 1
        else: high = mid - 1
        time.sleep(random.uniform(0.5, 1.2))
    print(f"  [결과] {f'page={answer}' if answer else '해당 없음'}")
    return answer


# =========================================================
# 본문 파싱
# =========================================================
def normalize_url(url):
    if not url: return ""
    url = url.strip()
    return "https:" + url if url.startswith("//") else url


def is_external_url(url):
    if not url: return False
    try: netloc = urlparse(url).netloc.lower()
    except: return False
    dc = ["dcinside.com", "gall.dcinside.com", "image.dcinside.com", "nstatic.dcinside.com"]
    return netloc and not any(d in netloc for d in dc)


def extract_post_assets(soup):
    body_tag = soup.select_one("div.write_div")
    if not body_tag:
        return {"body_text": "",
                "has_image": False, "image_count": 0, "image_urls_json": "[]",
                "has_video": False, "video_count": 0, "video_urls_json": "[]",
                "has_external_link": False, "external_link_count": 0, "external_links_json": "[]"}
    body_text = body_tag.get_text("\n", strip=True)
    imgs, seen_i = [], set()
    for t in body_tag.select("img"):
        s = normalize_url(t.get("src", ""))
        if s and s not in seen_i: seen_i.add(s); imgs.append(s)
    vids, seen_v = [], set()
    for t in body_tag.select("video"):
        s = normalize_url(t.get("src", ""))
        if s and s not in seen_v: seen_v.add(s); vids.append(s)
        for src in t.select("source"):
            s = normalize_url(src.get("src", ""))
            if s and s not in seen_v: seen_v.add(s); vids.append(s)
    for t in body_tag.select("iframe"):
        s = normalize_url(t.get("src", ""))
        if s and s not in seen_v: seen_v.add(s); vids.append(s)
    lnks, seen_l = [], set()
    for t in body_tag.select("a[href]"):
        h = normalize_url(t.get("href", ""))
        if h and is_external_url(h) and h not in seen_l: seen_l.add(h); lnks.append(h)
    return {
        "body_text": body_text,
        "has_image": bool(imgs), "image_count": len(imgs),
        "image_urls_json": json.dumps(imgs, ensure_ascii=False),
        "has_video": bool(vids), "video_count": len(vids),
        "video_urls_json": json.dumps(vids, ensure_ascii=False),
        "has_external_link": bool(lnks), "external_link_count": len(lnks),
        "external_links_json": json.dumps(lnks, ensure_ascii=False),
    }


# =========================================================
# 댓글
# =========================================================
def get_comments(gall_id, gall_type, post_no, e_s_n_o, gallery_no, session):
    comments, page = [], 1
    cmt_h = {**session.headers,
              "X-Requested-With": "XMLHttpRequest",
              "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    while True:
        data = {"id": gall_id, "no": post_no, "cmt_id": gall_id, "cmt_no": post_no,
                "e_s_n_o": e_s_n_o, "comment_page": str(page),
                "_GALLTYPE_": gall_type, "cur_cate": "finance"}
        if gallery_no: data["gallery_no"] = gallery_no
        try:
            resp = session.post("https://gall.dcinside.com/board/comment/",
                                data=data, headers=cmt_h, timeout=15)
            if resp.status_code == 429 or len(resp.text) == 0:
                BLOCK_MGR.on_blocked(); break
            result = resp.json()
        except Exception as e:
            print(f"      [댓글 실패] post_no={post_no}, page={page} -> {e}"); break

        raw = result.get("comments", [])
        if not raw: break
        for cmt in raw:
            try:
                if cmt.get("del_yn") == "Y" or cmt.get("is_delete") == "1": continue
                memo = cmt.get("memo", "")
                if not memo: continue
                clean = BeautifulSoup(memo, HTML_PARSER).get_text(strip=True)
                if clean:
                    reg = cmt.get("reg_date") or ""
                    comments.append({
                        "gall_id": gall_id, "post_no": post_no,
                        "cmt_no": cmt.get("no", ""),
                        "cmt_writer": cmt.get("name", ""),
                        "cmt_date": reg.split()[0] if reg else "",
                        "cmt_body": clean, "cmt_rcnt": cmt.get("rcnt", "0"),
                    })
            except Exception as e:
                print(f"      [댓글 파싱 실패] {e}")

        total_cnt = result.get("total_cnt") or 0
        if len(comments) >= total_cnt or not raw: break
        page += 1
        time.sleep(random.uniform(0.5, 1.0))
    return comments


# =========================================================
# 상세 크롤링
# =========================================================
def get_post_detail(post_meta, max_retries=3):
    session = get_session()
    DELAY.sleep()

    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        try:
            resp = session.get(post_meta["_href"], timeout=15)
        except requests.RequestException as e:
            if any(c in str(e) for c in ("429", "403")): BLOCK_MGR.on_blocked()
            print(f"      [상세 실패] post_no={post_meta['post_no']}, attempt={attempt} -> {e}")
            if attempt < max_retries:
                reset_session(); session = get_session()
                time.sleep(random.uniform(3, 8))
            continue

        if is_blocked_response(resp):
            print(f"      [차단 감지] 상세 post_no={post_meta['post_no']}, attempt={attempt}, len={len(resp.text)}")
            reset_session(); session = get_session()
            BLOCK_MGR.on_blocked()
            if attempt >= max_retries: return None, []
            continue

        DELAY.on_success()
        BLOCK_MGR.record_response_time(time.time() - t0)
        BLOCK_MGR.on_success_streak()

        soup = BeautifulSoup(resp.text, HTML_PARSER)
        ai   = extract_post_assets(soup)
        up   = soup.select_one(".btn_recommend_box .up_num")
        down = soup.select_one(".btn_recommend_box .sup_num")
        et   = soup.select_one("input#e_s_n_o") or soup.select_one("input[name=e_s_n_o]")
        e_s_n_o = et.get("value", "") if et else post_meta.get("_e_s_n_o", "")
        gallery_no = ""
        for inp in soup.select("input[name=gallery_no]"):
            v = inp.get("value", "")
            if v.isdigit() and v != post_meta["post_no"]: gallery_no = v; break
        dt = soup.select_one("span.gall_date")
        date_str = dt.get("title", "").split()[0] if dt else post_meta["date"]

        post_row = {
            "gall_id": post_meta["gall_id"], "gall_type": post_meta["gall_type"],
            "post_no": post_meta["post_no"], "title": post_meta["title"],
            "writer": post_meta["writer"], "date": date_str, "views": post_meta["views"],
            "recommend":   up.get_text(strip=True)   if up   else "0",
            "unrecommend": down.get_text(strip=True) if down else "0",
            "body": ai["body_text"],
            "has_image": ai["has_image"], "image_count": ai["image_count"],
            "image_urls_json": ai["image_urls_json"],
            "has_video": ai["has_video"], "video_count": ai["video_count"],
            "video_urls_json": ai["video_urls_json"],
            "has_external_link": ai["has_external_link"],
            "external_link_count": ai["external_link_count"],
            "external_links_json": ai["external_links_json"],
        }
        comments = get_comments(
            post_meta["gall_id"], post_meta["gall_type"],
            post_meta["post_no"], e_s_n_o, gallery_no, session
        )
        return post_row, comments

    return None, []


# =========================================================
# 병렬 수집
# =========================================================
def crawl_page_posts_parallel(filtered_post_list):
    page_posts, page_comments = [], []
    with ThreadPoolExecutor(max_workers=BLOCK_MGR.get_workers()) as executor:
        future_map = {executor.submit(get_post_detail, pm): pm for pm in filtered_post_list}
        for future in as_completed(future_map):
            pm = future_map[future]
            try:
                post_row, comments = future.result()
                if post_row:
                    page_posts.append(post_row); page_comments.extend(comments)
                    print(
                        f"      [완료] post_no={pm['post_no']} | "
                        f"댓글 {len(comments)}건 | 이미지 {post_row.get('image_count',0)}개 | "
                        f"누적 posts={len(page_posts)}, comments={len(page_comments)}"
                    )
                else:
                    print(f"      [스킵] post_no={pm['post_no']}")
            except Exception as e:
                print(f"      [예외] post_no={pm['post_no']} -> {e}")
    return page_posts, page_comments


# =========================================================
# CSV append
# =========================================================
_csv_lock = threading.Lock()


def append_to_csv(rows, filepath):
    if not rows: return
    df = pd.DataFrame(rows)
    if "posts" in os.path.basename(filepath): df = df.reindex(columns=POST_COLUMNS)
    elif "comments" in os.path.basename(filepath): df = df.reindex(columns=COMMENT_COLUMNS)
    with _csv_lock:
        df.to_csv(filepath, mode="a", header=not os.path.exists(filepath),
                  index=False, encoding="utf-8-sig")


# =========================================================
# 메인
# =========================================================
def crawl_top_remaining_large_galleries_reverse():
    os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
    target_rows  = select_top_remaining_galleries(PAGE_COUNT_CSV, TOP_N_REMAINING)
    cp           = load_checkpoint(CHECKPOINT_FILE)
    my_completed = set(cp.get("completed_galleries", []))
    run_start_ts = time.time()

    print("=" * 100)
    print(f"[{REV_RUN_NAME}] 역방향 크롤러 시작")
    print(f"날짜 범위: {START_DATE} ~ {END_DATE}")
    print(f"초기 워커: {MAX_WORKERS} (동적 조정 활성) | 파서: {HTML_PARSER}")
    print(f"긴 휴식: {LONG_REST_EVERY_N_PAGES}페이지마다 {LONG_REST_MIN}~{LONG_REST_MAX}s")
    print(f"순방향 체크포인트: {FWD_CHECKPOINT_FILE}  ← 완료 동기화 대상")
    print(f"역방향 체크포인트: {CHECKPOINT_FILE}")
    print(f"게시글 CSV: {POSTS_CSV}  ← 순방향과 공유")
    print(f"댓글 CSV:   {COMMENTS_CSV}  ← 순방향과 공유")
    print("=" * 100)
    print("\n[대상 갤러리]")
    for i, r in enumerate(target_rows, 1):
        print(f"{i:>2}. {r['gallery_name']} ({r['gall_id']}) / pages={r['total_pages']} / type={r['gall_type']}")
    print("-" * 100)

    if cp.get("current_gallery_name"):
        print(
            f"[재시작 감지] gallery={cp['current_gallery_name']} | "
            f"last_page={cp['last_page_completed']} | next_page={cp['next_page_to_crawl']} | "
            f"range={cp.get('current_gallery_start_page')}~{cp.get('current_gallery_end_page')}"
        )
        print("-" * 100)

    total_galleries = len(target_rows)

    for idx, row in enumerate(target_rows, 1):
        gall_name   = row["gallery_name"]
        gall_id     = row["gall_id"]
        gall_type   = row["gall_type"]
        total_pages = int(row["total_pages"])
        if MAX_PAGES_PER_GALLERY:
            total_pages = min(total_pages, MAX_PAGES_PER_GALLERY)

        if gall_name in my_completed:
            print(f"[{idx:>2}/{total_galleries}] {gall_name} -> 역방향 완료, 스킵"); continue
        if gall_name in load_completed_galleries_from_main():
            print(f"[{idx:>2}/{total_galleries}] {gall_name} -> 메인 완료, 스킵"); continue
        if gall_name in load_completed_from_fwd():
            print(f"[{idx:>2}/{total_galleries}] {gall_name} -> 순방향 완료, 스킵"); continue

        resume = (cp.get("current_gallery_name") == gall_name)
        print(); print("=" * 100)
        print(f"[{idx:>2}/{total_galleries}] {gall_name} ({gall_id} / {gall_type}) | 총 {total_pages}p")
        print("=" * 100)

        if resume:
            start_page = cp.get("current_gallery_start_page", 1)
            end_page   = cp.get("current_gallery_end_page", total_pages)
            next_page  = cp.get("next_page_to_crawl", end_page)
            print(f"이어하기: {start_page}~{end_page}, page {next_page}부터 (역방향)")
        else:
            start_page = find_start_page_for_end_date(gall_id, gall_type, total_pages, END_DATE)
            end_page   = find_end_page_for_start_date(gall_id, gall_type, total_pages, START_DATE)
            if start_page is None or end_page is None or start_page > end_page:
                print("[스킵] 유효한 날짜 범위 없음")
                mark_gallery_completed(cp, gall_name)
                my_completed = set(cp.get("completed_galleries", [])); continue
            next_page = end_page
            print(f"역방향 시작: page {end_page} → {start_page}")

        if next_page < start_page:
            mark_gallery_completed(cp, gall_name)
            my_completed = set(cp.get("completed_galleries", [])); continue

        mark_gallery_started(cp, gall_name, gall_id, gall_type, total_pages, start_page, end_page)
        if resume:
            cp["next_page_to_crawl"] = next_page; save_checkpoint(cp, CHECKPOINT_FILE)

        gallery_start_ts = time.time()
        g_posts = g_comments = g_pages = 0
        stopped = False

        prefetch_ex      = ThreadPoolExecutor(max_workers=1)
        next_list_future = prefetch_ex.submit(get_post_list, gall_id, gall_type, next_page)

        try:
            for page in range(next_page, start_page - 1, -1):
                print(f"\n  [페이지 {page:>6}/{end_page}→{start_page}] 목록 수집 (역방향)")
                post_list = next_list_future.result()
                print(f"    목록 게시글 수: {len(post_list)}")

                if page > start_page:
                    next_list_future = prefetch_ex.submit(get_post_list, gall_id, gall_type, page - 1)

                fwd_next = get_fwd_next_page(gall_name)
                if fwd_next is not None and page <= fwd_next + 5:
                    print(f"  [충돌 방지] 역방향 page={page} ≤ 순방향 next_page={fwd_next}+5 → 중단")
                    stopped = True; break

                fr = filter_posts_by_date_range(post_list, START_DATE, END_DATE)
                print(
                    f"    날짜 필터 | 수집={len(fr['filtered_posts'])}, "
                    f"미래제외={fr['newer_count']}, 과거제외={fr['older_count']}, "
                    f"파싱실패={fr['unparsable_count']}"
                )

                pp, pc = crawl_page_posts_parallel(fr["filtered_posts"])
                append_to_csv(pp, POSTS_CSV)
                append_to_csv(pc, COMMENTS_CSV)
                mark_page_completed(cp, page, len(pp), len(pc))

                g_posts += len(pp); g_comments += len(pc); g_pages += 1

                print(f"  [페이지 완료] {page} (역방향)")
                print(f"    이번: posts={len(pp)}, comments={len(pc)}, images={sum(x.get('image_count',0) for x in pp)}")
                print(f"    갤러리 누적: pages={g_pages}, posts={g_posts}, comments={g_comments}")
                print(
                    f"    전체 누적: galleries={cp['stats']['galleries_completed']}, "
                    f"pages={cp['stats']['pages_completed']}, "
                    f"posts={cp['stats']['posts_saved']}, comments={cp['stats']['comments_saved']}"
                )
                print_eta_log(run_start_ts, gallery_start_ts, total_galleries,
                              len(my_completed), end_page - start_page + 1,
                              g_pages, page, start_page, end_page)

                if fr["should_stop_gallery"]:
                    stopped = True; print(f"  [종료] {START_DATE} 이전 구간 도달"); break

                if g_pages % LONG_REST_EVERY_N_PAGES == 0:
                    rest = random.uniform(LONG_REST_MIN, LONG_REST_MAX)
                    print(f"  [긴 휴식] {rest:.0f}s ..."); time.sleep(rest)
                else:
                    DELAY.sleep()

        finally:
            prefetch_ex.shutdown(wait=False)

        # ★ 완료 시 역방향 + 모든 순방향 체크포인트 동기화
        mark_gallery_completed(cp, gall_name)
        my_completed = set(cp.get("completed_galleries", []))

        g_elapsed   = time.time() - gallery_start_ts
        run_elapsed = time.time() - run_start_ts
        remaining   = max(0, total_galleries - len(my_completed))
        run_eta     = safe_div(run_elapsed, len(my_completed)) * remaining if my_completed else None

        print(f"\n[갤러리 완료] {gall_name} (역방향)")
        print(f"  pages={g_pages} | posts={g_posts} | comments={g_comments}")
        print(f"  경과: {format_seconds(g_elapsed)} | 조기종료: {stopped}")
        print(f"  전체: {len(my_completed)}/{total_galleries} | 남은 예상: {format_seconds(run_eta)}")

    print("\n" + "=" * 100)
    print(f"[{REV_RUN_NAME}] 역방향 전체 완료")
    print(f"게시글: {POSTS_CSV} | 댓글: {COMMENTS_CSV}")
    print("중복 제거: python dedup_csv.py 실행 필요")
    print("=" * 100)


if __name__ == "__main__":
    crawl_top_remaining_large_galleries_reverse()