import os
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


# =========================================================
# 경로 설정
# 현재 파일 위치: scripts/gall_division/
# 입력:
#   - data/raw/dci_gallery_page_counts.csv
#   - data/raw/dci_posts*.csv
#   - data/raw/dci_comments*.csv
# 출력:
#   - data/gall_division/gallery_policy_decision.csv
#   - data/gall_division/type2_monthly_sampled_posts.csv
#   - data/gall_division/type2_monthly_sample_summary.csv
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../scripts/gall_division
SCRIPTS_DIR = os.path.dirname(BASE_DIR)                        # .../scripts
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)                    # .../npc_generator

RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
OUT_DIR = os.path.join(PROJECT_ROOT, "data", "gall_division")
os.makedirs(OUT_DIR, exist_ok=True)

PAGE_COUNT_CSV = os.path.join(RAW_DIR, "dci_gallery_page_counts.csv")

POLICY_CSV = os.path.join(OUT_DIR, "gallery_policy_decision.csv")
TYPE2_POSTS_CSV = os.path.join(OUT_DIR, "type2_monthly_sampled_posts.csv")
TYPE2_SUMMARY_CSV = os.path.join(OUT_DIR, "type2_monthly_sample_summary.csv")


@dataclass
class PolicyConfig:
    page_count_csv: str = PAGE_COUNT_CSV
    raw_dir: str = RAW_DIR
    out_dir: str = OUT_DIR

    # 통계 결과 기반 경계
    general_max_pages: int = 165
    sampled_min_pages: int = 319
    sampled_max_pages: int = 2635
    recommend_min_pages: int = 9750

    # 기간
    start_date: str = "2013-01-01"
    end_date: str = "2023-12-31"

    # 유형 2 후보 규칙
    # 추천수 2 이상 OR 댓글수 2 이상
    min_comment_count: int = 2
    min_recommend_count: int = 2

    # 유형 2 월별 quota
    min_monthly_quota: int = 100
    quota_ratio: float = 0.30
    max_monthly_quota: int = 500

    # 정렬
    use_score_sort: bool = True


