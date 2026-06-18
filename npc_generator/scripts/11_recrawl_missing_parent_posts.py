from pathlib import Path
from typing import Dict, List, Optional, Tuple
import time
import random
import re
import json
import sys

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


# =========================================================
# 설정
# =========================================================

class RecrawlConfig:
    def __init__(self):
        script_path = Path(__file__).resolve()
        self.project_root = script_path.parents[1]

        self.processed_dir = self.project_root / "data" / "processed"
        self.raw_dir = self.project_root / "data" / "raw"

        self.posts_ready_path = self.processed_dir / "dci_posts_ready.csv"
        self.comments_ready_path = self.processed_dir / "dci_comments_ready.csv"

        self.output_path = self.raw_dir / "dci_posts_recrawl_missing_parents.csv"
        self.failed_path = self.raw_dir / "dci_posts_recrawl_missing_parents_failed.csv"

        self.target_gall_ids = [
            "stock_new1",
            "dow100",
            "of",
        ]

        self.request_timeout = 10
        self.max_retry = 3

        self.min_sleep = 0.8
        self.max_sleep = 1.8

        # 인터넷 끊김이나 프로세스 종료 대비
        # 너무 크게 잡으면 갑자기 죽었을 때 성공분 일부가 저장 안 될 수 있음
        self.save_every = 10

        # 이미 영구 실패로 판정된 글은 재시도하지 않음
        # 단, 과거 failed 파일에 failure_type이 없으면 재시도함
        self.skip_permanent_failed = True

        # 이 HTTP 상태는 네트워크/차단/서버 문제로 보고 failed 처리하지 않음
        self.transient_status_codes = {403, 408, 429, 500, 502, 503, 504}

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://gall.dcinside.com/",
        }


# =========================================================
# 누락 부모글 대상 추출
# =========================================================

class MissingParentPostExtractor:
    def __init__(self, config: RecrawlConfig):
        self.config = config

    def extract(self) -> pd.DataFrame:
        posts = self._read_csv(self.config.posts_ready_path)
        comments = self._read_csv(self.config.comments_ready_path)

        posts = self._normalize_key_columns(posts)
        comments = self._normalize_key_columns(comments)

        posts_keys = posts[["gall_id", "post_id"]].drop_duplicates().copy()
        posts_keys["has_parent_post"] = True

        merged = comments.merge(
            posts_keys,
            on=["gall_id", "post_id"],
            how="left"
        )

        missing = merged[
            merged["has_parent_post"].isna()
            & merged["gall_id"].isin(self.config.target_gall_ids)
        ].copy()

        targets = (
            missing[["gall_id", "post_id"]]
            .drop_duplicates()
            .sort_values(["gall_id", "post_id"])
            .reset_index(drop=True)
        )

        print("=" * 80)
        print("[MISSING TARGET POSTS]")
        print(targets["gall_id"].value_counts())
        print("total unique missing posts:", len(targets))

        return targets

    def _read_csv(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"필요 파일이 없음: {path}")

        return pd.read_csv(
            path,
            encoding="utf-8-sig",
            dtype=str,
            low_memory=False
        )

    def _normalize_key_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in ["gall_id", "post_id", "comment_id"]:
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str).str.strip()
                df[col] = df[col].str.replace(r"\.0$", "", regex=True)

        return df


# =========================================================
# DCInside 게시글 파서
# =========================================================

