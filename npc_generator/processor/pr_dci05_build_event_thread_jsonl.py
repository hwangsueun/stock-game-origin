# ============================================================
# pr_dci05_build_event_thread_jsonl.py
#
# 입력:
#   data/processed/dci_event_candidates_final/event_generation_candidates_for_llm_final.csv
#   data/processed/dci_stock_thread_comment_subset/stock_attributed_posts.csv
#   data/processed/dci_stock_thread_comment_subset/dci_comments_stock_thread_only.csv
#
# 출력:
#   data/processed/dci_llm_event_inputs/
#     event_thread_units.jsonl
#     event_thread_units_preview.csv
#     event_thread_units_report.txt
#
# 목적:
#   event 후보 + 실제 디시 게시글/댓글 thread 결합
#   LLM 생성 입력 단위 생성
# ============================================================

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 0. 설정
# ============================================================

@dataclass
class EventThreadConfig:
    project_root: Path
    candidate_path: Path
    posts_path: Path
    comments_path: Path
    output_dir: Path

    encoding: str = "utf-8-sig"

    thread_window_before_days: int = 0
    thread_window_after_days: int = 0

    max_threads_per_event: int = 5
    max_comments_per_thread: int = 40

    max_title_chars: int = 120
    max_body_chars: int = 800
    max_comment_chars: int = 280

    include_unmatched_candidates: bool = True


# ============================================================
# 1. 공통 유틸
# ============================================================

class StockCodeNormalizer:
    @staticmethod
    def normalize(value: object) -> str:
        if pd.isna(value):
            return ""

        s = str(value).strip()

        if re.fullmatch(r"\d+\.0", s):
            s = s[:-2]

        s = s.replace("A", "")
        s = s.replace(".KS", "")
        s = s.replace(".KQ", "")
        s = re.sub(r"\D", "", s)

        if not s:
            return ""

        if len(s) > 6:
            s = s[-6:]

        if len(s) < 6:
            s = s.zfill(6)

        return s


class TextNormalizer:
    @staticmethod
    def normalize_for_match(value: object) -> str:
        if pd.isna(value):
            return ""

        s = str(value).strip().lower()
        s = re.sub(r"\s+", "", s)
        s = re.sub(r"[\[\]\(\)\{\},./\\|:_\-+~!@#$%^&*=`'\";?<>]", "", s)

        return s

    @staticmethod
    def clean_text(value: object, max_chars: int) -> str:
        if pd.isna(value):
            return ""

        s = str(value)
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = s.strip()

        if len(s) > max_chars:
            s = s[:max_chars].rstrip() + "..."

        return s


class ColumnFinder:
    @staticmethod
    def find_first(cols: List[str], candidates: List[str]) -> Optional[str]:
        lower_map = {str(c).lower(): c for c in cols}

        for cand in candidates:
            if cand.lower() in lower_map:
                return lower_map[cand.lower()]

        for col in cols:
            low = str(col).lower()
            for cand in candidates:
                if cand.lower() in low:
                    return col

        return None