class GalleryPolicyBuilder:
    def __init__(self, config: PolicyConfig):
        self.config = config

    # =====================================================
    # 파일 로드
    # =====================================================
    def _find_files(self, prefix: str) -> list[str]:
        files = []
        for name in os.listdir(self.config.raw_dir):
            if name.startswith(prefix) and name.endswith(".csv"):
                files.append(os.path.join(self.config.raw_dir, name))
        files.sort()
        return files

    def load_page_counts(self) -> pd.DataFrame:
        if not os.path.exists(self.config.page_count_csv):
            raise FileNotFoundError(f"파일 없음: {self.config.page_count_csv}")

        df = pd.read_csv(self.config.page_count_csv)
        required = {"gallery_name", "gall_id", "gall_type", "total_pages"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"page count csv 누락 컬럼: {sorted(missing)}")

        df = df.copy()
        df["total_pages"] = pd.to_numeric(df["total_pages"], errors="coerce")
        df = df.dropna(subset=["total_pages"]).copy()
        df["total_pages"] = df["total_pages"].astype(int)
        return df

    def load_posts(self) -> pd.DataFrame:
        post_files = self._find_files("dci_posts")
        if not post_files:
            raise FileNotFoundError(f"{self.config.raw_dir} 안에 dci_posts*.csv 없음")

        dfs = []
        usecols = [
            "gall_id", "gall_type", "post_no", "title", "writer", "date",
            "views", "recommend", "unrecommend", "body"
        ]

        for path in post_files:
            try:
                df = pd.read_csv(path, usecols=lambda c: c in usecols, low_memory=False)
                dfs.append(df)
            except Exception as e:
                print(f"[posts skip] {path} -> {e}")

        if not dfs:
            raise ValueError("읽을 수 있는 posts csv가 없음")

        df = pd.concat(dfs, ignore_index=True).copy()

        df["gall_id"] = df["gall_id"].astype(str).str.strip()
        df["post_no"] = df["post_no"].astype(str).str.strip()
        df["recommend"] = pd.to_numeric(df["recommend"], errors="coerce").fillna(0).astype(int)
        df["unrecommend"] = pd.to_numeric(df["unrecommend"], errors="coerce").fillna(0).astype(int)
        df["views"] = pd.to_numeric(df["views"], errors="coerce").fillna(0).astype(int)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        df = df.dropna(subset=["date"]).copy()
        df = df[(df["date"] >= self.config.start_date) & (df["date"] <= self.config.end_date)].copy()

        df = df.drop_duplicates(subset=["gall_id", "post_no"], keep="first").copy()
        return df

    def load_comment_counts(self) -> pd.DataFrame:
        comment_files = self._find_files("dci_comments")
        if not comment_files:
            raise FileNotFoundError(f"{self.config.raw_dir} 안에 dci_comments*.csv 없음")

        dfs = []
        usecols = ["gall_id", "post_no", "cmt_no"]

        for path in comment_files:
            try:
                df = pd.read_csv(path, usecols=lambda c: c in usecols, low_memory=False)
                dfs.append(df)
            except Exception as e:
                print(f"[comments skip] {path} -> {e}")

        if not dfs:
            raise ValueError("읽을 수 있는 comments csv가 없음")

        cmt = pd.concat(dfs, ignore_index=True).copy()

        cmt["gall_id"] = cmt["gall_id"].astype(str).str.strip()
        cmt["post_no"] = cmt["post_no"].astype(str).str.strip()

        if "cmt_no" in cmt.columns:
            cmt["cmt_no"] = cmt["cmt_no"].astype(str).fillna("").str.strip()
            cmt = cmt.drop_duplicates(subset=["gall_id", "post_no", "cmt_no"], keep="first").copy()

        count_df = (
            cmt.groupby(["gall_id", "post_no"], as_index=False)
               .size()
               .rename(columns={"size": "comment_count"})
        )
        return count_df

    # =====================================================
    # 정책 결정
    # =====================================================
    def build_policy_table(self, page_df: pd.DataFrame) -> pd.DataFrame:
        df = page_df.copy()

        def decide_policy(total_pages: int) -> str:
            if total_pages <= self.config.general_max_pages:
                return "general_full"
            if self.config.sampled_min_pages <= total_pages <= self.config.sampled_max_pages:
                return "general_monthly_stratified"
            if total_pages >= self.config.recommend_min_pages:
                return "recommend_only"
            return "gap_unassigned"

        df["collection_policy"] = df["total_pages"].apply(decide_policy)

        def decide_group(total_pages: int) -> str:
            if total_pages <= self.config.general_max_pages:
                return "type_1_general"
            if self.config.sampled_min_pages <= total_pages <= self.config.sampled_max_pages:
                return "type_2_sampled"
            if total_pages >= self.config.recommend_min_pages:
                return "type_3_recommend"
            return "gap"

        df["policy_group"] = df["total_pages"].apply(decide_group)
        df["log10_pages"] = np.log10(df["total_pages"].astype(float))
        return df.sort_values(["total_pages", "gallery_name"], ascending=[True, True]).reset_index(drop=True)

    # =====================================================
    # 유형 2 월별 층화추출
    # 후보 조건:
    #   추천수 >= 2 OR 댓글수 >= 2
    # 비추수는 수집은 하지만 후보 판정에는 사용하지 않음
    # =====================================================
    def build_type2_candidates(
        self,
        posts_df: pd.DataFrame,
        comment_count_df: pd.DataFrame,
        policy_df: pd.DataFrame,
    ) -> pd.DataFrame:
        type2_gall_ids = set(
            policy_df.loc[policy_df["policy_group"] == "type_2_sampled", "gall_id"]
            .astype(str).str.strip().tolist()
        )

        posts = posts_df[posts_df["gall_id"].isin(type2_gall_ids)].copy()
        posts = posts.merge(
            comment_count_df,
            on=["gall_id", "post_no"],
            how="left"
        )
        posts["comment_count"] = posts["comment_count"].fillna(0).astype(int)

        posts = posts[
            (posts["comment_count"] >= self.config.min_comment_count) |
            (posts["recommend"] >= self.config.min_recommend_count)
        ].copy()

        posts["year_month"] = posts["date"].dt.to_period("M").astype(str)

        # 비추수는 저장만, 점수에는 사용하지 않음
        posts["score"] = np.log1p(posts["comment_count"]) + np.log1p(posts["recommend"])

        return posts

    def sample_type2_monthly(self, candidate_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        sampled_parts = []
        summary_rows = []

        for (gall_id, year_month), part in candidate_df.groupby(["gall_id", "year_month"], sort=True):
            n_candidates = len(part)
            n_month = min(
                max(self.config.min_monthly_quota, math.ceil(self.config.quota_ratio * n_candidates)),
                self.config.max_monthly_quota
            )
            n_take = min(n_candidates, n_month)

            if self.config.use_score_sort:
                picked = (
                    part.sort_values(
                        by=["score", "comment_count", "recommend", "views", "date", "post_no"],
                        ascending=[False, False, False, False, False, False]
                    )
                    .head(n_take)
                    .copy()
                )
            else:
                picked = part.sample(n=n_take, random_state=42).copy()

            picked["monthly_quota"] = n_month
            picked["monthly_taken"] = n_take

            sampled_parts.append(picked)

            summary_rows.append({
                "gall_id": gall_id,
                "year_month": year_month,
                "candidate_count": n_candidates,
                "monthly_quota": n_month,
                "monthly_taken": n_take,
                "candidate_to_taken_ratio": round(n_take / n_candidates, 4) if n_candidates > 0 else 0.0,
            })

        sampled_df = pd.concat(sampled_parts, ignore_index=True) if sampled_parts else pd.DataFrame()
        summary_df = pd.DataFrame(summary_rows)

        return sampled_df, summary_df

    # =====================================================
    # 저장
    # =====================================================
    def save_outputs(
        self,
        policy_df: pd.DataFrame,
        sampled_df: pd.DataFrame,
        summary_df: pd.DataFrame,
    ) -> None:
        policy_df.to_csv(POLICY_CSV, index=False, encoding="utf-8-sig")

        if not sampled_df.empty:
            sampled_df = sampled_df.sort_values(
                ["gall_id", "year_month", "score", "date", "post_no"],
                ascending=[True, True, False, False, False]
            ).reset_index(drop=True)
            sampled_df.to_csv(TYPE2_POSTS_CSV, index=False, encoding="utf-8-sig")
        else:
            pd.DataFrame().to_csv(TYPE2_POSTS_CSV, index=False, encoding="utf-8-sig")

        if not summary_df.empty:
            summary_df = summary_df.sort_values(["gall_id", "year_month"]).reset_index(drop=True)
            summary_df.to_csv(TYPE2_SUMMARY_CSV, index=False, encoding="utf-8-sig")
        else:
            pd.DataFrame().to_csv(TYPE2_SUMMARY_CSV, index=False, encoding="utf-8-sig")

    # =====================================================
    # 실행
    # =====================================================
    def run(self) -> None:
        print(f"[PAGE COUNT] {self.config.page_count_csv}")
        print(f"[RAW DIR]    {self.config.raw_dir}")
        print(f"[OUT DIR]    {self.config.out_dir}")

        page_df = self.load_page_counts()
        policy_df = self.build_policy_table(page_df)

        posts_df = self.load_posts()
        comment_count_df = self.load_comment_counts()

        candidate_df = self.build_type2_candidates(posts_df, comment_count_df, policy_df)
        sampled_df, summary_df = self.sample_type2_monthly(candidate_df)

        self.save_outputs(policy_df, sampled_df, summary_df)

        print("\n===== POLICY SUMMARY =====")
        print(policy_df["collection_policy"].value_counts(dropna=False).to_string())

        print("\n===== TYPE2 CANDIDATE SUMMARY =====")
        if candidate_df.empty:
            print("후보 없음")
        else:
            tmp = (
                candidate_df.groupby("gall_id", as_index=False)
                .size()
                .rename(columns={"size": "candidate_posts"})
                .sort_values("candidate_posts", ascending=False)
            )
            print(tmp.head(20).to_string(index=False))

        print("\n===== TYPE2 SAMPLED SUMMARY =====")
        if summary_df.empty:
            print("월별 샘플 없음")
        else:
            tmp2 = (
                summary_df.groupby("gall_id", as_index=False)[["candidate_count", "monthly_taken"]]
                .sum()
                .sort_values("monthly_taken", ascending=False)
            )
            print(tmp2.head(20).to_string(index=False))

        print("\n[FINISHED]")
        print(POLICY_CSV)
        print(TYPE2_POSTS_CSV)
        print(TYPE2_SUMMARY_CSV)


def main() -> None:
    config = PolicyConfig()
    builder = GalleryPolicyBuilder(config)
    builder.run()


if __name__ == "__main__":
    main()