class DcinsidePostParser:
    def parse(
        self,
        html: str,
        gall_id: str,
        post_id: str,
        gall_type: str,
        url: str
    ) -> Tuple[Optional[Dict], str]:
        soup = BeautifulSoup(html, "html.parser")
        page_problem = self._detect_page_problem(soup)

        if page_problem:
            return None, page_problem

        title = self._parse_title(soup)
        content = self._parse_content(soup)
        author = self._parse_author(soup)
        date = self._parse_date(soup)
        view_count = self._parse_view_count(soup)
        recommend_count = self._parse_recommend_count(soup)
        dislike_count = self._parse_dislike_count(soup)

        if not title and not content:
            return None, "empty_title_and_content"

        image_urls = self._parse_images(soup)
        video_urls = self._parse_videos(soup)
        external_links = self._parse_external_links(soup)

        return {
            "gall_id": gall_id,
            "gall_type": gall_type,
            "post_id": post_id,
            "title": title,
            "content": content,
            "author": author,
            "date": date,
            "view_count": view_count,
            "recommend_count": recommend_count,
            "unrecommend": dislike_count,
            "has_image": int(len(image_urls) > 0),
            "image_count": len(image_urls),
            "image_urls_json": json.dumps(image_urls, ensure_ascii=False),
            "has_video": int(len(video_urls) > 0),
            "video_count": len(video_urls),
            "video_urls_json": json.dumps(video_urls, ensure_ascii=False),
            "has_external_link": int(len(external_links) > 0),
            "external_link_count": len(external_links),
            "external_links_json": json.dumps(external_links, ensure_ascii=False),
            "url": url,
            "collection_policy": "missing_parent_recrawl",
            "collected_by": "hgs_recrawl_01",
        }, ""

    def _detect_page_problem(self, soup: BeautifulSoup) -> str:
        text = soup.get_text(" ", strip=True)

        permanent_patterns = [
            "존재하지 않는 게시물",
            "삭제된 게시물",
            "게시물이 존재하지 않습니다",
            "해당 게시물은 삭제되었습니다",
            "게시글이 삭제되었거나",
            "해당 게시물은 존재하지 않습니다",
        ]

        transient_patterns = [
            "자동입력 방지",
            "접근 제한",
            "일시적으로 차단",
            "비정상적인 접근",
            "서비스 이용이 제한",
            "잠시 후 다시 시도",
        ]

        for pattern in transient_patterns:
            if pattern in text:
                return "transient_blocked_or_access_limited"

        for pattern in permanent_patterns:
            if pattern in text:
                return "permanent_deleted_or_missing"

        return ""

    def _parse_title(self, soup: BeautifulSoup) -> str:
        candidates = [
            "span.title_subject",
            ".gallview_head .title_subject",
            ".view_head .title_subject",
            "h3.title",
        ]

        for selector in candidates:
            node = soup.select_one(selector)
            if node:
                return self._clean_text(node.get_text(" ", strip=True))

        meta = soup.select_one("meta[property='og:title']")
        if meta and meta.get("content"):
            return self._clean_text(meta.get("content"))

        return ""

    def _parse_content(self, soup: BeautifulSoup) -> str:
        candidates = [
            "div.write_div",
            "#write_div",
            ".view_content_wrap .write_div",
            ".writing_view_box .write_div",
        ]

        for selector in candidates:
            node = soup.select_one(selector)
            if node:
                for bad in node.select("script, style, iframe"):
                    bad.decompose()

                text = node.get_text("\n", strip=True)
                return self._clean_text(text)

        return ""

    def _parse_author(self, soup: BeautifulSoup) -> str:
        candidates = [
            ".gall_writer",
            ".fl .gall_writer",
            ".nickname",
        ]

        for selector in candidates:
            node = soup.select_one(selector)
            if node:
                data_nick = node.get("data-nick")
                if data_nick:
                    return self._clean_text(data_nick)

                text = node.get_text(" ", strip=True)
                if text:
                    return self._clean_text(text)

        return ""

    def _parse_date(self, soup: BeautifulSoup) -> str:
        candidates = [
            ".gall_date",
            ".write_date",
            ".date_time",
        ]

        for selector in candidates:
            node = soup.select_one(selector)
            if node:
                title = node.get("title")
                if title:
                    return self._clean_text(title)

                text = node.get_text(" ", strip=True)
                if text:
                    return self._clean_text(text)

        return ""

    def _parse_view_count(self, soup: BeautifulSoup) -> str:
        text = soup.get_text(" ", strip=True)

        patterns = [
            r"조회\s*([0-9,]+)",
            r"조회수\s*([0-9,]+)",
        ]

        return self._find_first_number(text, patterns)

    def _parse_recommend_count(self, soup: BeautifulSoup) -> str:
        candidates = [
            ".up_num",
            "#recommend_view_up_num",
            ".btn_recommend_box .up_num",
        ]

        for selector in candidates:
            node = soup.select_one(selector)
            if node:
                return self._only_number(node.get_text(" ", strip=True))

        text = soup.get_text(" ", strip=True)
        return self._find_first_number(text, [r"추천\s*([0-9,]+)"])

    def _parse_dislike_count(self, soup: BeautifulSoup) -> str:
        candidates = [
            ".down_num",
            "#recommend_view_down_num",
            ".btn_recommend_box .down_num",
        ]

        for selector in candidates:
            node = soup.select_one(selector)
            if node:
                return self._only_number(node.get_text(" ", strip=True))

        text = soup.get_text(" ", strip=True)
        return self._find_first_number(text, [r"비추천\s*([0-9,]+)", r"비추\s*([0-9,]+)"])

    def _parse_images(self, soup: BeautifulSoup) -> List[str]:
        urls = []

        for img in soup.select("div.write_div img, #write_div img"):
            src = img.get("src") or img.get("data-original") or ""
            src = src.strip()

            if src and src.startswith("http"):
                urls.append(src)

        return list(dict.fromkeys(urls))

    def _parse_videos(self, soup: BeautifulSoup) -> List[str]:
        urls = []

        for node in soup.select("video source, video, iframe"):
            src = node.get("src") or ""
            src = src.strip()

            if src and src.startswith("http"):
                urls.append(src)

        return list(dict.fromkeys(urls))

    def _parse_external_links(self, soup: BeautifulSoup) -> List[str]:
        urls = []

        for a in soup.select("div.write_div a, #write_div a"):
            href = a.get("href") or ""
            href = href.strip()

            if href.startswith("http") and "dcinside.com" not in href:
                urls.append(href)

        return list(dict.fromkeys(urls))

    def _find_first_number(self, text: str, patterns: List[str]) -> str:
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return self._only_number(m.group(1))
        return ""

    def _only_number(self, value: str) -> str:
        value = str(value)
        value = value.replace(",", "")
        m = re.search(r"\d+", value)
        return m.group(0) if m else ""

    def _clean_text(self, text: str) -> str:
        text = str(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


# =========================================================
# 리크롤러
# =========================================================

class DcinsidePostRecrawler:
    def __init__(self, config: RecrawlConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(config.headers)
        self.parser = DcinsidePostParser()

    def recrawl(self, targets: pd.DataFrame) -> None:
        done_keys = self._load_done_keys()
        permanent_failed_keys = self._load_permanent_failed_keys()

        rows = self._load_existing_rows()
        failed_rows = self._load_existing_failed_rows()

        targets = targets.copy()
        targets["key"] = targets["gall_id"].astype(str) + "::" + targets["post_id"].astype(str)

        if self.config.skip_permanent_failed:
            targets = targets[
                ~targets["key"].isin(done_keys)
                & ~targets["key"].isin(permanent_failed_keys)
            ].reset_index(drop=True)
        else:
            targets = targets[
                ~targets["key"].isin(done_keys)
            ].reset_index(drop=True)

        print("=" * 80)
        print("[RECRAWL START]")
        print("already success:", len(done_keys))
        print("permanent failed skipped:", len(permanent_failed_keys) if self.config.skip_permanent_failed else 0)
        print("remaining targets:", len(targets))
        print("output:", self.config.output_path)
        print("failed:", self.config.failed_path)

        try:
            for idx, row in tqdm(targets.iterrows(), total=len(targets)):
                gall_id = str(row["gall_id"]).strip()
                post_id = str(row["post_id"]).strip()
                key = f"{gall_id}::{post_id}"

                fetch_result = self._fetch_one_post(gall_id, post_id)

                if fetch_result["status"] == "success":
                    rows.append(fetch_result["data"])
                    done_keys.add(key)

                    # 과거 failed에 있던 같은 key는 성공 시 제거
                    failed_rows = self._remove_failed_key(failed_rows, key)

                elif fetch_result["status"] == "permanent_failed":
                    failed_rows = self._upsert_failed_row(
                        failed_rows,
                        {
                            "gall_id": gall_id,
                            "post_id": post_id,
                            "reason": fetch_result["reason"],
                            "failure_type": "permanent",
                            "last_url": fetch_result.get("last_url", ""),
                        }
                    )

                elif fetch_result["status"] == "transient_failed":
                    # 인터넷 끊김, 서버 차단, 429, 503 등은 failed 처리하지 않음
                    # 지금까지 성공분만 저장하고 종료
                    self._save_rows(rows)
                    self._save_failed_rows(failed_rows, done_keys)

                    print("\n" + "=" * 80)
                    print("[TRANSIENT ERROR - STOPPED SAFELY]")
                    print("네트워크/차단/서버 문제라서 현재 작업을 중단함.")
                    print("이 글은 failed 처리하지 않았음. 인터넷 연결 후 다시 실행하면 여기서부터 이어감.")
                    print(f"gall_id: {gall_id}")
                    print(f"post_id: {post_id}")
                    print(f"reason : {fetch_result['reason']}")
                    print(f"last_url: {fetch_result.get('last_url', '')}")
                    print("=" * 80)
                    return

                if (idx + 1) % self.config.save_every == 0:
                    self._save_rows(rows)
                    self._save_failed_rows(failed_rows, done_keys)
                    print(f"\n[SAVED] success={len(rows):,}, failed={len(failed_rows):,}")

                time.sleep(random.uniform(self.config.min_sleep, self.config.max_sleep))

        except KeyboardInterrupt:
            self._save_rows(rows)
            self._save_failed_rows(failed_rows, done_keys)
            print("\n" + "=" * 80)
            print("[INTERRUPTED - SAVED]")
            print("Ctrl+C로 중단됨. 현재까지 성공분 저장 완료.")
            print("다시 실행하면 저장된 성공분은 스킵하고 이어서 진행함.")
            print("=" * 80)
            return

        except Exception as e:
            self._save_rows(rows)
            self._save_failed_rows(failed_rows, done_keys)
            print("\n" + "=" * 80)
            print("[ERROR - SAVED]")
            print(f"{type(e).__name__}: {e}")
            print("현재까지 성공분 저장 완료.")
            print("=" * 80)
            raise

        self._save_rows(rows)
        self._save_failed_rows(failed_rows, done_keys)

        print("=" * 80)
        print("[RECRAWL DONE]")
        print("success:", len(rows))
        print("failed :", len(failed_rows))
        print("output :", self.config.output_path)
        print("failed :", self.config.failed_path)

    def _fetch_one_post(self, gall_id: str, post_id: str) -> Dict:
        url_candidates = self._make_url_candidates(gall_id, post_id)

        last_reason = "unknown"
        last_url = ""

        for gall_type, url in url_candidates:
            last_url = url

            for attempt in range(1, self.config.max_retry + 1):
                try:
                    response = self.session.get(
                        url,
                        timeout=self.config.request_timeout
                    )

                    status_code = response.status_code

                    if status_code in self.config.transient_status_codes:
                        last_reason = f"transient_http_{status_code}"
                        self._sleep_after_attempt(attempt)
                        continue

                    if status_code == 404:
                        last_reason = f"permanent_http_404_{gall_type}"
                        break

                    if status_code != 200:
                        last_reason = f"permanent_http_{status_code}_{gall_type}"
                        break

                    html = response.text

                    parsed, parse_reason = self.parser.parse(
                        html=html,
                        gall_id=gall_id,
                        post_id=post_id,
                        gall_type=gall_type,
                        url=url
                    )

                    if parsed is not None:
                        return {
                            "status": "success",
                            "data": parsed,
                            "reason": "",
                            "last_url": url,
                        }

                    if parse_reason.startswith("transient"):
                        return {
                            "status": "transient_failed",
                            "data": None,
                            "reason": parse_reason,
                            "last_url": url,
                        }

                    if parse_reason.startswith("permanent"):
                        last_reason = f"{parse_reason}_{gall_type}"
                        break

                    last_reason = f"parse_failed_{parse_reason}_{gall_type}"
                    break

                except requests.RequestException as e:
                    last_reason = f"transient_request_error_{type(e).__name__}"
                    self._sleep_after_attempt(attempt)
                    continue

                except Exception as e:
                    last_reason = f"permanent_unexpected_error_{type(e).__name__}"
                    break

        if last_reason.startswith("transient"):
            return {
                "status": "transient_failed",
                "data": None,
                "reason": last_reason,
                "last_url": last_url,
            }

        return {
            "status": "permanent_failed",
            "data": None,
            "reason": last_reason,
            "last_url": last_url,
        }

    def _make_url_candidates(self, gall_id: str, post_id: str) -> List[Tuple[str, str]]:
        return [
            (
                "M",
                f"https://gall.dcinside.com/board/view/?id={gall_id}&no={post_id}"
            ),
            (
                "MI",
                f"https://gall.dcinside.com/mgallery/board/view/?id={gall_id}&no={post_id}"
            ),
            (
                "MN",
                f"https://gall.dcinside.com/mini/board/view/?id={gall_id}&no={post_id}"
            ),
        ]

    def _sleep_after_attempt(self, attempt: int) -> None:
        sleep_sec = min(30, 2 ** attempt) + random.uniform(0.0, 1.0)
        time.sleep(sleep_sec)

    def _load_existing_rows(self) -> List[Dict]:
        if not self.config.output_path.exists():
            return []

        df = pd.read_csv(
            self.config.output_path,
            encoding="utf-8-sig",
            dtype=str,
            low_memory=False
        )

        return df.to_dict("records")

    def _load_existing_failed_rows(self) -> List[Dict]:
        if not self.config.failed_path.exists():
            return []

        df = pd.read_csv(
            self.config.failed_path,
            encoding="utf-8-sig",
            dtype=str,
            low_memory=False
        )

        return df.to_dict("records")

    def _load_done_keys(self) -> set:
        if not self.config.output_path.exists():
            return set()

        df = pd.read_csv(
            self.config.output_path,
            encoding="utf-8-sig",
            dtype=str,
            low_memory=False
        )

        if df.empty:
            return set()

        df["key"] = df["gall_id"].astype(str) + "::" + df["post_id"].astype(str)

        return set(df["key"])

    def _load_permanent_failed_keys(self) -> set:
        if not self.config.failed_path.exists():
            return set()

        df = pd.read_csv(
            self.config.failed_path,
            encoding="utf-8-sig",
            dtype=str,
            low_memory=False
        )

        if df.empty:
            return set()

        if "failure_type" not in df.columns:
            return set()

        permanent = df[df["failure_type"].fillna("").astype(str).eq("permanent")].copy()

        if permanent.empty:
            return set()

        permanent["key"] = (
            permanent["gall_id"].astype(str)
            + "::"
            + permanent["post_id"].astype(str)
        )

        return set(permanent["key"])

    def _remove_failed_key(self, failed_rows: List[Dict], key: str) -> List[Dict]:
        result = []

        for row in failed_rows:
            row_key = self._make_key_from_row(row)
            if row_key != key:
                result.append(row)

        return result

    def _upsert_failed_row(self, failed_rows: List[Dict], new_row: Dict) -> List[Dict]:
        key = self._make_key_from_row(new_row)
        filtered = self._remove_failed_key(failed_rows, key)
        filtered.append(new_row)
        return filtered

    def _make_key_from_row(self, row: Dict) -> str:
        gall_id = str(row.get("gall_id", "")).strip()
        post_id = str(row.get("post_id", "")).strip()
        return f"{gall_id}::{post_id}"

    def _save_rows(self, rows: List[Dict]) -> None:
        if not rows:
            return

        df = pd.DataFrame(rows)

        if df.empty:
            return

        df = df.drop_duplicates(subset=["gall_id", "post_id"], keep="last")
        df = df.sort_values(["gall_id", "post_id"]).reset_index(drop=True)

        df.to_csv(
            self.config.output_path,
            index=False,
            encoding="utf-8-sig"
        )

    def _save_failed_rows(self, rows: List[Dict], done_keys: set) -> None:
        if not rows:
            return

        df = pd.DataFrame(rows)

        if df.empty:
            return

        df["key"] = df["gall_id"].astype(str) + "::" + df["post_id"].astype(str)
        df = df[~df["key"].isin(done_keys)].copy()

        if df.empty:
            return

        df = df.drop_duplicates(subset=["gall_id", "post_id"], keep="last")
        df = df.drop(columns=["key"])
        df = df.sort_values(["gall_id", "post_id"]).reset_index(drop=True)

        df.to_csv(
            self.config.failed_path,
            index=False,
            encoding="utf-8-sig"
        )


# =========================================================
# 파이프라인
# =========================================================

class RecrawlPipeline:
    def __init__(self, config: RecrawlConfig):
        self.config = config
        self.extractor = MissingParentPostExtractor(config)
        self.recrawler = DcinsidePostRecrawler(config)

    def run(self):
        targets = self.extractor.extract()
        self.recrawler.recrawl(targets)


def main():
    config = RecrawlConfig()
    pipeline = RecrawlPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()