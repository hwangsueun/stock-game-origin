import os
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


# =========================================================
# 설정
# =========================================================

class MergeConfig:
    """
    디시인사이드 수집 CSV 병합 설정
    """

    def __init__(self):
        # 현재 파일 위치:
        # /Users/hgs/Desktop/IISE CD/npc_generator/scripts/merge_dci_collected_files.py
        script_path = Path(__file__).resolve()

        # 프로젝트 루트:
        # /Users/hgs/Desktop/IISE CD/npc_generator
        self.project_root = script_path.parents[1]

        # 수집 CSV들이 들어있는 폴더:
        # /Users/hgs/Desktop/IISE CD/npc_generator/data/raw
        self.input_root = self.project_root / "data" / "raw"

        # 결과 저장 폴더:
        # /Users/hgs/Desktop/IISE CD/npc_generator/data/processed
        self.output_dir = self.project_root / "data" / "processed"

        self.posts_output_path = self.output_dir / "dci_posts_merged_dedup.csv"
        self.comments_output_path = self.output_dir / "dci_comments_merged_dedup.csv"
        self.report_output_path = self.output_dir / "dci_merge_report.csv"

        # 주의:
        # "policy"는 제외하면 안 됨.
        # dci_posts_policy_merged.csv 같은 실제 수집 데이터가 스킵됨.
        self.exclude_keywords = [
            "checkpoint",
            "job",
            "report",
            "dedup",
            "backup",
            ".DS_Store",
        ]

        # 이미 병합된 최종 산출물만 제외하고 싶으면 merged도 조심해야 함.
        # 현재 raw 폴더 안에 dci_posts_policy_merged.csv가 실제 데이터처럼 보이므로
        # "merged"도 제외하지 않는 게 안전함.

        self.encodings = [
            "utf-8-sig",
            "utf-8",
            "cp949",
            "euc-kr",
        ]


# =========================================================
# CSV 로더
# =========================================================

class CsvFileLoader:
    """
    여러 인코딩을 시도해서 CSV를 안전하게 읽는 클래스
    """

    def __init__(self, config: MergeConfig):
        self.config = config

    def find_csv_files(self) -> List[Path]:
        files = []

        for path in self.config.input_root.rglob("*.csv"):
            lower_path = str(path).lower()

            if any(keyword.lower() in lower_path for keyword in self.config.exclude_keywords):
                continue

            files.append(path)

        return sorted(files)

    def read_csv_safely(self, path: Path) -> Optional[pd.DataFrame]:
        for encoding in self.config.encodings:
            try:
                df = pd.read_csv(
                    path,
                    encoding=encoding,
                    dtype=str,
                    low_memory=False
                )
                df["source_file"] = str(path)
                df["source_filename"] = path.name
                return df

            except UnicodeDecodeError:
                continue

            except Exception as e:
                print(f"[READ ERROR] {path} | {type(e).__name__}: {e}")
                return None

        print(f"[ENCODING FAIL] {path}")
        return None


# =========================================================
# 컬럼 표준화
# =========================================================

