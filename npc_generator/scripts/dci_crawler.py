import os
import json
import time
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from shared_checkpoint import (
    SharedCheckpointClient,
    CrawlJob,
    get_page_iter_range,
    get_next_page_after,
    is_job_finished,
)

# =========================================================
# 설정
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "raw")

WORKER_NAME = "hgs_forward_01"
DIRECTION = "forward"

# CSV는 워커별로 분리 권장
POSTS_CSV = os.path.join(OUTPUT_DIR, f"dci_posts_{WORKER_NAME}.csv")
COMMENTS_CSV = os.path.join(OUTPUT_DIR, f"dci_comments_{WORKER_NAME}.csv")

BASE_MIN_DELAY = 2.0
BASE_MAX_DELAY = 4.5
MAX_WORKERS = 2

LONG_REST_EVERY_N_PAGES = 50
LONG_REST_MIN = 10
LONG_REST_MAX = 30

STALE_MINUTES = 20
AUTO_RESET_STALE_ON_START = False

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
        self.base_min = base_min
        self.base_max = base_max
        self.current_min = base_min
        self.current_max = base_max
        self.consecutive_ok = 0
        self._lock = threading.Lock()

    def sleep(self):
        time.sleep(random.uniform(self.current_min, self.current_max))

    def on_success(self):
        with self._lock:
            self.consecutive_ok += 1
            if self.consecutive_ok >= 20:
                new_min = max(self.base_min, self.current_min * 0.85)
                new_max = max(self.base_max, self.current_max * 0.85)
                if new_min < self.current_min or new_max < self.current_max:
                    self.current_min = new_min
                    self.current_max = new_max
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
        self.max_workers = max_workers
        self.workers = max_workers
        self._block_count = 0
        self._streak = 0
        self._times = []
        self._lock = threading.Lock()
        self._wait_schedule = [30, 60, 120, 300]

    def record_response_time(self, elapsed: float):
        with self._lock:
            self._times.append(elapsed)
            if len(self._times) > 30:
                self._times.pop(0)
            self._streak += 1
            avg = sum(self._times) / len(self._times)
            if self._streak >= 50 and self.workers < self.max_workers:
                self.workers += 1
                self._streak = 0
                print(f"    [워커 증가] 평균 응답 {avg:.1f}s → workers={self.workers}")

    def on_blocked(self):
        with self._lock:
            self.workers = 1
            self._streak = 0
            self._block_count += 1
            idx = min(self._block_count - 1, len(self._wait_schedule) - 1)
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
        try:
            _thread_local.session.close()
        except Exception:
            pass
        del _thread_local.session
    get_session()


# =========================================================
# 유틸
# =========================================================
def safe_div(a, b):
    return a / b if b else 0.0


def format_seconds(s):
    if s is None:
        return "N/A"
    s = int(max(0, s))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d > 0:
        return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h > 0:
        return f"{h:02d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"


def print_eta_log(job_start_ts, job: CrawlJob, pages_done: int, current_page: int):
    now = time.time()
    elapsed = now - job_start_ts
    remaining_pages = max(0, job.page_end - current_page)
    eta = (elapsed / pages_done * remaining_pages) if pages_done > 0 else None

    print(f"    [ETA @ {datetime.now().strftime('%H:%M:%S')}]")
    print(f"      shard 경과:         {format_seconds(elapsed)}")
    print(f"      shard 남은 예상:    {format_seconds(eta)}  (남은 페이지 {remaining_pages:,})")
    print(f"      현재 워커 수:       {BLOCK_MGR.get_workers()}")


# =========================================================
# URL 생성
# =========================================================
def make_list_url(gall_id, gall_type, page):
    if gall_type == "MI":
        return f"https://gall.dcinside.com/mini/board/lists/?id={gall_id}&page={page}"
    if gall_type == "G":
        return f"https://gall.dcinside.com/board/lists/?id={gall_id}&page={page}"
    return f"https://gall.dcinside.com/mgallery/board/lists/?id={gall_id}&page={page}"


def make_view_url(gall_id, gall_type, post_no):
    if gall_type == "MI":
        return f"https://gall.dcinside.com/mini/board/view/?id={gall_id}&no={post_no}"
    if gall_type == "G":
        return f"https://gall.dcinside.com/board/view/?id={gall_id}&no={post_no}"
    return f"https://gall.dcinside.com/mgallery/board/view/?id={gall_id}&no={post_no}"


