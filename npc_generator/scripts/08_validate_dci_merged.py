from pathlib import Path
from typing import List, Dict

import pandas as pd


class DcinsideValidationConfig:
    def __init__(self):
        script_path = Path(__file__).resolve()
        self.project_root = script_path.parents[1]

        self.processed_dir = self.project_root / "data" / "processed"

        self.posts_path = self.processed_dir / "dci_posts_merged_dedup.csv"
        self.comments_path = self.processed_dir / "dci_comments_merged_dedup.csv"

        self.posts_ready_path = self.processed_dir / "dci_posts_ready.csv"
        self.comments_ready_path = self.processed_dir / "dci_comments_ready.csv"

        self.gallery_gap_path = self.processed_dir / "dci_gallery_gap_report.csv"
        self.post_missing_by_source_path = self.processed_dir / "dci_post_missing_by_source.csv"
        self.comment_missing_by_source_path = self.processed_dir / "dci_comment_missing_by_source.csv"
        self.comment_parent_coverage_path = self.processed_dir / "dci_comment_parent_coverage.csv"


class DcinsideMergedValidator:
    def __init__(self, config: DcinsideValidationConfig):
        self.config = config

    def run(self):
        posts = self._read_csv(self.config.posts_path)
        comments = self._read_csv(self.config.comments_path)

        print("=" * 80)
        print("[BASIC COUNTS]")
        print(f"posts rows    : {len(posts):,}")
        print(f"comments rows : {len(comments):,}")
        print(f"posts gall    : {posts['gall_id'].nunique():,}")
        print(f"comments gall : {comments['gall_id'].nunique():,}")

        self._check_duplicate_keys(posts, comments)
        self._check_gallery_gap(posts, comments)
        self._check_missing_by_source(posts, comments)
        self._check_comment_parent_coverage(posts, comments)

        posts_ready, comments_ready = self._build_ready_files(posts, comments)

        posts_ready.to_csv(self.config.posts_ready_path, index=False, encoding="utf-8-sig")
        comments_ready.to_csv(self.config.comments_ready_path, index=False, encoding="utf-8-sig")

        print("=" * 80)
        print("[DONE]")
        print(f"posts ready    : {self.config.posts_ready_path}")
        print(f"comments ready : {self.config.comments_ready_path}")

    def _read_csv(self, path: Path) -> pd.DataFrame:
        return pd.read_csv(path, encoding="utf-8-sig", dtype=str, low_memory=False)

    def _check_duplicate_keys(self, posts: pd.DataFrame, comments: pd.DataFrame):
        print("=" * 80)
        print("[DUPLICATE KEY CHECK]")

        post_dup = posts["dedup_key"].duplicated().sum()
        comment_dup = comments["dedup_key"].duplicated().sum()

        print(f"post duplicate dedup_key    : {post_dup:,}")
        print(f"comment duplicate dedup_key : {comment_dup:,}")

    def _check_gallery_gap(self, posts: pd.DataFrame, comments: pd.DataFrame):
        print("=" * 80)
        print("[GALLERY GAP CHECK]")

        post_galls = set(posts["gall_id"].dropna().astype(str))
        comment_galls = set(comments["gall_id"].dropna().astype(str))

        post_only = sorted(post_galls - comment_galls)
        comment_only = sorted(comment_galls - post_galls)

        print(f"post only galleries    : {len(post_only)}")
        print(f"comment only galleries : {len(comment_only)}")

        if post_only:
            print("\n[POST ONLY]")
            for gall in post_only:
                print("-", gall)

        if comment_only:
            print("\n[COMMENT ONLY]")
            for gall in comment_only:
                print("-", gall)

        rows = []

        for gall in sorted(post_galls | comment_galls):
            rows.append({
                "gall_id": gall,
                "post_rows": int((posts["gall_id"] == gall).sum()),
                "comment_rows": int((comments["gall_id"] == gall).sum()),
                "exists_in_posts": gall in post_galls,
                "exists_in_comments": gall in comment_galls,
            })

        report = pd.DataFrame(rows)
        report.to_csv(self.config.gallery_gap_path, index=False, encoding="utf-8-sig")

        print(f"\ngallery gap report saved: {self.config.gallery_gap_path}")

    def _check_missing_by_source(self, posts: pd.DataFrame, comments: pd.DataFrame):
        print("=" * 80)
        print("[MISSING BY SOURCE]")

        post_cols = ["gall_id", "post_id", "title", "content", "author", "date"]
        comment_cols = ["gall_id", "post_id", "comment_id", "content", "author", "date"]

        post_report = self._missing_report_by_source(posts, post_cols)
        comment_report = self._missing_report_by_source(comments, comment_cols)

        post_report.to_csv(self.config.post_missing_by_source_path, index=False, encoding="utf-8-sig")
        comment_report.to_csv(self.config.comment_missing_by_source_path, index=False, encoding="utf-8-sig")

        print("\n[POST MISSING TOP 10]")
        print(post_report.head(10))

        print("\n[COMMENT MISSING TOP 10]")
        print(comment_report.head(10))

        print(f"\npost missing report saved    : {self.config.post_missing_by_source_path}")
        print(f"comment missing report saved : {self.config.comment_missing_by_source_path}")

    def _missing_report_by_source(self, df: pd.DataFrame, target_cols: List[str]) -> pd.DataFrame:
        if "source_filename" not in df.columns:
            df["source_filename"] = "unknown"

        rows = []

        for source_name, group in df.groupby("source_filename", dropna=False):
            row = {
                "source_filename": source_name,
                "rows": len(group),
            }

            for col in target_cols:
                if col in group.columns:
                    row[f"{col}_missing_ratio"] = group[col].fillna("").astype(str).str.strip().eq("").mean()
                else:
                    row[f"{col}_missing_ratio"] = 1.0

            rows.append(row)

        report = pd.DataFrame(rows)

        missing_cols = [col for col in report.columns if col.endswith("_missing_ratio")]
        report["avg_missing_ratio"] = report[missing_cols].mean(axis=1)

        return report.sort_values(
            ["avg_missing_ratio", "rows"],
            ascending=[False, False]
        ).reset_index(drop=True)

    def _check_comment_parent_coverage(self, posts: pd.DataFrame, comments: pd.DataFrame):
        print("=" * 80)
        print("[COMMENT PARENT POST COVERAGE]")

        post_keys = posts[["gall_id", "post_id"]].drop_duplicates().copy()
        post_keys["has_parent_post"] = True

        merged = comments[["gall_id", "post_id"]].merge(
            post_keys,
            on=["gall_id", "post_id"],
            how="left"
        )

        coverage = merged["has_parent_post"].fillna(False).mean()

        print(f"comments with parent post coverage: {coverage:.4f}")

        by_gall = merged.copy()
        by_gall["has_parent_post"] = by_gall["has_parent_post"].fillna(False)

        report = (
            by_gall
            .groupby("gall_id")
            .agg(
                comment_rows=("post_id", "count"),
                parent_coverage=("has_parent_post", "mean")
            )
            .reset_index()
            .sort_values(["parent_coverage", "comment_rows"], ascending=[True, False])
        )

        report.to_csv(self.config.comment_parent_coverage_path, index=False, encoding="utf-8-sig")

        print("\n[PARENT COVERAGE LOW TOP 10]")
        print(report.head(10))

        print(f"\nparent coverage report saved: {self.config.comment_parent_coverage_path}")

    def _build_ready_files(
        self,
        posts: pd.DataFrame,
        comments: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        print("=" * 80)
        print("[BUILD READY FILES]")

        posts = posts.copy()
        comments = comments.copy()

        posts["post_date_final"] = posts["date"].fillna("").astype(str).str.strip()

        post_date_map = posts[["gall_id", "post_id", "post_date_final"]].drop_duplicates(
            subset=["gall_id", "post_id"],
            keep="first"
        )

        comments = comments.merge(
            post_date_map,
            on=["gall_id", "post_id"],
            how="left"
        )

        comments["comment_date_raw"] = comments["date"].fillna("").astype(str).str.strip()
        comments["comment_date_final"] = comments["comment_date_raw"]

        missing_comment_date = comments["comment_date_final"].eq("")
        comments.loc[missing_comment_date, "comment_date_final"] = (
            comments.loc[missing_comment_date, "post_date_final"]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        posts["activity_date"] = posts["post_date_final"].map(self._extract_date)
        comments["activity_date"] = comments["comment_date_final"].map(self._extract_date)

        print(f"posts activity_date missing ratio    : {posts['activity_date'].eq('').mean():.4f}")
        print(f"comments activity_date missing ratio : {comments['activity_date'].eq('').mean():.4f}")

        post_keep_cols = [
            "gall_id",
            "gall_type",
            "post_id",
            "title",
            "content",
            "author",
            "date",
            "post_date_final",
            "activity_date",
            "view_count",
            "recommend_count",
            "dislike_count",
            "comment_count",
            "url",
            "content_hash",
            "dedup_key",
            "source_filename",
        ]

        comment_keep_cols = [
            "gall_id",
            "post_id",
            "comment_id",
            "content",
            "author",
            "date",
            "comment_date_raw",
            "comment_date_final",
            "post_date_final",
            "activity_date",
            "recommend_count",
            "dislike_count",
            "content_hash",
            "dedup_key",
            "source_filename",
        ]

        posts_ready = self._select_existing_columns(posts, post_keep_cols)
        comments_ready = self._select_existing_columns(comments, comment_keep_cols)

        return posts_ready, comments_ready

    def _select_existing_columns(self, df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
        existing = [col for col in columns if col in df.columns]
        rest = [col for col in df.columns if col not in existing]
        return df[existing + rest]

    def _extract_date(self, value: str) -> str:
        value = str(value).strip()

        if value == "" or value.lower() == "nan":
            return ""

        # 예: 2024-01-03 12:34:56
        if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
            return value[:10]

        # 예: 2024.01.03 12:34
        if len(value) >= 10 and value[4:5] == "." and value[7:8] == ".":
            return value[:10].replace(".", "-")

        # 예: 2024/01/03
        if len(value) >= 10 and value[4:5] == "/" and value[7:8] == "/":
            return value[:10].replace("/", "-")

        return value[:10]


def main():
    config = DcinsideValidationConfig()
    validator = DcinsideMergedValidator(config)
    validator.run()


if __name__ == "__main__":
    main()