class ThreadKeyBuilder:
    @staticmethod
    def add_thread_key(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        if "thread_key" in out.columns:
            out["thread_key"] = out["thread_key"].fillna("").astype(str)
            return out

        if "gall_id" not in out.columns:
            out["gall_id"] = ""

        if "post_id" not in out.columns:
            if "id" in out.columns:
                out["post_id"] = out["id"]
            else:
                out["post_id"] = ""

        out["gall_id"] = out["gall_id"].fillna("").astype(str)
        out["post_id"] = out["post_id"].fillna("").astype(str)

        out["thread_key"] = out["gall_id"] + "__" + out["post_id"]

        return out


# ============================================================
# 2. 후보 로더
# ============================================================

class CandidateLoader:
    def __init__(self, config: EventThreadConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.candidate_path

        if not path.exists():
            raise FileNotFoundError(f"candidate 파일 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)

        if "candidate_id" not in df.columns:
            df = df.reset_index(drop=True)
            df["candidate_id"] = [f"EVT_{i + 1:06d}" for i in range(len(df))]

        if "candidate_date" not in df.columns:
            raise ValueError("candidate_date 컬럼이 필요합니다.")

        if "stock_name" not in df.columns:
            raise ValueError("stock_name 컬럼이 필요합니다.")

        if "stock_code" not in df.columns:
            df["stock_code"] = ""

        df["candidate_date"] = pd.to_datetime(df["candidate_date"], errors="coerce").dt.normalize()
        df["stock_code_norm"] = df["stock_code"].map(StockCodeNormalizer.normalize)
        df["stock_name_norm"] = df["stock_name"].map(TextNormalizer.normalize_for_match)

        numeric_cols = [
            "comment_count",
            "unique_comment_author",
            "board_activity_score",
            "comment_count_ratio_20d",
            "has_price_shock",
            "has_volume_shock",
            "has_market_evidence",
            "has_factual_evidence",
            "market_residual_z_abs",
            "market_return_z_abs",
            "market_volume_ratio",
            "dart_event_count",
            "dart_max_materiality_score",
        ]

        for col in numeric_cols:
            if col not in df.columns:
                df[col] = 0

            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        df = df[df["candidate_date"].notna()].copy()

        print(f"[CandidateLoader] rows: {len(df):,}")
        print(f"[CandidateLoader] date range: {df['candidate_date'].min()} ~ {df['candidate_date'].max()}")

        return df


# ============================================================
# 3. 게시글 로더
# ============================================================

class PostLoader:
    DATE_CANDIDATES = [
        "created_at",
        "write_at",
        "written_at",
        "datetime",
        "date_time",
        "post_datetime",
        "post_date",
        "created_date",
        "date",
        "regdate",
        "write_date",
        "time",
    ]

    TITLE_CANDIDATES = [
        "clean_title",
        "title_clean",
        "title",
        "subject",
        "post_title",
    ]

    BODY_CANDIDATES = [
        "clean_body",
        "body_clean",
        "clean_content",
        "content_clean",
        "body",
        "content",
        "text",
        "post_body",
        "post_content",
    ]

    AUTHOR_CANDIDATES = [
        "author_hash",
        "user_hash",
        "nickname_hash",
        "writer_hash",
        "author",
        "nickname",
        "writer",
        "user_id",
    ]

    STOCK_NAME_CANDIDATES = [
        "stock_name",
        "matched_stock_name",
        "matched_stock_names",
        "stock_names",
        "matched_stocks",
        "matched_stock_list",
        "attributed_stock_name",
        "target_stock_name",
    ]

    STOCK_CODE_CANDIDATES = [
        "stock_code",
        "matched_stock_code",
        "matched_stock_codes",
        "stock_codes",
        "attributed_stock_code",
        "target_stock_code",
    ]

    def __init__(self, config: EventThreadConfig):
        self.config = config

    def load(self) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
        path = self.config.posts_path

        if not path.exists():
            raise FileNotFoundError(f"posts 파일 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)
        df = ThreadKeyBuilder.add_thread_key(df)

        cols = list(df.columns)

        date_col = ColumnFinder.find_first(cols, self.DATE_CANDIDATES)
        title_col = ColumnFinder.find_first(cols, self.TITLE_CANDIDATES)
        body_col = ColumnFinder.find_first(cols, self.BODY_CANDIDATES)
        author_col = ColumnFinder.find_first(cols, self.AUTHOR_CANDIDATES)
        stock_name_col = ColumnFinder.find_first(cols, self.STOCK_NAME_CANDIDATES)
        stock_code_col = ColumnFinder.find_first(cols, self.STOCK_CODE_CANDIDATES)

        if date_col is None:
            raise ValueError(f"posts 날짜 컬럼을 찾지 못했습니다. columns={cols}")

        if title_col is None:
            df["__title__"] = ""
            title_col = "__title__"

        if body_col is None:
            df["__body__"] = ""
            body_col = "__body__"

        if author_col is None:
            df["__author__"] = ""
            author_col = "__author__"

        if stock_name_col is None:
            df["__stock_name_attr__"] = ""
            stock_name_col = "__stock_name_attr__"

        if stock_code_col is None:
            df["__stock_code_attr__"] = ""
            stock_code_col = "__stock_code_attr__"

        df["post_datetime"] = pd.to_datetime(df[date_col], errors="coerce")
        df["post_date"] = df["post_datetime"].dt.normalize()

        df["title_text"] = df[title_col].map(lambda x: TextNormalizer.clean_text(x, self.config.max_title_chars))
        df["body_text"] = df[body_col].map(lambda x: TextNormalizer.clean_text(x, self.config.max_body_chars))
        df["author_text"] = df[author_col].fillna("").astype(str)

        df["stock_name_attr"] = df[stock_name_col].fillna("").astype(str)
        df["stock_name_attr_norm"] = df["stock_name_attr"].map(TextNormalizer.normalize_for_match)

        df["stock_code_attr"] = df[stock_code_col].fillna("").astype(str)
        df["stock_code_attr_norm"] = df["stock_code_attr"].map(StockCodeNormalizer.normalize)

        df["title_body_norm"] = (
            df["title_text"].fillna("").astype(str)
            + " "
            + df["body_text"].fillna("").astype(str)
        ).map(TextNormalizer.normalize_for_match)

        df = df[
            df["post_date"].notna()
            & df["thread_key"].astype(str).str.len().gt(2)
        ].copy()

        meta = {
            "date_col": date_col,
            "title_col": title_col,
            "body_col": body_col,
            "author_col": author_col,
            "stock_name_col": stock_name_col,
            "stock_code_col": stock_code_col,
        }

        print(f"[PostLoader] rows: {len(df):,}")
        print(f"[PostLoader] date range: {df['post_date'].min()} ~ {df['post_date'].max()}")
        print(f"[PostLoader] columns used: {meta}")

        return df, meta


# ============================================================
# 4. 댓글 로더
# ============================================================

class CommentLoader:
    DATE_CANDIDATES = [
        "created_at",
        "write_at",
        "written_at",
        "datetime",
        "date_time",
        "comment_datetime",
        "comment_date",
        "created_date",
        "date",
        "regdate",
        "write_date",
        "time",
    ]

    TEXT_CANDIDATES = [
        "clean_text",
        "text_clean",
        "clean_comment",
        "comment_clean",
        "comment_text",
        "comment",
        "content",
        "body",
        "text",
        "reply",
        "memo",
    ]

    AUTHOR_CANDIDATES = [
        "author_hash",
        "user_hash",
        "nickname_hash",
        "writer_hash",
        "author",
        "nickname",
        "writer",
        "user_id",
    ]

    COMMENT_ID_CANDIDATES = [
        "comment_id",
        "reply_id",
        "id",
        "comment_no",
    ]

    def __init__(self, config: EventThreadConfig):
        self.config = config

    def load(self) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
        path = self.config.comments_path

        if not path.exists():
            raise FileNotFoundError(f"comments 파일 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)
        df = ThreadKeyBuilder.add_thread_key(df)

        cols = list(df.columns)

        date_col = ColumnFinder.find_first(cols, self.DATE_CANDIDATES)
        text_col = ColumnFinder.find_first(cols, self.TEXT_CANDIDATES)
        author_col = ColumnFinder.find_first(cols, self.AUTHOR_CANDIDATES)
        comment_id_col = ColumnFinder.find_first(cols, self.COMMENT_ID_CANDIDATES)

        if text_col is None:
            raise ValueError(f"comments 텍스트 컬럼을 찾지 못했습니다. columns={cols}")

        if date_col is None:
            df["__comment_datetime__"] = pd.NaT
            date_col = "__comment_datetime__"

        if author_col is None:
            df["__comment_author__"] = ""
            author_col = "__comment_author__"

        if comment_id_col is None:
            df["__comment_id__"] = [f"CMT_{i + 1}" for i in range(len(df))]
            comment_id_col = "__comment_id__"

        df["comment_datetime"] = pd.to_datetime(df[date_col], errors="coerce")
        df["comment_text_out"] = df[text_col].map(
            lambda x: TextNormalizer.clean_text(x, self.config.max_comment_chars)
        )
        df["comment_author_text"] = df[author_col].fillna("").astype(str)
        df["comment_id_out"] = df[comment_id_col].fillna("").astype(str)

        df = df[
            df["thread_key"].astype(str).str.len().gt(2)
            & df["comment_text_out"].astype(str).str.len().gt(0)
        ].copy()

        df = df.sort_values(["thread_key", "comment_datetime"], na_position="last")

        meta = {
            "date_col": date_col,
            "text_col": text_col,
            "author_col": author_col,
            "comment_id_col": comment_id_col,
        }

        print(f"[CommentLoader] rows: {len(df):,}")
        print(f"[CommentLoader] columns used: {meta}")

        return df, meta


# ============================================================
# 5. 댓글 그룹 생성
# ============================================================

class CommentGrouper:
    def __init__(self, config: EventThreadConfig):
        self.config = config

    def build(self, comments: pd.DataFrame) -> Dict[str, Dict]:
        grouped = {}

        for thread_key, g in comments.groupby("thread_key", dropna=False):
            g = g.copy()

            full_comment_count = len(g)
            unique_authors = g["comment_author_text"].replace("", np.nan).nunique(dropna=True)

            limited = g.head(self.config.max_comments_per_thread)

            comment_items = []

            for _, row in limited.iterrows():
                dt = row.get("comment_datetime")

                if pd.isna(dt):
                    dt_str = ""
                else:
                    dt_str = pd.to_datetime(dt).strftime("%Y-%m-%d %H:%M:%S")

                comment_items.append({
                    "comment_id": str(row.get("comment_id_out", "")),
                    "comment_datetime": dt_str,
                    "author": str(row.get("comment_author_text", "")),
                    "text": str(row.get("comment_text_out", "")),
                })

            grouped[str(thread_key)] = {
                "full_comment_count": int(full_comment_count),
                "unique_comment_authors": int(unique_authors),
                "used_comment_count": int(len(comment_items)),
                "comments": comment_items,
            }

        print(f"[CommentGrouper] grouped threads: {len(grouped):,}")

        return grouped


# ============================================================
# 6. 후보별 thread 매칭
# ============================================================

class ThreadMatcher:
    def __init__(self, config: EventThreadConfig, comment_groups: Dict[str, Dict]):
        self.config = config
        self.comment_groups = comment_groups

    def match_for_candidate(self, candidate: pd.Series, posts: pd.DataFrame) -> pd.DataFrame:
        candidate_date = pd.to_datetime(candidate["candidate_date"], errors="coerce")

        if pd.isna(candidate_date):
            return pd.DataFrame()

        start_date = candidate_date - pd.Timedelta(days=self.config.thread_window_before_days)
        end_date = candidate_date + pd.Timedelta(days=self.config.thread_window_after_days)

        stock_code = StockCodeNormalizer.normalize(candidate.get("stock_code", ""))
        stock_name_norm = TextNormalizer.normalize_for_match(candidate.get("stock_name", ""))

        pool = posts[
            (posts["post_date"] >= start_date)
            & (posts["post_date"] <= end_date)
        ].copy()

        if pool.empty:
            return pool

        pool["stock_match_score"] = 0

        # 코드 매칭
        if stock_code:
            code_match = pool["stock_code_attr_norm"].astype(str).str.contains(
                re.escape(stock_code),
                na=False,
            )
            pool.loc[code_match, "stock_match_score"] = np.maximum(
                pool.loc[code_match, "stock_match_score"],
                100,
            )

        # 종목명 attribution 컬럼 매칭
        if stock_name_norm:
            name_match = pool["stock_name_attr_norm"].astype(str).str.contains(
                re.escape(stock_name_norm),
                na=False,
            )
            pool.loc[name_match, "stock_match_score"] = np.maximum(
                pool.loc[name_match, "stock_match_score"],
                90,
            )

        # attribution 컬럼이 없을 때만 제목/본문 fallback
        # DL, LG처럼 너무 짧은 이름은 fallback 금지
        if stock_name_norm and len(stock_name_norm) >= 3:
            text_match = pool["title_body_norm"].astype(str).str.contains(
                re.escape(stock_name_norm),
                na=False,
            )
            pool.loc[text_match, "stock_match_score"] = np.maximum(
                pool.loc[text_match, "stock_match_score"],
                40,
            )

        pool = pool[pool["stock_match_score"] > 0].copy()

        if pool.empty:
            return pool

        pool["thread_comment_count"] = pool["thread_key"].map(
            lambda k: self.comment_groups.get(str(k), {}).get("full_comment_count", 0)
        )

        pool["thread_unique_comment_authors"] = pool["thread_key"].map(
            lambda k: self.comment_groups.get(str(k), {}).get("unique_comment_authors", 0)
        )

        pool["post_body_len"] = pool["body_text"].fillna("").astype(str).str.len()
        pool["post_title_len"] = pool["title_text"].fillna("").astype(str).str.len()

        pool["rank_score"] = (
            pool["stock_match_score"]
            + np.log1p(pool["thread_comment_count"]) * 10
            + np.log1p(pool["thread_unique_comment_authors"]) * 5
            + np.minimum(pool["post_body_len"], 500) / 100
            + np.minimum(pool["post_title_len"], 100) / 100
        )

        pool = pool.sort_values(
            ["rank_score", "thread_comment_count", "thread_unique_comment_authors"],
            ascending=[False, False, False],
        )

        return pool.head(self.config.max_threads_per_event)


# ============================================================
# 7. JSONL 빌더
# ============================================================

class EventThreadUnitBuilder:
    def __init__(
        self,
        config: EventThreadConfig,
        posts: pd.DataFrame,
        comment_groups: Dict[str, Dict],
    ):
        self.config = config
        self.posts = posts
        self.comment_groups = comment_groups
        self.matcher = ThreadMatcher(config, comment_groups)

    def build_units(self, candidates: pd.DataFrame) -> Tuple[List[Dict], pd.DataFrame]:
        units = []
        preview_rows = []

        for _, candidate in candidates.iterrows():
            matched_posts = self.matcher.match_for_candidate(candidate, self.posts)

            if matched_posts.empty and not self.config.include_unmatched_candidates:
                continue

            threads = self._build_thread_items(matched_posts)

            unit = self._build_unit(candidate, threads)
            units.append(unit)

            preview_rows.append(self._build_preview_row(candidate, threads))

        preview = pd.DataFrame(preview_rows)

        return units, preview

    def _build_thread_items(self, matched_posts: pd.DataFrame) -> List[Dict]:
        thread_items = []

        for _, row in matched_posts.iterrows():
            thread_key = str(row.get("thread_key", ""))

            comments_info = self.comment_groups.get(thread_key, {
                "full_comment_count": 0,
                "unique_comment_authors": 0,
                "used_comment_count": 0,
                "comments": [],
            })

            post_dt = row.get("post_datetime")

            if pd.isna(post_dt):
                post_dt_str = ""
            else:
                post_dt_str = pd.to_datetime(post_dt).strftime("%Y-%m-%d %H:%M:%S")

            thread_items.append({
                "thread_key": thread_key,
                "gall_id": str(row.get("gall_id", "")),
                "post_id": str(row.get("post_id", "")),
                "post_datetime": post_dt_str,
                "author": str(row.get("author_text", "")),
                "title": str(row.get("title_text", "")),
                "body": str(row.get("body_text", "")),
                "stock_match_score": float(row.get("stock_match_score", 0)),
                "rank_score": float(row.get("rank_score", 0)),
                "full_comment_count": int(comments_info.get("full_comment_count", 0)),
                "unique_comment_authors": int(comments_info.get("unique_comment_authors", 0)),
                "used_comment_count": int(comments_info.get("used_comment_count", 0)),
                "comments": comments_info.get("comments", []),
            })

        return thread_items

    def _build_unit(self, candidate: pd.Series, threads: List[Dict]) -> Dict:
        candidate_date = pd.to_datetime(candidate.get("candidate_date"), errors="coerce")

        if pd.isna(candidate_date):
            candidate_date_str = ""
        else:
            candidate_date_str = candidate_date.strftime("%Y-%m-%d")

        return {
            "candidate_id": str(candidate.get("candidate_id", "")),
            "candidate_date": candidate_date_str,
            "market_date": str(candidate.get("market_date", "")),
            "stock": {
                "stock_code": str(candidate.get("stock_code", "")),
                "stock_name": str(candidate.get("stock_name", "")),
            },
            "event_generation_candidate_type": str(candidate.get("event_generation_candidate_type", "")),
            "generation_permissions": {
                "fact_claim_allowed": int(float(candidate.get("fact_claim_allowed", 0))),
                "market_reaction_allowed": int(float(candidate.get("market_reaction_allowed", 0))),
                "rumor_expression_allowed": int(float(candidate.get("rumor_expression_allowed", 0))),
                "community_reaction_allowed": int(float(candidate.get("community_reaction_allowed", 0))),
            },
            "guardrail": str(candidate.get("llm_guardrail", "")),
            "evidence": {
                "board": {
                    "board_burst_level": str(candidate.get("board_burst_level", "")),
                    "board_signal_quality": str(candidate.get("board_signal_quality", "")),
                    "comment_count": float(candidate.get("comment_count", 0)),
                    "unique_comment_author": float(candidate.get("unique_comment_author", 0)),
                    "comment_count_ratio_20d": float(candidate.get("comment_count_ratio_20d", 0)),
                    "board_activity_score": float(candidate.get("board_activity_score", 0)),
                },
                "market": {
                    "has_market_evidence": int(float(candidate.get("has_market_evidence", 0))),
                    "has_price_shock": int(float(candidate.get("has_price_shock", 0))),
                    "has_volume_shock": int(float(candidate.get("has_volume_shock", 0))),
                    "market_residual_z_abs": float(candidate.get("market_residual_z_abs", 0)),
                    "market_return_z_abs": float(candidate.get("market_return_z_abs", 0)),
                    "market_volume_ratio": float(candidate.get("market_volume_ratio", 0)),
                    "market_return_pct": float(candidate.get("market_return_pct", 0)),
                },
                "dart": {
                    "has_factual_evidence": int(float(candidate.get("has_factual_evidence", 0))),
                    "dart_event_count": float(candidate.get("dart_event_count", 0)),
                    "dart_max_materiality_score": float(candidate.get("dart_max_materiality_score", 0)),
                    "dart_dates": str(candidate.get("dart_dates", "")),
                    "dart_event_groups": str(candidate.get("dart_event_groups", "")),
                    "dart_report_names": str(candidate.get("dart_report_names", "")),
                    "dart_rcept_nos": str(candidate.get("dart_rcept_nos", "")),
                    "factual_basis_text": str(candidate.get("factual_basis_text", "")),
                },
            },
            "source_threads": threads,
            "source_thread_count": len(threads),
            "total_used_comments": int(sum(t.get("used_comment_count", 0) for t in threads)),
            "generation_instruction": self._build_generation_instruction(candidate, threads),
        }

    @staticmethod
    def _build_generation_instruction(candidate: pd.Series, threads: List[Dict]) -> str:
        event_type = str(candidate.get("event_generation_candidate_type", ""))

        if event_type == "factual_news_needed":
            return (
                "DART 공시명과 공시일 범위 안에서만 개별 종목 사실형 뉴스를 작성한다. "
                "게시글/댓글은 시장 반응과 어조 참고용으로만 사용한다. "
                "공시에 없는 계약상대, 금액, 실적 수치, 원인을 만들지 않는다."
            )

        if event_type == "market_reaction_news":
            return (
                "가격/거래량 shock과 커뮤니티 반응을 바탕으로 시장 반응형 뉴스를 작성한다. "
                "구체적 사건 원인이나 내부정보를 단정하지 않는다."
            )

        if event_type == "rumor_or_speculation":
            return (
                "사실형 뉴스가 아니라 커뮤니티의 추측, 기대, 불안, 소문 분위기만 작성한다. "
                "공식 확인 표현을 쓰지 않는다."
            )

        if event_type == "community_reaction_only":
            return (
                "뉴스 문장을 만들지 않는다. 종토방 반응, 농담, 불안, 기대 같은 분위기 문장만 만든다."
            )

        return "생성 제외."

    @staticmethod
    def _build_preview_row(candidate: pd.Series, threads: List[Dict]) -> Dict:
        top_title = ""
        top_comment_count = 0

        if threads:
            top_title = threads[0].get("title", "")
            top_comment_count = threads[0].get("full_comment_count", 0)

        return {
            "candidate_id": candidate.get("candidate_id", ""),
            "candidate_date": candidate.get("candidate_date", ""),
            "stock_code": candidate.get("stock_code", ""),
            "stock_name": candidate.get("stock_name", ""),
            "event_generation_candidate_type": candidate.get("event_generation_candidate_type", ""),
            "source_thread_count": len(threads),
            "total_used_comments": int(sum(t.get("used_comment_count", 0) for t in threads)),
            "top_thread_title": top_title,
            "top_thread_comment_count": top_comment_count,
            "has_factual_evidence": candidate.get("has_factual_evidence", 0),
            "has_market_evidence": candidate.get("has_market_evidence", 0),
            "dart_report_names": candidate.get("dart_report_names", ""),
            "llm_guardrail": candidate.get("llm_guardrail", ""),
        }


# ============================================================
# 8. 저장
# ============================================================

class EventThreadWriter:
    def __init__(self, config: EventThreadConfig):
        self.config = config

    def write(self, units: List[Dict], preview: pd.DataFrame) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = self.config.output_dir / "event_thread_units.jsonl"
        preview_path = self.config.output_dir / "event_thread_units_preview.csv"
        report_path = self.config.output_dir / "event_thread_units_report.txt"

        with open(jsonl_path, "w", encoding="utf-8") as f:
            for unit in units:
                f.write(json.dumps(unit, ensure_ascii=False) + "\n")

        preview_out = preview.copy()

        if "candidate_date" in preview_out.columns:
            preview_out["candidate_date"] = pd.to_datetime(
                preview_out["candidate_date"],
                errors="coerce",
            ).dt.strftime("%Y-%m-%d")

        preview_out.to_csv(preview_path, index=False, encoding=self.config.encoding)

        self._write_report(units, preview_out, report_path)

        print(f"[SAVE] jsonl: {jsonl_path}")
        print(f"[SAVE] preview: {preview_path}")
        print(f"[SAVE] report: {report_path}")

    def _write_report(self, units: List[Dict], preview: pd.DataFrame, report_path: Path) -> None:
        lines = []

        lines.append("# Event Thread Units Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- candidate_path: {self.config.candidate_path}")
        lines.append(f"- posts_path: {self.config.posts_path}")
        lines.append(f"- comments_path: {self.config.comments_path}")
        lines.append("")
        lines.append("## Config")
        lines.append(f"- thread_window_before_days: {self.config.thread_window_before_days}")
        lines.append(f"- thread_window_after_days: {self.config.thread_window_after_days}")
        lines.append(f"- max_threads_per_event: {self.config.max_threads_per_event}")
        lines.append(f"- max_comments_per_thread: {self.config.max_comments_per_thread}")
        lines.append("")
        lines.append("## Counts")
        lines.append(f"- total_units: {len(units):,}")

        matched = sum(1 for u in units if u.get("source_thread_count", 0) > 0)
        unmatched = len(units) - matched

        lines.append(f"- units_with_threads: {matched:,}")
        lines.append(f"- units_without_threads: {unmatched:,}")
        lines.append(f"- total_threads_used: {sum(u.get('source_thread_count', 0) for u in units):,}")
        lines.append(f"- total_comments_used: {sum(u.get('total_used_comments', 0) for u in units):,}")
        lines.append("")

        if not preview.empty:
            lines.append("## By event_generation_candidate_type")
            for k, v in preview["event_generation_candidate_type"].value_counts(dropna=False).items():
                lines.append(f"- {k}: {int(v):,}")

            lines.append("")
            lines.append("## Thread match by type")
            temp = preview.copy()
            temp["has_thread"] = (temp["source_thread_count"].astype(float) > 0).astype(int)

            cross = (
                temp.groupby(["event_generation_candidate_type", "has_thread"], dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values(["event_generation_candidate_type", "has_thread"])
            )

            for _, row in cross.iterrows():
                lines.append(
                    f"- {row['event_generation_candidate_type']} / has_thread={int(row['has_thread'])}: {int(row['count']):,}"
                )

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))


# ============================================================
# 9. 파이프라인
# ============================================================

class EventThreadPipeline:
    def __init__(self, config: EventThreadConfig):
        self.config = config

    def run(self) -> None:
        candidates = CandidateLoader(self.config).load()
        posts, post_meta = PostLoader(self.config).load()
        comments, comment_meta = CommentLoader(self.config).load()

        comment_groups = CommentGrouper(self.config).build(comments)

        units, preview = EventThreadUnitBuilder(
            config=self.config,
            posts=posts,
            comment_groups=comment_groups,
        ).build_units(candidates)

        EventThreadWriter(self.config).write(units, preview)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


# ============================================================
# 10. CLI
# ============================================================

def build_config_from_args() -> EventThreadConfig:
    project_root = Path(__file__).resolve().parent.parent

    default_candidate_path = (
        project_root
        / "data"
        / "processed"
        / "dci_event_candidates_final"
        / "event_generation_candidates_for_llm_final.csv"
    )

    default_posts_path = (
        project_root
        / "data"
        / "processed"
        / "dci_stock_thread_comment_subset"
        / "stock_attributed_posts.csv"
    )

    default_comments_path = (
        project_root
        / "data"
        / "processed"
        / "dci_stock_thread_comment_subset"
        / "dci_comments_stock_thread_only.csv"
    )

    default_output_dir = (
        project_root
        / "data"
        / "processed"
        / "dci_llm_event_inputs"
    )

    parser = argparse.ArgumentParser()

    parser.add_argument("--candidate", type=str, default=str(default_candidate_path))
    parser.add_argument("--posts", type=str, default=str(default_posts_path))
    parser.add_argument("--comments", type=str, default=str(default_comments_path))
    parser.add_argument("--output-dir", type=str, default=str(default_output_dir))

    parser.add_argument("--thread-window-before-days", type=int, default=0)
    parser.add_argument("--thread-window-after-days", type=int, default=0)

    parser.add_argument("--max-threads-per-event", type=int, default=5)
    parser.add_argument("--max-comments-per-thread", type=int, default=40)

    parser.add_argument("--exclude-unmatched-candidates", action="store_true")

    args = parser.parse_args()

    return EventThreadConfig(
        project_root=project_root,
        candidate_path=Path(args.candidate).expanduser().resolve(),
        posts_path=Path(args.posts).expanduser().resolve(),
        comments_path=Path(args.comments).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        thread_window_before_days=args.thread_window_before_days,
        thread_window_after_days=args.thread_window_after_days,
        max_threads_per_event=args.max_threads_per_event,
        max_comments_per_thread=args.max_comments_per_thread,
        include_unmatched_candidates=not args.exclude_unmatched_candidates,
    )


def main() -> None:
    config = build_config_from_args()
    EventThreadPipeline(config).run()


if __name__ == "__main__":
    main()