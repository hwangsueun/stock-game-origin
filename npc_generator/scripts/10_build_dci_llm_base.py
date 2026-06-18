from pathlib import Path
from typing import List

import pandas as pd


class DciLlmBaseConfig:
    def __init__(self):
        script_path = Path(__file__).resolve()
        self.project_root = script_path.parents[1]
        self.processed_dir = self.project_root / "data" / "processed"

        self.posts_path = self.processed_dir / "dci_posts_ready.csv"
        self.comments_path = self.processed_dir / "dci_comments_ready.csv"

        self.comments_with_parent_path = self.processed_dir / "dci_comments_with_parent_context.csv"
        self.daily_gallery_stats_path = self.processed_dir / "dci_daily_gallery_stats.csv"


class DciLlmBaseBuilder:
    def __init__(self, config: DciLlmBaseConfig):
        self.config = config

    def run(self):
        posts = self._read_csv(self.config.posts_path)
        comments = self._read_csv(self.config.comments_path)

        posts = self._normalize_keys(posts)
        comments = self._normalize_keys(comments)

        comments_with_parent = self._attach_parent_post_context(posts, comments)
        daily_stats = self._build_daily_gallery_stats(comments_with_parent)

        comments_with_parent.to_csv(
            self.config.comments_with_parent_path,
            index=False,
            encoding="utf-8-sig"
        )

        daily_stats.to_csv(
            self.config.daily_gallery_stats_path,
            index=False,
            encoding="utf-8-sig"
        )

        self._print_summary(comments_with_parent, daily_stats)

    def _read_csv(self, path: Path) -> pd.DataFrame:
        return pd.read_csv(
            path,
            encoding="utf-8-sig",
            dtype=str,
            low_memory=False
        )

    def _normalize_keys(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in ["gall_id", "post_id", "comment_id"]:
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str).str.strip()
                df[col] = df[col].str.replace(r"\.0$", "", regex=True)

        if "activity_date" in df.columns:
            df["activity_date"] = df["activity_date"].fillna("").astype(str).str.strip()

        return df

    def _attach_parent_post_context(
        self,
        posts: pd.DataFrame,
        comments: pd.DataFrame
    ) -> pd.DataFrame:
        posts_context_cols = [
            "gall_id",
            "post_id",
            "title",
            "content",
            "author",
            "date",
            "activity_date",
            "view_count",
            "recommend_count",
            "dislike_count",
            "comment_count",
        ]

        posts_context_cols = [col for col in posts_context_cols if col in posts.columns]

        post_context = posts[posts_context_cols].drop_duplicates(
            subset=["gall_id", "post_id"],
            keep="first"
        ).copy()

        rename_map = {
            "title": "parent_post_title",
            "content": "parent_post_content",
            "author": "parent_post_author",
            "date": "parent_post_date",
            "activity_date": "parent_post_activity_date",
            "view_count": "parent_post_view_count",
            "recommend_count": "parent_post_recommend_count",
            "dislike_count": "parent_post_dislike_count",
            "comment_count": "parent_post_comment_count",
        }

        post_context = post_context.rename(columns=rename_map)

        merged = comments.merge(
            post_context,
            on=["gall_id", "post_id"],
            how="left"
        )

        merged["has_parent_post"] = merged["parent_post_title"].fillna("").astype(str).str.strip().ne("")

        # 댓글 날짜가 있으면 댓글 날짜 우선, 없으면 부모글 날짜 사용
        merged["llm_activity_date"] = merged["activity_date"].fillna("").astype(str).str.strip()

        missing_date = merged["llm_activity_date"].eq("")
        if "parent_post_activity_date" in merged.columns:
            merged.loc[missing_date, "llm_activity_date"] = (
                merged.loc[missing_date, "parent_post_activity_date"]
                .fillna("")
                .astype(str)
                .str.strip()
            )

        merged["comment_only"] = ~merged["has_parent_post"]

        # LLM 입력에서 쓸 최소 텍스트 필드
        merged["llm_context_text"] = merged.apply(self._make_llm_context_text, axis=1)

        return merged

    def _make_llm_context_text(self, row: pd.Series) -> str:
        gall_id = str(row.get("gall_id", "")).strip()
        date = str(row.get("llm_activity_date", "")).strip()

        parent_title = str(row.get("parent_post_title", "")).strip()
        parent_content = str(row.get("parent_post_content", "")).strip()
        comment_content = str(row.get("content", "")).strip()

        parts = []

        parts.append(f"[갤러리] {gall_id}")

        if date:
            parts.append(f"[날짜] {date}")

        if parent_title:
            parts.append(f"[원글 제목] {parent_title}")

        if parent_content:
            parent_content = self._truncate(parent_content, 500)
            parts.append(f"[원글 내용] {parent_content}")

        if comment_content:
            comment_content = self._truncate(comment_content, 300)
            parts.append(f"[댓글] {comment_content}")

        return "\n".join(parts)

    def _truncate(self, text: str, max_len: int) -> str:
        text = str(text).strip()
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    def _build_daily_gallery_stats(self, df: pd.DataFrame) -> pd.DataFrame:
        valid = df.copy()
        valid = valid[valid["llm_activity_date"].fillna("").astype(str).str.strip().ne("")]

        valid["has_parent_post_int"] = valid["has_parent_post"].astype(int)
        valid["comment_only_int"] = valid["comment_only"].astype(int)

        stats = (
            valid
            .groupby(["llm_activity_date", "gall_id"])
            .agg(
                comment_count=("comment_id", "count"),
                unique_post_count=("post_id", "nunique"),
                parent_matched_comment_count=("has_parent_post_int", "sum"),
                comment_only_count=("comment_only_int", "sum"),
            )
            .reset_index()
        )

        stats["parent_match_ratio"] = (
            stats["parent_matched_comment_count"] / stats["comment_count"]
        )

        stats = stats.sort_values(
            ["llm_activity_date", "comment_count"],
            ascending=[True, False]
        ).reset_index(drop=True)

        return stats

    def _print_summary(self, comments_with_parent: pd.DataFrame, daily_stats: pd.DataFrame):
        print("=" * 80)
        print("[DONE]")
        print("comments with parent context:", len(comments_with_parent))
        print("daily gallery stats rows    :", len(daily_stats))

        print("\n[PARENT MATCH]")
        print(comments_with_parent["has_parent_post"].value_counts(dropna=False))
        print("parent match ratio:", comments_with_parent["has_parent_post"].mean())

        print("\n[DATE MISSING]")
        print(
            comments_with_parent["llm_activity_date"]
            .fillna("")
            .astype(str)
            .str.strip()
            .eq("")
            .mean()
        )

        print("\n[TOP DAILY STATS]")
        print(daily_stats.head(20))

        print("\noutputs:")
        print(self.config.comments_with_parent_path)
        print(self.config.daily_gallery_stats_path)


def main():
    config = DciLlmBaseConfig()
    builder = DciLlmBaseBuilder(config)
    builder.run()


if __name__ == "__main__":
    main()