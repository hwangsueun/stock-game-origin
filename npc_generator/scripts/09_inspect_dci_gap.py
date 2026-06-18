from pathlib import Path
import pandas as pd


class GapInspectorConfig:
    def __init__(self):
        script_path = Path(__file__).resolve()
        self.project_root = script_path.parents[1]
        self.processed_dir = self.project_root / "data" / "processed"

        self.posts_path = self.processed_dir / "dci_posts_ready.csv"
        self.comments_path = self.processed_dir / "dci_comments_ready.csv"

        self.output_path = self.processed_dir / "dci_missing_parent_comments_sample.csv"


class DcinsideGapInspector:
    def __init__(self, config: GapInspectorConfig):
        self.config = config

    def run(self):
        posts = pd.read_csv(self.config.posts_path, encoding="utf-8-sig", dtype=str, low_memory=False)
        comments = pd.read_csv(self.config.comments_path, encoding="utf-8-sig", dtype=str, low_memory=False)

        posts = self._normalize_key_columns(posts)
        comments = self._normalize_key_columns(comments)

        print("=" * 80)
        print("[BASIC]")
        print("posts:", len(posts))
        print("comments:", len(comments))

        self._inspect_gap(posts, comments)
        self._inspect_suspicious_galleries(posts, comments)

    def _normalize_key_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in ["gall_id", "post_id", "comment_id"]:
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str).str.strip()
                df[col] = df[col].str.replace(r"\.0$", "", regex=True)

        return df

    def _inspect_gap(self, posts: pd.DataFrame, comments: pd.DataFrame):
        print("=" * 80)
        print("[MISSING PARENT COMMENTS]")

        post_keys = posts[["gall_id", "post_id"]].drop_duplicates()
        post_keys["has_parent_post"] = "Y"

        merged = comments.merge(
            post_keys,
            on=["gall_id", "post_id"],
            how="left"
        )

        missing = merged[merged["has_parent_post"].isna()].copy()

        print("missing parent comments:", len(missing))
        print("missing ratio:", len(missing) / len(comments))

        by_gall = (
            missing
            .groupby("gall_id")
            .agg(
                missing_comments=("comment_id", "count"),
                unique_missing_posts=("post_id", "nunique")
            )
            .reset_index()
            .sort_values("missing_comments", ascending=False)
        )

        print("\n[MISSING BY GALLERY]")
        print(by_gall.head(30))

        sample_cols = [
            "gall_id",
            "post_id",
            "comment_id",
            "author",
            "date",
            "activity_date",
            "content",
            "source_filename",
        ]
        sample_cols = [col for col in sample_cols if col in missing.columns]

        sample = (
            missing
            .sort_values(["gall_id", "post_id", "comment_id"])
            [sample_cols]
            .head(5000)
        )

        sample.to_csv(self.config.output_path, index=False, encoding="utf-8-sig")
        print("\nsample saved:", self.config.output_path)

    def _inspect_suspicious_galleries(self, posts: pd.DataFrame, comments: pd.DataFrame):
        print("=" * 80)
        print("[SUSPICIOUS GALLERY CHECK]")

        targets = [
            "chartmaster",
            "chartanalysis",
            "tenbagger",
            "securities",
            "dragontail",
            "issue1",
            "lumira",
            "passiveindexfund",
            "scamstock123",
            "stnec",
        ]

        for gall in targets:
            post_g = posts[posts["gall_id"] == gall]
            comment_g = comments[comments["gall_id"] == gall]

            print("-" * 80)
            print("gall_id:", gall)
            print("post rows:", len(post_g), "unique posts:", post_g["post_id"].nunique() if len(post_g) else 0)
            print("comment rows:", len(comment_g), "unique commented posts:", comment_g["post_id"].nunique() if len(comment_g) else 0)

            if len(post_g):
                print("post source files:")
                print(post_g["source_filename"].value_counts().head(10))

            if len(comment_g):
                print("comment source files:")
                print(comment_g["source_filename"].value_counts().head(10))

            if len(post_g) and len(comment_g):
                post_ids = set(post_g["post_id"])
                comment_post_ids = set(comment_g["post_id"])

                overlap = len(post_ids & comment_post_ids)
                print("post_id overlap:", overlap)
                print("comment post_id coverage:", overlap / max(len(comment_post_ids), 1))


def main():
    config = GapInspectorConfig()
    inspector = DcinsideGapInspector(config)
    inspector.run()


if __name__ == "__main__":
    main()