class ColumnNormalizer:
    """
    파일마다 제각각인 컬럼명을 표준 컬럼명으로 맞춤
    """

    COLUMN_MAP = {
    # 갤러리
    "gall": "gall_id",
    "gallery": "gall_id",
    "gallery_id": "gall_id",
    "gall_id": "gall_id",
    "갤러리ID": "gall_id",
    "갤러리id": "gall_id",

    "gall_name": "gall_name",
    "gallery_name": "gall_name",
    "갤러리명": "gall_name",

    "gall_type": "gall_type",
    "gallery_type": "gall_type",
    "갤러리타입": "gall_type",

    # 글
    "no": "post_id",
    "num": "post_id",
    "post_no": "post_id",
    "post_num": "post_id",
    "post_id": "post_id",
    "article_id": "post_id",
    "글번호": "post_id",
    "게시글번호": "post_id",

    "title": "title",
    "subject": "title",
    "제목": "title",

    "content": "content",
    "body": "content",
    "text": "content",
    "본문": "content",
    "내용": "content",

    "author": "author",
    "nickname": "author",
    "nick": "author",
    "writer": "author",
    "작성자": "author",
    "닉네임": "author",

    "date": "date",
    "created_at": "date",
    "datetime": "date",
    "time": "date",
    "작성일": "date",
    "작성시간": "date",

    "view": "view_count",
    "views": "view_count",
    "view_count": "view_count",
    "조회수": "view_count",

    "recommend": "recommend_count",
    "recommend_count": "recommend_count",
    "up": "recommend_count",
    "추천": "recommend_count",

    "dislike": "dislike_count",
    "down": "dislike_count",
    "dislike_count": "dislike_count",
    "unrecommend": "dislike_count",
    "unrecommend_count": "dislike_count",
    "비추": "dislike_count",

    "comment_count": "comment_count",
    "comments": "comment_count",
    "댓글수": "comment_count",

    "url": "url",
    "link": "url",
    "링크": "url",

    # 댓글
    "comment_id": "comment_id",
    "comment_no": "comment_id",
    "reply_id": "comment_id",
    "reply_no": "comment_id",
    "댓글번호": "comment_id",

    # 네 수집 파일에서 쓰는 댓글 컬럼
    "cmt_no": "comment_id",
    "cmt_id": "comment_id",
    "cmt_writer": "author",
    "cmt_nick": "author",
    "cmt_date": "date",
    "cmt_body": "content",
    "cmt_content": "content",
    "cmt_text": "content",
    "cmt_rcnt": "recommend_count",

    "parent_id": "parent_comment_id",
    "parent_comment_id": "parent_comment_id",
    "부모댓글": "parent_comment_id",

    "is_reply": "is_reply",
    "depth": "depth",
}

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        new_columns = {}
        for col in df.columns:
            clean_col = self._clean_column_name(col)
            mapped_col = self.COLUMN_MAP.get(clean_col, clean_col)
            new_columns[col] = mapped_col

        df = df.rename(columns=new_columns)

        # 같은 이름으로 중복 rename된 컬럼 처리
        df = self._merge_duplicate_columns(df)

        return df

    def _clean_column_name(self, col: str) -> str:
        col = str(col).strip()
        col = col.replace(" ", "_")
        col = col.replace("-", "_")
        col = col.replace(".", "_")
        return col

    def _merge_duplicate_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        rename 결과로 같은 컬럼명이 여러 개 생긴 경우
        왼쪽부터 값이 있는 것을 우선 사용
        """
        duplicated_cols = df.columns[df.columns.duplicated()].unique()

        for col in duplicated_cols:
            same_cols = df.loc[:, df.columns == col]
            merged = same_cols.bfill(axis=1).iloc[:, 0]
            df = df.drop(columns=same_cols.columns)
            df[col] = merged

        return df


# =========================================================
# 파일 유형 분류
# =========================================================

class DcinsideFileClassifier:
    """
    CSV가 글 파일인지 댓글 파일인지 판별
    """

    def classify(self, path: Path, df: pd.DataFrame) -> str:
        filename = path.name.lower()
        columns = set(df.columns)

        # 파일명 기반 우선 판별
        if any(keyword in filename for keyword in ["comment", "comments", "reply", "replies", "댓글"]):
            return "comments"

        if any(keyword in filename for keyword in ["post", "posts", "article", "articles", "board", "글"]):
            return "posts"

        # 컬럼 기반 판별
        if "comment_id" in columns or "parent_comment_id" in columns:
            return "comments"

        # 댓글 파일은 보통 title이 없고 content/comment 관련만 있음
        if "post_id" in columns and "content" in columns and "title" not in columns:
            if "author" in columns and "date" in columns:
                return "comments"

        if "post_id" in columns and ("title" in columns or "url" in columns):
            return "posts"

        return "unknown"


# =========================================================
# 텍스트 정리 및 키 생성
# =========================================================

class DcinsideDataCleaner:
    """
    문자열 정리, 해시 생성, 중복 키 생성
    """

    def clean_common(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str)
                df[col] = df[col].replace({"nan": "", "None": "", "NaN": ""})
                df[col] = df[col].map(self._normalize_text)

        if "gall_id" in df.columns:
            df["gall_id"] = df["gall_id"].map(self._normalize_id)

        if "post_id" in df.columns:
            df["post_id"] = df["post_id"].map(self._normalize_id)

        if "comment_id" in df.columns:
            df["comment_id"] = df["comment_id"].map(self._normalize_id)

        if "date" in df.columns:
            df["date"] = df["date"].map(self._normalize_date_text)

        return df

    def make_post_keys(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        self._ensure_columns(df, ["gall_id", "post_id"])

        df["dedup_key"] = (
            df["gall_id"].fillna("").astype(str)
            + "::"
            + df["post_id"].fillna("").astype(str)
        )

        # post_id가 없는 비정상 row fallback
        invalid_mask = (
            df["gall_id"].fillna("").eq("")
            | df["post_id"].fillna("").eq("")
        )

        if invalid_mask.any():
            df.loc[invalid_mask, "dedup_key"] = df.loc[invalid_mask].apply(
                lambda row: self._hash_values([
                    row.get("gall_name", ""),
                    row.get("title", ""),
                    row.get("author", ""),
                    row.get("date", ""),
                    row.get("content", ""),
                    row.get("url", ""),
                ]),
                axis=1
            )

        return df

    def make_comment_keys(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        self._ensure_columns(df, ["gall_id", "post_id", "comment_id", "author", "date", "content"])

        has_comment_id = df["comment_id"].fillna("").astype(str).str.len() > 0

        df["dedup_key"] = ""

        df.loc[has_comment_id, "dedup_key"] = (
            df.loc[has_comment_id, "gall_id"].fillna("").astype(str)
            + "::"
            + df.loc[has_comment_id, "post_id"].fillna("").astype(str)
            + "::"
            + df.loc[has_comment_id, "comment_id"].fillna("").astype(str)
        )

        # comment_id가 없으면 내용 기반 fallback
        no_comment_id = ~has_comment_id

        if no_comment_id.any():
            df.loc[no_comment_id, "dedup_key"] = df.loc[no_comment_id].apply(
                lambda row: self._hash_values([
                    row.get("gall_id", ""),
                    row.get("post_id", ""),
                    row.get("author", ""),
                    row.get("date", ""),
                    row.get("content", ""),
                ]),
                axis=1
            )

        return df

    def add_content_hash(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        if "content" in df.columns:
            df["content_hash"] = df["content"].fillna("").map(
                lambda x: hashlib.md5(str(x).encode("utf-8")).hexdigest()
            )
        else:
            df["content_hash"] = ""

        return df

    def _normalize_text(self, value: str) -> str:
        value = str(value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _normalize_id(self, value: str) -> str:
        value = str(value).strip()

        if value in ["", "nan", "None", "NaN"]:
            return ""

        # 123.0 같은 형태 보정
        if re.fullmatch(r"\d+\.0", value):
            value = value[:-2]

        return value

    def _normalize_date_text(self, value: str) -> str:
        value = str(value).strip()

        if value in ["", "nan", "None", "NaN"]:
            return ""

        return value

    def _hash_values(self, values: List[str]) -> str:
        joined = "||".join([str(v) for v in values])
        return hashlib.md5(joined.encode("utf-8")).hexdigest()

    def _ensure_columns(self, df: pd.DataFrame, columns: List[str]) -> None:
        for col in columns:
            if col not in df.columns:
                df[col] = ""


# =========================================================
# 병합 및 중복 제거
# =========================================================

class DcinsideMerger:
    """
    디시인사이드 글/댓글 CSV 병합 실행 클래스
    """

    def __init__(self, config: MergeConfig):
        self.config = config
        self.loader = CsvFileLoader(config)
        self.normalizer = ColumnNormalizer()
        self.classifier = DcinsideFileClassifier()
        self.cleaner = DcinsideDataCleaner()

        self.report_rows = []

    def run(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        csv_files = self.loader.find_csv_files()

        print("=" * 80)
        print(f"[START] CSV files found: {len(csv_files)}")
        print(f"[INPUT ROOT] {self.config.input_root}")
        print("=" * 80)

        post_frames = []
        comment_frames = []
        unknown_files = []

        for idx, path in enumerate(csv_files, start=1):
            print(f"[{idx}/{len(csv_files)}] {path}")

            raw_df = self.loader.read_csv_safely(path)

            if raw_df is None or raw_df.empty:
                self._append_report(path, "read_fail_or_empty", 0, 0)
                continue

            normalized_df = self.normalizer.normalize(raw_df)
            file_type = self.classifier.classify(path, normalized_df)

            if file_type == "posts":
                post_frames.append(normalized_df)
                self._append_report(path, "posts", len(normalized_df), len(normalized_df))

            elif file_type == "comments":
                comment_frames.append(normalized_df)
                self._append_report(path, "comments", len(normalized_df), len(normalized_df))

            else:
                unknown_files.append(path)
                self._append_report(path, "unknown", len(normalized_df), 0)
                print(f"  -> [SKIP UNKNOWN] columns={list(normalized_df.columns)}")

        posts = self._merge_posts(post_frames)
        comments = self._merge_comments(comment_frames)

        self._save_report()

        print("=" * 80)
        print("[DONE]")
        print(f"posts output    : {self.config.posts_output_path}")
        print(f"comments output : {self.config.comments_output_path}")
        print(f"report output   : {self.config.report_output_path}")
        print(f"unknown files   : {len(unknown_files)}")
        print("=" * 80)

        if unknown_files:
            print("[UNKNOWN FILES]")
            for path in unknown_files:
                print(f"- {path}")

    def _merge_posts(self, frames: List[pd.DataFrame]) -> pd.DataFrame:
        print("=" * 80)
        print("[MERGE POSTS]")

        if not frames:
            print("No post files found.")
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        before = len(df)

        df = self.cleaner.clean_common(df)
        df = self.cleaner.add_content_hash(df)
        df = self.cleaner.make_post_keys(df)

        # 완전히 빈 글 제거
        df = df[~(
            df.get("post_id", "").fillna("").eq("")
            & df.get("title", "").fillna("").eq("")
            & df.get("content", "").fillna("").eq("")
        )].copy()

        before_drop = len(df)

        # 같은 dedup_key 중 첫 번째 유지
        df = df.drop_duplicates(subset=["dedup_key"], keep="first").copy()

        after = len(df)

        df = self._sort_posts(df)
        df = self._order_post_columns(df)

        df.to_csv(self.config.posts_output_path, index=False, encoding="utf-8-sig")

        print(f"raw rows          : {before:,}")
        print(f"valid rows        : {before_drop:,}")
        print(f"deduplicated rows : {after:,}")
        print(f"duplicates removed: {before_drop - after:,}")

        return df

    def _merge_comments(self, frames: List[pd.DataFrame]) -> pd.DataFrame:
        print("=" * 80)
        print("[MERGE COMMENTS]")

        if not frames:
            print("No comment files found.")
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        before = len(df)

        df = self.cleaner.clean_common(df)
        df = self.cleaner.add_content_hash(df)
        df = self.cleaner.make_comment_keys(df)

        missing_comment_id_ratio = df["comment_id"].fillna("").astype(str).eq("").mean()
        missing_content_ratio = df["content"].fillna("").astype(str).eq("").mean()

        print(f"missing comment_id ratio: {missing_comment_id_ratio:.4f}")
        print(f"missing content ratio   : {missing_content_ratio:.4f}")

        if missing_comment_id_ratio > 0.5 and missing_content_ratio > 0.5:
            raise ValueError(
                "댓글 컬럼 매핑 실패 가능성이 큼. "
                "comment_id와 content가 대부분 비어 있음. "
                "cmt_no, cmt_body 컬럼 매핑을 확인해야 함."
            )

        # 완전히 빈 댓글 제거
        df = df[~(
            df.get("post_id", "").fillna("").eq("")
            & df.get("comment_id", "").fillna("").eq("")
            & df.get("content", "").fillna("").eq("")
        )].copy()

        before_drop = len(df)

        df = df.drop_duplicates(subset=["dedup_key"], keep="first").copy()

        after = len(df)

        df = self._sort_comments(df)
        df = self._order_comment_columns(df)

        df.to_csv(self.config.comments_output_path, index=False, encoding="utf-8-sig")

        print(f"raw rows          : {before:,}")
        print(f"valid rows        : {before_drop:,}")
        print(f"deduplicated rows : {after:,}")
        print(f"duplicates removed: {before_drop - after:,}")

        return df

    def _sort_posts(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        sort_cols = []
        for col in ["gall_id", "post_id", "date"]:
            if col in df.columns:
                sort_cols.append(col)

        if sort_cols:
            df = df.sort_values(sort_cols, ascending=True)

        return df.reset_index(drop=True)

    def _sort_comments(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        sort_cols = []
        for col in ["gall_id", "post_id", "comment_id", "date"]:
            if col in df.columns:
                sort_cols.append(col)

        if sort_cols:
            df = df.sort_values(sort_cols, ascending=True)

        return df.reset_index(drop=True)

    def _order_post_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        preferred = [
            "gall_id",
            "gall_name",
            "gall_type",
            "post_id",
            "title",
            "content",
            "author",
            "date",
            "view_count",
            "recommend_count",
            "dislike_count",
            "comment_count",
            "url",
            "content_hash",
            "dedup_key",
            "source_file",
            "source_filename",
        ]

        return self._reorder_columns(df, preferred)

    def _order_comment_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        preferred = [
            "gall_id",
            "gall_name",
            "gall_type",
            "post_id",
            "comment_id",
            "parent_comment_id",
            "is_reply",
            "depth",
            "content",
            "author",
            "date",
            "recommend_count",
            "dislike_count",
            "content_hash",
            "dedup_key",
            "source_file",
            "source_filename",
        ]

        return self._reorder_columns(df, preferred)

    def _reorder_columns(self, df: pd.DataFrame, preferred: List[str]) -> pd.DataFrame:
        existing_preferred = [col for col in preferred if col in df.columns]
        remaining = [col for col in df.columns if col not in existing_preferred]
        return df[existing_preferred + remaining]

    def _append_report(
        self,
        path: Path,
        file_type: str,
        input_rows: int,
        used_rows: int
    ) -> None:
        self.report_rows.append({
            "file_path": str(path),
            "file_name": path.name,
            "file_type": file_type,
            "input_rows": input_rows,
            "used_rows": used_rows,
        })

    def _save_report(self) -> None:
        report_df = pd.DataFrame(self.report_rows)
        report_df.to_csv(self.config.report_output_path, index=False, encoding="utf-8-sig")


# =========================================================
# 실행부
# =========================================================

def main():
    config = MergeConfig()

    # 필요하면 여기서 직접 경로 수정
    # 예시:
    # config.input_root = Path("/Users/hgs/Desktop/IISE CD/npc_generator/data/raw")
    # config.output_dir = Path("/Users/hgs/Desktop/IISE CD/npc_generator/data/processed")

    merger = DcinsideMerger(config)
    merger.run()


if __name__ == "__main__":
    main()