import os
from dataclasses import dataclass
from typing import Tuple, Dict

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.mixture import GaussianMixture


# =====================================================
# 경로 설정
# 현재 파일 위치: scripts/gall_division/
# 입력 파일 위치: data/raw/dci_gallery_page_counts.csv
# 출력 폴더 위치: data/gall_division/
# =====================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../scripts/gall_division
SCRIPTS_DIR = os.path.dirname(BASE_DIR)                        # .../scripts
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)                    # .../npc_generator

INPUT_CSV = os.path.join(PROJECT_ROOT, "data", "raw", "dci_gallery_page_counts.csv")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "gall_division")
os.makedirs(OUTPUT_DIR, exist_ok=True)


@dataclass
class AnalysisConfig:
    input_csv: str = INPUT_CSV
    output_dir: str = OUTPUT_DIR
    random_state: int = 42
    gmm_k_candidates: Tuple[int, ...] = (2, 3, 4)
    bootstrap_n: int = 500
    min_positive_pages: int = 1


class GalleryPageStatAnalyzer:
    def __init__(self, config: AnalysisConfig):
        self.config = config

    # =====================================================
    # 1. 데이터 로드
    # =====================================================
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
        df = df[df["total_pages"] >= self.config.min_positive_pages].copy()
        df["total_pages"] = df["total_pages"].astype(int)
        df["log10_pages"] = np.log10(df["total_pages"].astype(float))

        if len(df) < 10:
            raise ValueError("유효한 갤러리 수가 너무 적음")

        return df

    # =====================================================
    # 2. 이상치 판정 (IQR on log scale)
    # =====================================================
    def detect_outliers_iqr(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, float]]:
        x = df["log10_pages"].values

        q1 = np.percentile(x, 25)
        q3 = np.percentile(x, 75)
        iqr = q3 - q1

        mild_upper = q3 + 1.5 * iqr
        extreme_upper = q3 + 3.0 * iqr

        out_df = df.copy()
        out_df["outlier_mild"] = out_df["log10_pages"] > mild_upper
        out_df["outlier_extreme"] = out_df["log10_pages"] > extreme_upper

        stats = {
            "q1": float(q1),
            "q3": float(q3),
            "iqr": float(iqr),
            "mild_upper": float(mild_upper),
            "extreme_upper": float(extreme_upper),
        }
        return out_df, stats

    # =====================================================
    # 3. GMM 적합 및 선택
    # =====================================================
    def fit_gmm_models(self, x: np.ndarray) -> pd.DataFrame:
        rows = []
        x_2d = x.reshape(-1, 1)

        for k in self.config.gmm_k_candidates:
            model = GaussianMixture(
                n_components=k,
                covariance_type="full",
                random_state=self.config.random_state,
                n_init=10,
            )
            model.fit(x_2d)

            rows.append({
                "k": k,
                "bic": float(model.bic(x_2d)),
                "aic": float(model.aic(x_2d)),
                "lower_bound": float(model.lower_bound_),
            })

        return pd.DataFrame(rows).sort_values("bic").reset_index(drop=True)

    def fit_best_gmm(self, x: np.ndarray, k: int) -> GaussianMixture:
        model = GaussianMixture(
            n_components=k,
            covariance_type="full",
            random_state=self.config.random_state,
            n_init=20,
        )
        model.fit(x.reshape(-1, 1))
        return model

    def assign_gmm_clusters(self, df: pd.DataFrame, model: GaussianMixture):
        x = df["log10_pages"].values.reshape(-1, 1)
        raw_labels = model.predict(x)
        probs = model.predict_proba(x)

        means = model.means_.flatten()
        order = np.argsort(means)
        remap = {old: new for new, old in enumerate(order)}

        df2 = df.copy()
        df2["gmm_cluster"] = np.array([remap[l] for l in raw_labels], dtype=int)

        sorted_probs = np.zeros_like(probs)
        for old_idx, new_idx in remap.items():
            sorted_probs[:, new_idx] = probs[:, old_idx]

        for c in range(sorted_probs.shape[1]):
            df2[f"cluster_prob_{c}"] = sorted_probs[:, c]

        cluster_summary = []
        for c in range(len(order)):
            part = df2[df2["gmm_cluster"] == c]
            cluster_summary.append({
                "cluster": c,
                "n": int(len(part)),
                "min_pages": int(part["total_pages"].min()),
                "max_pages": int(part["total_pages"].max()),
                "min_log10": float(part["log10_pages"].min()),
                "max_log10": float(part["log10_pages"].max()),
                "mean_log10": float(part["log10_pages"].mean()),
            })

        return df2, pd.DataFrame(cluster_summary)

    def derive_cluster_boundaries(self, df_clustered: pd.DataFrame) -> pd.DataFrame:
        bounds = []

        clusters = sorted(df_clustered["gmm_cluster"].unique())
        for c1, c2 in zip(clusters[:-1], clusters[1:]):
            left = df_clustered[df_clustered["gmm_cluster"] == c1]["total_pages"]
            right = df_clustered[df_clustered["gmm_cluster"] == c2]["total_pages"]

            bounds.append({
                "from_cluster": int(c1),
                "to_cluster": int(c2),
                "boundary_pages": int(right.min()),
                "left_max_pages": int(left.max()),
                "right_min_pages": int(right.min()),
            })

        return pd.DataFrame(bounds)

    # =====================================================
    # 4. 변화점 탐색 (2개)
    # =====================================================
    def _segment_sse(self, arr: np.ndarray, start: int, end: int) -> float:
        seg = arr[start:end]
        if len(seg) <= 1:
            return 0.0
        mu = np.mean(seg)
        return float(np.sum((seg - mu) ** 2))

    def find_two_changepoints(self, sorted_log: np.ndarray) -> Tuple[int, int, float]:
        n = len(sorted_log)
        best_cost = float("inf")
        best_i, best_j = 1, n - 1

        for i in range(5, n - 10):
            for j in range(i + 5, n - 5):
                cost = (
                    self._segment_sse(sorted_log, 0, i)
                    + self._segment_sse(sorted_log, i, j)
                    + self._segment_sse(sorted_log, j, n)
                )
                if cost < best_cost:
                    best_cost = cost
                    best_i, best_j = i, j

        return best_i, best_j, best_cost

    # =====================================================
    # 5. bootstrap 경계 안정성
    # =====================================================
    def bootstrap_boundaries(self, df: pd.DataFrame, selected_k: int) -> pd.DataFrame:
        rng = np.random.default_rng(self.config.random_state)
        rows = []
        x = df["log10_pages"].values

        for b in range(self.config.bootstrap_n):
            sample_idx = rng.integers(0, len(x), len(x))
            sample_x = x[sample_idx]

            try:
                model = self.fit_best_gmm(sample_x, selected_k)
                means = np.sort(model.means_.flatten())

                for i in range(len(means) - 1):
                    log_boundary = float((means[i] + means[i + 1]) / 2.0)
                    page_boundary = float(10 ** log_boundary)

                    rows.append({
                        "bootstrap_iter": b,
                        "boundary_no": i + 1,
                        "log_boundary": log_boundary,
                        "page_boundary": page_boundary,
                    })
            except Exception:
                continue

        return pd.DataFrame(rows)

    # =====================================================
    # 6. 시각화
    # =====================================================
    def plot_histogram(self, df: pd.DataFrame) -> None:
        plt.figure(figsize=(10, 6))
        plt.hist(df["log10_pages"], bins=20)
        plt.xlabel("log10(total_pages)")
        plt.ylabel("Frequency")
        plt.title("Histogram of log10(total_pages)")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "01_hist_log10_pages.png"), dpi=200)
        plt.close()

    def plot_sorted_distribution(
        self,
        df_sorted: pd.DataFrame,
        gmm_bounds: pd.DataFrame,
        cp1_rank: int,
        cp2_rank: int,
    ) -> None:
        plt.figure(figsize=(14, 7))
        plt.plot(df_sorted["rank"], df_sorted["log10_pages"], marker="o", linewidth=1)

        for _, row in gmm_bounds.iterrows():
            log_b = np.log10(row["boundary_pages"])
            plt.axhline(log_b, linestyle="--")

        plt.axvline(cp1_rank, linestyle=":")
        plt.axvline(cp2_rank, linestyle=":")

        plt.xlabel("Sorted Rank")
        plt.ylabel("log10(total_pages)")
        plt.title("Sorted log10(total_pages) with GMM boundaries and changepoints")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "02_sorted_log10_with_boundaries.png"), dpi=200)
        plt.close()

    def plot_clustered_scatter(self, df_clustered: pd.DataFrame) -> None:
        plt.figure(figsize=(14, 7))
        for c in sorted(df_clustered["gmm_cluster"].unique()):
            part = df_clustered[df_clustered["gmm_cluster"] == c]
            plt.scatter(part["rank"], part["log10_pages"], s=20, label=f"cluster {c}")

        plt.xlabel("Sorted Rank")
        plt.ylabel("log10(total_pages)")
        plt.title("Cluster assignment on sorted galleries")
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "03_cluster_assignment.png"), dpi=200)
        plt.close()

    def plot_bootstrap_boundaries(self, boot_df: pd.DataFrame) -> None:
        if boot_df.empty:
            return

        for boundary_no in sorted(boot_df["boundary_no"].unique()):
            part = boot_df[boot_df["boundary_no"] == boundary_no]

            plt.figure(figsize=(10, 6))
            plt.hist(part["page_boundary"], bins=30)
            plt.xlabel("Boundary pages")
            plt.ylabel("Frequency")
            plt.title(f"Bootstrap distribution of boundary {boundary_no}")
            plt.tight_layout()
            plt.savefig(
                os.path.join(self.config.output_dir, f"04_bootstrap_boundary_{boundary_no}.png"),
                dpi=200
            )
            plt.close()

    def plot_boxplots(self, df: pd.DataFrame) -> None:
        # 1) 전체 log10 박스플롯
        plt.figure(figsize=(8, 6))
        plt.boxplot(df["log10_pages"].values, vert=True)
        plt.ylabel("log10(total_pages)")
        plt.title("Boxplot of log10(total_pages)")
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, "05_boxplot_log10_pages.png"), dpi=200)
        plt.close()

        # 2) 이상치 그룹별 박스플롯
        normal_vals = df.loc[~df["outlier_mild"], "log10_pages"].values if "outlier_mild" in df.columns else np.array([])
        mild_vals = df.loc[df["outlier_mild"], "log10_pages"].values if "outlier_mild" in df.columns else np.array([])

        data = []
        labels = []

        if len(normal_vals) > 0:
            data.append(normal_vals)
            labels.append("non_outlier")

        if len(mild_vals) > 0:
            data.append(mild_vals)
            labels.append("mild_outlier")

        if len(data) > 0:
            plt.figure(figsize=(9, 6))
            plt.boxplot(data, tick_labels=labels)
            plt.ylabel("log10(total_pages)")
            plt.title("Boxplot by Outlier Group")
            plt.tight_layout()
            plt.savefig(os.path.join(self.config.output_dir, "06_boxplot_by_outlier_group.png"), dpi=200)
            plt.close()

        # 3) GMM 군집별 박스플롯
        if "gmm_cluster" in df.columns:
            clusters = sorted(df["gmm_cluster"].dropna().unique())
            cluster_data = [df.loc[df["gmm_cluster"] == c, "log10_pages"].values for c in clusters]

            if len(cluster_data) > 0:
                plt.figure(figsize=(10, 6))
                plt.boxplot(cluster_data, tick_labels=[f"cluster_{int(c)}" for c in clusters])
                plt.ylabel("log10(total_pages)")
                plt.title("Boxplot by GMM Cluster")
                plt.tight_layout()
                plt.savefig(os.path.join(self.config.output_dir, "07_boxplot_by_gmm_cluster.png"), dpi=200)
                plt.close()

    # =====================================================
    # 7. 저장
    # =====================================================
    def save_outputs(
        self,
        df_raw: pd.DataFrame,
        df_out: pd.DataFrame,
        outlier_stats: Dict[str, float],
        gmm_score_df: pd.DataFrame,
        df_clustered: pd.DataFrame,
        cluster_summary_df: pd.DataFrame,
        gmm_bounds_df: pd.DataFrame,
        cp_summary_df: pd.DataFrame,
        boot_df: pd.DataFrame,
        boot_summary_df: pd.DataFrame,
    ) -> None:
        pd.DataFrame([outlier_stats]).to_csv(
            os.path.join(self.config.output_dir, "00_outlier_stats.csv"),
            index=False,
            encoding="utf-8-sig"
        )
        df_raw.to_csv(os.path.join(self.config.output_dir, "01_raw_with_log10.csv"), index=False, encoding="utf-8-sig")
        df_out.to_csv(os.path.join(self.config.output_dir, "02_outlier_flags.csv"), index=False, encoding="utf-8-sig")
        gmm_score_df.to_csv(os.path.join(self.config.output_dir, "03_gmm_bic_aic.csv"), index=False, encoding="utf-8-sig")
        df_clustered.to_csv(os.path.join(self.config.output_dir, "04_clustered_galleries.csv"), index=False, encoding="utf-8-sig")
        cluster_summary_df.to_csv(os.path.join(self.config.output_dir, "05_cluster_summary.csv"), index=False, encoding="utf-8-sig")
        gmm_bounds_df.to_csv(os.path.join(self.config.output_dir, "06_cluster_boundaries.csv"), index=False, encoding="utf-8-sig")
        cp_summary_df.to_csv(os.path.join(self.config.output_dir, "07_changepoints.csv"), index=False, encoding="utf-8-sig")
        boot_df.to_csv(os.path.join(self.config.output_dir, "08_bootstrap_boundaries_raw.csv"), index=False, encoding="utf-8-sig")
        boot_summary_df.to_csv(os.path.join(self.config.output_dir, "09_bootstrap_boundary_summary.csv"), index=False, encoding="utf-8-sig")

    # =====================================================
    # 8. 실행
    # =====================================================
    def run(self) -> None:
        df = self.load_data()
        df_out, outlier_stats = self.detect_outliers_iqr(df)

        df_sorted = df_out.sort_values("total_pages", ascending=True).reset_index(drop=True).copy()
        df_sorted["rank"] = np.arange(1, len(df_sorted) + 1)

        gmm_score_df = self.fit_gmm_models(df_sorted["log10_pages"].values)
        selected_k = int(gmm_score_df.iloc[0]["k"])

        best_gmm = self.fit_best_gmm(df_sorted["log10_pages"].values, selected_k)
        df_clustered, cluster_summary_df = self.assign_gmm_clusters(df_sorted, best_gmm)

        # 이상치 컬럼 유지 확인
        if "outlier_mild" not in df_clustered.columns and "outlier_mild" in df_sorted.columns:
            df_clustered["outlier_mild"] = df_sorted["outlier_mild"].values
        if "outlier_extreme" not in df_clustered.columns and "outlier_extreme" in df_sorted.columns:
            df_clustered["outlier_extreme"] = df_sorted["outlier_extreme"].values

        gmm_bounds_df = self.derive_cluster_boundaries(df_clustered)

        cp1_idx, cp2_idx, cp_cost = self.find_two_changepoints(df_clustered["log10_pages"].values)
        cp1_rank = int(df_clustered.iloc[cp1_idx]["rank"])
        cp2_rank = int(df_clustered.iloc[cp2_idx]["rank"])
        cp1_pages = int(df_clustered.iloc[cp1_idx]["total_pages"])
        cp2_pages = int(df_clustered.iloc[cp2_idx]["total_pages"])

        cp_summary_df = pd.DataFrame([
            {"changepoint_no": 1, "rank": cp1_rank, "total_pages": cp1_pages, "cost": cp_cost},
            {"changepoint_no": 2, "rank": cp2_rank, "total_pages": cp2_pages, "cost": cp_cost},
        ])

        boot_df = self.bootstrap_boundaries(df_clustered, selected_k)

        boot_summary_rows = []
        if not boot_df.empty:
            for boundary_no in sorted(boot_df["boundary_no"].unique()):
                part = boot_df[boot_df["boundary_no"] == boundary_no]["page_boundary"].values
                boot_summary_rows.append({
                    "boundary_no": int(boundary_no),
                    "mean_boundary": float(np.mean(part)),
                    "median_boundary": float(np.median(part)),
                    "p025_boundary": float(np.percentile(part, 2.5)),
                    "p975_boundary": float(np.percentile(part, 97.5)),
                })

        boot_summary_df = pd.DataFrame(boot_summary_rows)

        self.save_outputs(
            df_raw=df_sorted,
            df_out=df_out,
            outlier_stats=outlier_stats,
            gmm_score_df=gmm_score_df,
            df_clustered=df_clustered,
            cluster_summary_df=cluster_summary_df,
            gmm_bounds_df=gmm_bounds_df,
            cp_summary_df=cp_summary_df,
            boot_df=boot_df,
            boot_summary_df=boot_summary_df,
        )

        self.plot_histogram(df_clustered)
        self.plot_sorted_distribution(df_clustered, gmm_bounds_df, cp1_rank, cp2_rank)
        self.plot_clustered_scatter(df_clustered)
        self.plot_bootstrap_boundaries(boot_df)
        self.plot_boxplots(df_clustered)

        print("\n===== SUMMARY =====")
        print(f"갤러리 수: {len(df_clustered)}")

        print("\n[GMM BIC/AIC]")
        print(gmm_score_df.to_string(index=False))
        print(f"\n선택된 군집 수(K): {selected_k}")

        print("\n[Cluster Summary]")
        print(cluster_summary_df.to_string(index=False))

        print("\n[Cluster Boundaries]")
        print(gmm_bounds_df.to_string(index=False))

        print("\n[Outlier Stats on log10(total_pages)]")
        for k, v in outlier_stats.items():
            print(f"{k}: {v:.4f}")

        print("\n[Changepoints]")
        print(cp_summary_df.to_string(index=False))

        if not boot_summary_df.empty:
            print("\n[Bootstrap Boundary Summary]")
            print(boot_summary_df.to_string(index=False))


def main():
    print(f"[INPUT]  {INPUT_CSV}")
    print(f"[OUTPUT] {OUTPUT_DIR}")

    config = AnalysisConfig()
    analyzer = GalleryPageStatAnalyzer(config)
    analyzer.run()

    print("\n[FINISHED]")
    print(f"input:  {config.input_csv}")
    print(f"output: {config.output_dir}")


if __name__ == "__main__":
    main()