# =========================================================
# 차단 여부 확인
# =========================================================
def is_blocked_response(resp) -> bool:
    if len(resp.text) == 0:
        return True
    if resp.status_code in (429, 403):
        return True
    return False


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
            if attempt < max_retries:
                time.sleep(random.uniform(3, 8))
            continue

        if is_blocked_response(resp):
            print(f"    [차단 감지] 목록 page={page}, attempt={attempt}, len={len(resp.text)}")
            reset_session()
            BLOCK_MGR.on_blocked()
            if attempt >= max_retries:
                return []
            continue

        soup = BeautifulSoup(resp.text, HTML_PARSER)
        rows = soup.select("tr.ub-content")
        if not rows:
            print(f"    [차단 감지] 목록 page={page} 게시글 행 없음, attempt={attempt}")
            reset_session()
            BLOCK_MGR.on_blocked()
            if attempt >= max_retries:
                return []
            continue

        posts = []
        for row in rows:
            num_tag = row.select_one("td.gall_num")
            title_tag = row.select_one("td.gall_tit a:not(.reply_numbox)")
            date_tag = row.select_one("td.gall_date")
            if not (num_tag and title_tag and date_tag):
                continue
            if not num_tag.get_text(strip=True).isdigit():
                continue

            href = title_tag.get("href", "")
            m = re.search(r"no=(\d+)", href)
            if not m:
                continue
            e = re.search(r"e_s_n_o=([a-zA-Z0-9]+)", href)

            d = (date_tag.get("title") or date_tag.get_text(strip=True)).split()[0]
            writer_tag = row.select_one("td.gall_writer")
            count_tag = row.select_one("td.gall_count")
            recom_tag = row.select_one("td.gall_recommend")

            posts.append({
                "gall_id": gall_id,
                "gall_type": gall_type,
                "post_no": m.group(1),
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


# =========================================================
# 본문 파싱
# =========================================================
def normalize_url(url):
    if not url:
        return ""
    url = url.strip()
    return "https:" + url if url.startswith("//") else url


def is_external_url(url):
    if not url:
        return False
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    dc = ["dcinside.com", "gall.dcinside.com", "image.dcinside.com", "nstatic.dcinside.com"]
    return netloc and not any(d in netloc for d in dc)


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

    lnks, seen_l = [], set()
    for t in body_tag.select("a[href]"):
        h = normalize_url(t.get("href", ""))
        if h and is_external_url(h) and h not in seen_l:
            seen_l.add(h)
            lnks.append(h)

    return {
        "body_text": body_text,
        "has_image": bool(imgs),
        "image_count": len(imgs),
        "image_urls_json": json.dumps(imgs, ensure_ascii=False),
        "has_video": bool(vids),
        "video_count": len(vids),
        "video_urls_json": json.dumps(vids, ensure_ascii=False),
        "has_external_link": bool(lnks),
        "external_link_count": len(lnks),
        "external_links_json": json.dumps(lnks, ensure_ascii=False),
    }


# =========================================================
# 댓글
# =========================================================
def get_comments(gall_id, gall_type, post_no, e_s_n_o, gallery_no, session):
    comments, page = [], 1
    cmt_h = {
        **session.headers,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    while True:
        data = {
            "id": gall_id,
            "no": post_no,
            "cmt_id": gall_id,
            "cmt_no": post_no,
            "e_s_n_o": e_s_n_o,
            "comment_page": str(page),
            "_GALLTYPE_": gall_type,
            "cur_cate": "finance",
        }
        if gallery_no:
            data["gallery_no"] = gallery_no

        try:
            resp = session.post(
                "https://gall.dcinside.com/board/comment/",
                data=data,
                headers=cmt_h,
                timeout=15,
            )
            if resp.status_code == 429 or len(resp.text) == 0:
                BLOCK_MGR.on_blocked()
                break
            result = resp.json()
        except Exception as e:
            print(f"      [댓글 실패] post_no={post_no}, page={page} -> {e}")
            break

        raw = result.get("comments", [])
        if not raw:
            break

        for cmt in raw:
            try:
                if cmt.get("del_yn") == "Y" or cmt.get("is_delete") == "1":
                    continue
                memo = cmt.get("memo", "")
                if not memo:
                    continue
                clean = BeautifulSoup(memo, HTML_PARSER).get_text(strip=True)
                if clean:
                    reg = cmt.get("reg_date") or ""
                    comments.append({
                        "gall_id": gall_id,
                        "post_no": post_no,
                        "cmt_no": cmt.get("no", ""),
                        "cmt_writer": cmt.get("name", ""),
                        "cmt_date": reg.split()[0] if reg else "",
                        "cmt_body": clean,
                        "cmt_rcnt": cmt.get("rcnt", "0"),
                    })
            except Exception as e:
                print(f"      [댓글 파싱 실패] {e}")

        total_cnt = result.get("total_cnt") or 0
        if len(comments) >= total_cnt or not raw:
            break

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
            if any(c in str(e) for c in ("429", "403")):
                BLOCK_MGR.on_blocked()
            print(f"      [상세 실패] post_no={post_meta['post_no']}, attempt={attempt} -> {e}")
            if attempt < max_retries:
                reset_session()
                session = get_session()
                time.sleep(random.uniform(3, 8))
            continue

        if is_blocked_response(resp):
            print(f"      [차단 감지] 상세 post_no={post_meta['post_no']}, attempt={attempt}, len={len(resp.text)}")
            reset_session()
            session = get_session()
            BLOCK_MGR.on_blocked()
            if attempt >= max_retries:
                return None, []
            continue

        DELAY.on_success()
        BLOCK_MGR.record_response_time(time.time() - t0)
        BLOCK_MGR.on_success_streak()

        soup = BeautifulSoup(resp.text, HTML_PARSER)
        ai = extract_post_assets(soup)
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
        date_str = dt.get("title", "").split()[0] if dt else post_meta["date"]

        post_row = {
            "gall_id": post_meta["gall_id"],
            "gall_type": post_meta["gall_type"],
            "post_no": post_meta["post_no"],
            "title": post_meta["title"],
            "writer": post_meta["writer"],
            "date": date_str,
            "views": post_meta["views"],
            "recommend": up.get_text(strip=True) if up else "0",
            "unrecommend": down.get_text(strip=True) if down else "0",
            "body": ai["body_text"],
            "has_image": ai["has_image"],
            "image_count": ai["image_count"],
            "image_urls_json": ai["image_urls_json"],
            "has_video": ai["has_video"],
            "video_count": ai["video_count"],
            "video_urls_json": ai["video_urls_json"],
            "has_external_link": ai["has_external_link"],
            "external_link_count": ai["external_link_count"],
            "external_links_json": ai["external_links_json"],
        }

        comments = get_comments(
            post_meta["gall_id"],
            post_meta["gall_type"],
            post_meta["post_no"],
            e_s_n_o,
            gallery_no,
            session,
        )
        return post_row, comments

    return None, []


# =========================================================
# 병렬 수집
# =========================================================
def crawl_page_posts_parallel(post_list):
    page_posts, page_comments = [], []

    with ThreadPoolExecutor(max_workers=BLOCK_MGR.get_workers()) as executor:
        future_map = {executor.submit(get_post_detail, pm): pm for pm in post_list}

        for future in as_completed(future_map):
            pm = future_map[future]
            try:
                post_row, comments = future.result()
                if post_row:
                    page_posts.append(post_row)
                    page_comments.extend(comments)
                    print(
                        f"      [완료] post_no={pm['post_no']} | "
                        f"댓글 {len(comments)}건 | 이미지 {post_row.get('image_count', 0)}개 | "
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


def append_to_csv(rows, filepath, columns):
    if not rows:
        return
    df = pd.DataFrame(rows).reindex(columns=columns)
    with _csv_lock:
        df.to_csv(
            filepath,
            mode="a",
            header=not os.path.exists(filepath),
            index=False,
            encoding="utf-8-sig",
        )


# =========================================================
# shard 처리
# =========================================================
def process_job(shared_client: SharedCheckpointClient, job: CrawlJob):
    print()
    print("=" * 90)
    print(
        f"[CLAIM] {job.gall_name} ({job.gall_id} / {job.gall_type}) | "
        f"shard={job.shard_no} | range={job.page_start}~{job.page_end} | "
        f"next={job.next_page} | direction={job.direction}"
    )
    print("=" * 90)

    job_start_ts = time.time()
    pages_done = posts_done = comments_done = 0

    page_range = list(get_page_iter_range(job))
    if not page_range:
        print("[스킵] 처리할 페이지 없음")
        shared_client.complete_job(job.id)
        return

    prefetch_ex = ThreadPoolExecutor(max_workers=1)
    next_list_future = prefetch_ex.submit(get_post_list, job.gall_id, job.gall_type, page_range[0])

    try:
        for idx, page in enumerate(page_range):
            print(f"\n  [페이지 {page}] 목록 수집 | shard {job.shard_no}")

            post_list = next_list_future.result()
            print(f"    목록 게시글 수: {len(post_list)}")

            if idx + 1 < len(page_range):
                next_page_for_prefetch = page_range[idx + 1]
                next_list_future = prefetch_ex.submit(
                    get_post_list, job.gall_id, job.gall_type, next_page_for_prefetch
                )

            pp, pc = crawl_page_posts_parallel(post_list)

            append_to_csv(pp, POSTS_CSV, POST_COLUMNS)
            append_to_csv(pc, COMMENTS_CSV, COMMENT_COLUMNS)

            pages_done += 1
            posts_done += len(pp)
            comments_done += len(pc)

            next_page = get_next_page_after(job.direction, page)
            shared_client.heartbeat_job(
                job_id=job.id,
                last_page=page,
                next_page=next_page,
            )

            print(f"  [페이지 완료] {page}")
            print(
                f"    이번: posts={len(pp)}, comments={len(pc)}, "
                f"images={sum(x.get('image_count', 0) for x in pp)}"
            )
            print(
                f"    shard 누적: pages={pages_done}, posts={posts_done}, comments={comments_done}"
            )
            print_eta_log(job_start_ts, job, pages_done, page)

            if is_job_finished(job.direction, job.page_start, job.page_end, next_page):
                shared_client.complete_job(job.id)
                print(f"  [SHARD 완료] job_id={job.id}")
                break

            if pages_done % LONG_REST_EVERY_N_PAGES == 0:
                rest = random.uniform(LONG_REST_MIN, LONG_REST_MAX)
                print(f"  [긴 휴식] {rest:.0f}s ...")
                time.sleep(rest)
            else:
                DELAY.sleep()

    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        print(f"[SHARD 실패] job_id={job.id} -> {error_message}")
        shared_client.fail_job(job.id, error_message)
        raise

    finally:
        prefetch_ex.shutdown(wait=False)

    elapsed = time.time() - job_start_ts
    print(f"\n[SHARD 종료] {job.gall_name} / shard={job.shard_no}")
    print(f"  pages={pages_done} | posts={posts_done} | comments={comments_done}")
    print(f"  경과: {format_seconds(elapsed)}")


# =========================================================
# 메인
# =========================================================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    shared_client = SharedCheckpointClient(worker_name=WORKER_NAME)

    if AUTO_RESET_STALE_ON_START:
        reset_cnt = shared_client.reset_stale_jobs(direction=DIRECTION, stale_minutes=STALE_MINUTES)
        print(f"[stale reset] {reset_cnt}")

    print("=" * 90)
    print(f"[FORWARD SHARED] worker={WORKER_NAME}")
    print(f"direction={DIRECTION}")
    print(f"posts_csv={POSTS_CSV}")
    print(f"comments_csv={COMMENTS_CSV}")
    print(f"초기 워커={MAX_WORKERS} | 파서={HTML_PARSER}")
    print("=" * 90)

    while True:
        job = shared_client.claim_next_job(direction=DIRECTION, stale_minutes=STALE_MINUTES)

        if job is None:
            print("[종료] claim 가능한 작업이 더 없음")
            break

        try:
            process_job(shared_client, job)
        except Exception as e:
            print(f"[계속 진행] 다음 shard 로 넘어감 -> {e}")
            time.sleep(random.uniform(5, 10))

    print("\n" + "=" * 90)
    print("[전체 종료]")
    print(f"게시글 CSV: {POSTS_CSV}")
    print(f"댓글 CSV:   {COMMENTS_CSV}")
    print("=" * 90)


if __name__ == "__main__":
    main()