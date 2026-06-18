import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


# =========================================================
# 설정
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(BASE_DIR, "../data/raw/dci_gallery_page_counts.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "page_volume_from_existing")
os.makedirs(OUTPUT_DIR, exist_ok=True)

MAX_REASONABLE_PAGES = 1_000_000   # 이보다 크면 이상치 후보로 분리
TOP_N_LABEL = 15


@dataclass
class AnalyzeConfig:
    input_csv: str = INPUT_CSV
    output_dir: str = OUTPUT_DIR
    max_reasonable_pages: int = MAX_REASONABLE_PAGES
    top_n_label: int = TOP_N_LABEL


# =========================================================
# 분석기
# =========================================================
class ExistingPageCountAnalyzer:
    def __init__(self, config: AnalyzeConfig):
        self.config = config

    def load_data(self) -> pd.DataFrame:
        if not os.path.exists(self.config.input_csv):
            raise FileNotFoundError(f"파일 없음: {self.config.input_csv}")

        df = pd.read_csv(self.config.input_csv)

        required = {"gallery_name", "gall_id", "gall_type", "total_pages"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"누락 컬럼: {sorted(missing)}")

        df = df.copy()
        df["total_pages"] = pd.to_numeric(df["total_pages"], errors="coerce")
        df = df.dropna(subset=["total_pages"]).copy()
        df["total_pages"] = df["total_pages"].astype(int)

        return df

    def build_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.sort_values("total_pages", ascending=True).reset_index(drop=True).copy()
        df["rank"] = np.arange(1, len(df) + 1)
        df["diff_from_prev"] = df["total_pages"].diff().fillna(0)

        prev = df["total_pages"].shift(1)
        df["ratio_from_prev"] = (df["total_pages"] / prev).replace([np.inf, -np.inf], np.nan)
        df["ratio_from_prev"] = df["ratio_from_prev"].fillna(1.0)

        return df

    def split_outliers(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        normal_df = df[df["total_pages"] <= self.config.max_reasonable_pages].copy()
        outlier_df = df[df["total_pages"] > self.config.max_reasonable_pages].copy()
        return normal_df, outlier_df

    def find_knee_index(self, values: np.ndarray) -> int:
        n = len(values)
        if n < 3:
            return max(0, n - 1)

        x = np.arange(n, dtype=float)
        y = values.astype(float)

        x_norm = (x - x.min()) / (x.max() - x.min() + 1e-9)
        y_norm = (y - y.min()) / (y.max() - y.min() + 1e-9)

        start = np.array([x_norm[0], y_norm[0]])
        end = np.array([x_norm[-1], y_norm[-1]])
        line_vec = end - start
        line_len = np.linalg.norm(line_vec)

        if line_len == 0:
            return n - 1

        dists = []
        for i in range(n):
            point = np.array([x_norm[i], y_norm[i]])
            dist = abs(line_vec[0] * (point[1] - start[1]) - line_vec[1] * (point[0] - start[0])) / line_len
            dists.append(dist)

        return int(np.argmax(dists))

    def save_tables(self, full_df: pd.DataFrame, normal_df: pd.DataFrame, outlier_df: pd.DataFrame) -> pd.DataFrame:
        full_df.to_csv(os.path.join(self.config.output_dir, "01_full_sorted.csv"), index=False, encoding="utf-8-sig")
        normal_df.to_csv(os.path.join(self.config.output_dir, "02_normal_only_sorted.csv"), index=False, encoding="utf-8-sig")
        outlier_df.to_csv(os.path.join(self.config.output_dir, "03_outliers_only.csv"), index=False, encoding="utf-8-sig")

        top20 = full_df.sort_values("total_pages", ascending=False).head(20)
        top20.to_csv(os.path.join(self.config.output_dir, "04_top20_largest.csv"), index=False, encoding="utf-8-sig")

        top10_diff = full_df.sort_values("diff_from_prev", ascending=False).head(10)
        top10_diff.to_csv(os.path.join(self.config.output_dir, "05_top10_diff.csv"), index=False, encoding="utf-8-sig")

        if len(normal_df) > 0:
            knee_idx = self.find_knee_index(normal_df["total_pages"].values)
            knee_row = normal_df.iloc[knee_idx]

            candidates = pd.DataFrame([
                {
                    "candidate_type": "knee_on_normal",
                    "rank": int(knee_row["rank"]),
                    "gallery_name": knee_row["gallery_name"],
                    "gall_id": knee_row["gall_id"],
                    "gall_type": knee_row["gall_type"],
                    "total_pages": int(knee_row["total_pages"]),
                },
                {
                    "candidate_type": "max_diff_on_normal",
                    "rank": int(normal_df.loc[normal_df["diff_from_prev"].idxmax(), "rank"]),
                    "gallery_name": normal_df.loc[normal_df["diff_from_prev"].idxmax(), "gallery_name"],
                    "gall_id": normal_df.loc[normal_df["diff_from_prev"].idxmax(), "gall_id"],
                    "gall_type": normal_df.loc[normal_df["diff_from_prev"].idxmax(), "gall_type"],
                    "total_pages": int(normal_df.loc[normal_df["diff_from_prev"].idxmax(), "total_pages"]),
                },
                {
                    "candidate_type": "max_ratio_on_normal",
                    "rank": int(normal_df.loc[normal_df["ratio_from_prev"].idxmax(), "rank"]),
                    "gallery_name": normal_df.loc[normal_df["ratio_from_prev"].idxmax(), "gallery_name"],
                    "gall_id": normal_df.loc[normal_df["ratio_from_prev"].idxmax(), "gall_id"],
                    "gall_type": normal_df.loc[normal_df["ratio_from_prev"].idxmax(), "gall_type"],
                    "total_pages": int(normal_df.loc[normal_df["ratio_from_prev"].idxmax(), "total_pages"]),
                },
            ])
        else:
            candidates = pd.DataFrame(columns=[
                "candidate_type", "rank", "gallery_name", "gall_id", "gall_type", "total_pages"
            ])

        candidates.to_csv(os.path.join(self.config.output_dir, "06_cut_candidates.csv"), index=False, encoding="utf-8-sig")
        return candidates

    def plot_full(self, df: pd.DataFrame) -> None:
        plt.figure(figsize=(14, 7))
        plt.plot(df["rank"], df["total_pages"], marker="o", linewidth=1)
        plt.xlabel("Sorted Rank")
        plt.ylabel("Total Pages")
        plt.title("Full Distribution of Gallery Total Pages")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "11_full_distribution.png"), dpi=200)
        plt.close()

        plt.figure(figsize=(14, 7))
        plt.plot(df["rank"], df["total_pages"], marker="o", linewidth=1)
        plt.yscale("log")
        plt.xlabel("Sorted Rank")
        plt.ylabel("Total Pages (log scale)")
        plt.title("Full Distribution of Gallery Total Pages - Log Scale")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "12_full_distribution_log.png"), dpi=200)
        plt.close()

    def plot_normal_only(self, normal_df: pd.DataFrame, candidates: pd.DataFrame) -> None:
        if len(normal_df) == 0:
            return

        knee_rank = None
        if len(candidates) > 0:
            knee_row = candidates[candidates["candidate_type"] == "knee_on_normal"]
            if len(knee_row) > 0:
                knee_rank = int(knee_row.iloc[0]["rank"])

        plt.figure(figsize=(14, 7))
        plt.plot(normal_df["rank"], normal_df["total_pages"], marker="o", linewidth=1)
        if knee_rank is not None:
            plt.axvline(knee_rank, linestyle="--")
        plt.xlabel("Sorted Rank")
        plt.ylabel("Total Pages")
        plt.title("Normal Range Distribution (Outliers Removed)")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "13_normal_only_distribution.png"), dpi=200)
        plt.close()

        plt.figure(figsize=(14, 7))
        plt.plot(normal_df["rank"], normal_df["total_pages"], marker="o", linewidth=1)
        if knee_rank is not None:
            plt.axvline(knee_rank, linestyle="--")
        plt.yscale("log")
        plt.xlabel("Sorted Rank")
        plt.ylabel("Total Pages (log scale)")
        plt.title("Normal Range Distribution - Log Scale")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "14_normal_only_distribution_log.png"), dpi=200)
        plt.close()

        plt.figure(figsize=(14, 7))
        plt.plot(normal_df["rank"], normal_df["diff_from_prev"], marker="o", linewidth=1)
        if knee_rank is not None:
            plt.axvline(knee_rank, linestyle="--")
        plt.xlabel("Sorted Rank")
        plt.ylabel("Diff from Previous")
        plt.title("Diff of Total Pages (Outliers Removed)")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "15_normal_only_diff.png"), dpi=200)
        plt.close()

        plt.figure(figsize=(14, 7))
        plt.plot(normal_df["rank"], normal_df["ratio_from_prev"], marker="o", linewidth=1)
        if knee_rank is not None:
            plt.axvline(knee_rank, linestyle="--")
        plt.xlabel("Sorted Rank")
        plt.ylabel("Ratio from Previous")
        plt.title("Ratio of Total Pages (Outliers Removed)")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "16_normal_only_ratio.png"), dpi=200)
        plt.close()

    def annotate_top_galleries(self, df: pd.DataFrame) -> None:
        top_df = df.sort_values("total_pages", ascending=False).head(self.config.top_n_label).copy()
        top_df = top_df.sort_values("rank")

        plt.figure(figsize=(14, 7))
        plt.plot(df["rank"], df["total_pages"], marker="o", linewidth=1)
        plt.yscale("log")

        for _, row in top_df.iterrows():
            plt.annotate(
                row["gallery_name"],
                (row["rank"], row["total_pages"]),
                textcoords="offset points",
                xytext=(4, 4),
                fontsize=8
            )

        plt.xlabel("Sorted Rank")
        plt.ylabel("Total Pages (log scale)")
        plt.title(f"Top {self.config.top_n_label} Largest Galleries Annotated")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "17_top_galleries_annotated.png"), dpi=200)
        plt.close()

    def print_summary(self, full_df: pd.DataFrame, normal_df: pd.DataFrame, outlier_df: pd.DataFrame, candidates: pd.DataFrame) -> None:
        print("\n===== SUMMARY =====")
        print(f"전체 갤러리 수: {len(full_df)}")
        print(f"정상 범위 갤러리 수 (<= {self.config.max_reasonable_pages:,} pages): {len(normal_df)}")
        print(f"이상치 후보 수 (> {self.config.max_reasonable_pages:,} pages): {len(outlier_df)}")

        if len(outlier_df) > 0:
            print("\n[이상치 후보 상위]")
            print(outlier_df.sort_values("total_pages", ascending=False)[
                ["gallery_name", "gall_id", "gall_type", "total_pages"]
            ].head(10).to_string(index=False))

        if len(candidates) > 0:
            print("\n[컷 후보]")
            print(candidates.to_string(index=False))

    def run(self) -> None:
        df = self.load_data()
        full_df = self.build_metrics(df)
        normal_df, outlier_df = self.split_outliers(full_df)

        # normal_df도 다시 rank 계산해서 그래프 보기 쉽게 정리
        normal_df = normal_df.sort_values("total_pages", ascending=True).reset_index(drop=True).copy()
        normal_df["rank"] = np.arange(1, len(normal_df) + 1)
        normal_df["diff_from_prev"] = normal_df["total_pages"].diff().fillna(0)
        prev = normal_df["total_pages"].shift(1)
        normal_df["ratio_from_prev"] = (normal_df["total_pages"] / prev).replace([np.inf, -np.inf], np.nan)
        normal_df["ratio_from_prev"] = normal_df["ratio_from_prev"].fillna(1.0)

        candidates = self.save_tables(full_df, normal_df, outlier_df)
        self.plot_full(full_df)
        self.plot_normal_only(normal_df, candidates)
        self.annotate_top_galleries(full_df)
        self.print_summary(full_df, normal_df, outlier_df, candidates)


# =========================================================
# 실행
# =========================================================
def main():
    config = AnalyzeConfig()
    analyzer = ExistingPageCountAnalyzer(config)
    analyzer.run()

    print("\n[FINISHED]")
    print(f"input:  {config.input_csv}")
    print(f"output: {config.output_dir}")


if __name__ == "__main__":
    main()