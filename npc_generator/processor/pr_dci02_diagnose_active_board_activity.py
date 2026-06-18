# ============================================================
# pr_dci02_diagnose_active_board_activity.py
#
# 입력:
#   data/processed/dci_board_activity/board_activity_daily_features.csv
#
# 출력:
#   data/processed/dci_board_activity_active_diagnostics/
#     active_board_distribution_summary.csv
#     active_board_threshold_probe.csv
#     active_board_top_candidates.csv
#     active_board_sample_candidates.csv
#     active_board_report.txt
#
# 목적:
#   전체 패널이 아니라 active row 기준으로 board activity 임계값 후보 진단
# ============================================================

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd


@dataclass
class ActiveBoardDiagnosticConfig:
    project_root: Path
    input_path: Path
    output_dir: Path
    encoding: str = "utf-8-sig"

    candidate_min_abs_list: List[int] = None
    candidate_score_quantiles: List[float] = None

    def __post_init__(self):
        if self.candidate_min_abs_list is None:
            self.candidate_min_abs_list = [1, 3, 5, 10, 20]

        if self.candidate_score_quantiles is None:
            self.candidate_score_quantiles = [0.50, 0.75, 0.90, 0.95, 0.99]


class ActiveBoardDataLoader:
    def __init__(self, config: ActiveBoardDiagnosticConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        if not self.config.input_path.exists():
            raise FileNotFoundError(f"입력 파일 없음: {self.config.input_path}")

        df = pd.read_csv(
            self.config.input_path,
            dtype={
                "scope": "string",
                "stock_code": "string",
                "stock_name": "string",
            },
            encoding=self.config.encoding,
        )

        df["activity_date"] = pd.to_datetime(df["activity_date"], errors="coerce")

        numeric_cols = [
            "comment_count",
            "comment_thread_count",
            "unique_comment_author",
            "top_comment_author_share",
            "comment_count_ratio_20d",
            "comment_count_z_20d",
            "comment_thread_count_ratio_20d",
            "unique_comment_author_ratio_20d",
            "board_activity_score",
            "post_count",
        ]

        for col in numeric_cols:
            if col not in df.columns:
                df[col] = 0

            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        df = df.dropna(subset=["activity_date"]).copy()

        print(f"[LOAD] rows: {len(df):,}")
        print(f"[LOAD] date range: {df['activity_date'].min()} ~ {df['activity_date'].max()}")
        print(f"[LOAD] scopes: {sorted(df['scope'].dropna().unique().tolist())}")

        return df


class ActiveBoardDistributionAnalyzer:
    METRICS = [
        "comment_count",
        "comment_thread_count",
        "unique_comment_author",
        "top_comment_author_share",
        "comment_count_ratio_20d",
        "comment_count_z_20d",
        "comment_thread_count_ratio_20d",
        "unique_comment_author_ratio_20d",
        "board_activity_score",
    ]

    PERCENTILES = [0.50, 0.75, 0.90, 0.95, 0.975, 0.99]

    FILTERS = {
        "all_rows": lambda df: df,
        "active_comment_gt_0": lambda df: df[df["comment_count"] > 0],
        "active_comment_ge_3": lambda df: df[df["comment_count"] >= 3],
        "active_comment_ge_5": lambda df: df[df["comment_count"] >= 5],
        "active_comment_ge_10": lambda df: df[df["comment_count"] >= 10],
        "active_comment_ge_20": lambda df: df[df["comment_count"] >= 20],
    }

    def __init__(self, config: ActiveBoardDiagnosticConfig):
        self.config = config

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = []

        for scope, scope_df in df.groupby("scope", dropna=False):
            for filter_name, filter_fn in self.FILTERS.items():
                filtered = filter_fn(scope_df)

                if filtered.empty:
                    continue

                for metric in self.METRICS:
                    s = (
                        pd.to_numeric(filtered[metric], errors="coerce")
                        .replace([np.inf, -np.inf], np.nan)
                        .dropna()
                    )

                    if s.empty:
                        continue

                    row = {
                        "scope": scope,
                        "filter": filter_name,
                        "metric": metric,
                        "rows": len(filtered),
                        "nonzero_rows": int((s > 0).sum()),
                        "mean": s.mean(),
                        "std": s.std(),
                        "min": s.min(),
                        "max": s.max(),
                    }

                    for p in self.PERCENTILES:
                        row[f"p{str(p).replace('.', '_')}"] = s.quantile(p)

                    rows.append(row)

        out = pd.DataFrame(rows)

        save_path = self.config.output_dir / "active_board_distribution_summary.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[SAVE] {save_path}")
        return out


class ThresholdProbeBuilder:
    def __init__(self, config: ActiveBoardDiagnosticConfig):
        self.config = config

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        active_df = df[df["comment_count"] > 0].copy()

        rows = []

        for scope, scope_df in active_df.groupby("scope", dropna=False):
            for min_abs in self.config.candidate_min_abs_list:
                base = scope_df[scope_df["comment_count"] >= min_abs].copy()

                if base.empty:
                    continue

                score_values = (
                    base["board_activity_score"]
                    .replace([np.inf, -np.inf], np.nan)
                    .dropna()
                )

                ratio_values = (
                    base["comment_count_ratio_20d"]
                    .replace([np.inf, -np.inf], np.nan)
                    .dropna()
                )

                author_values = (
                    base["unique_comment_author"]
                    .replace([np.inf, -np.inf], np.nan)
                    .dropna()
                )

                for q in self.config.candidate_score_quantiles:
                    score_cut = score_values.quantile(q)
                    ratio_cut = ratio_values.quantile(q)
                    author_cut = author_values.quantile(q)

                    score_hit = base[base["board_activity_score"] >= score_cut]
                    ratio_hit = base[base["comment_count_ratio_20d"] >= ratio_cut]

                    rows.append({
                        "scope": scope,
                        "min_abs_comments": min_abs,
                        "quantile": q,
                        "base_rows": len(base),
                        "score_cut": score_cut,
                        "score_hit_rows": len(score_hit),
                        "score_hit_ratio": len(score_hit) / len(base),
                        "ratio_cut": ratio_cut,
                        "ratio_hit_rows": len(ratio_hit),
                        "ratio_hit_ratio": len(ratio_hit) / len(base),
                        "author_cut": author_cut,
                    })

        out = pd.DataFrame(rows)

        save_path = self.config.output_dir / "active_board_threshold_probe.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[SAVE] {save_path}")
        return out


class CandidateSampler:
    def __init__(self, config: ActiveBoardDiagnosticConfig):
        self.config = config

    def save_top_candidates(self, df: pd.DataFrame) -> pd.DataFrame:
        active = df[df["comment_count"] > 0].copy()

        sort_cols = [
            "board_activity_score",
            "comment_count",
            "unique_comment_author",
            "comment_count_ratio_20d",
        ]

        out = active.sort_values(sort_cols, ascending=[False, False, False, False]).head(2000)

        save_path = self.config.output_dir / "active_board_top_candidates.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[SAVE] {save_path}")
        return out

    def save_sample_candidates(self, df: pd.DataFrame) -> pd.DataFrame:
        active = df[df["comment_count"] > 0].copy()

        samples = []

        sample_rules = [
            ("single_ge5_top_score", "single_only", 5, "board_activity_score"),
            ("single_ge10_top_score", "single_only", 10, "board_activity_score"),
            ("single_ge5_top_ratio", "single_only", 5, "comment_count_ratio_20d"),
            ("all_ge5_top_score", "all", 5, "board_activity_score"),
            ("all_ge10_top_score", "all", 10, "board_activity_score"),
            ("all_ge5_top_ratio", "all", 5, "comment_count_ratio_20d"),
        ]

        for label, scope, min_abs, sort_col in sample_rules:
            sub = active[
                (active["scope"] == scope)
                & (active["comment_count"] >= min_abs)
            ].copy()

            if sub.empty:
                continue

            sub = sub.sort_values(sort_col, ascending=False).head(100)
            sub["sample_rule"] = label
            samples.append(sub)

        if samples:
            out = pd.concat(samples, ignore_index=True)
        else:
            out = pd.DataFrame()

        save_path = self.config.output_dir / "active_board_sample_candidates.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[SAVE] {save_path}")
        return out


class ActiveBoardReportWriter:
    def __init__(self, config: ActiveBoardDiagnosticConfig):
        self.config = config

    def write(
        self,
        df: pd.DataFrame,
        distribution: pd.DataFrame,
        threshold_probe: pd.DataFrame,
    ) -> None:
        path = self.config.output_dir / "active_board_report.txt"

        lines = []
        lines.append("# Active Board Activity Diagnostic Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- input_path: {self.config.input_path}")
        lines.append(f"- total_rows: {len(df):,}")
        lines.append("")

        for scope, g in df.groupby("scope", dropna=False):
            lines.append(f"## Scope: {scope}")
            lines.append(f"- rows: {len(g):,}")
            lines.append(f"- active_comment_gt_0: {(g['comment_count'] > 0).sum():,}")
            lines.append(f"- active_comment_ge_3: {(g['comment_count'] >= 3).sum():,}")
            lines.append(f"- active_comment_ge_5: {(g['comment_count'] >= 5).sum():,}")
            lines.append(f"- active_comment_ge_10: {(g['comment_count'] >= 10).sum():,}")
            lines.append(f"- active_comment_ge_20: {(g['comment_count'] >= 20).sum():,}")
            lines.append("")

            for metric in [
                "comment_count",
                "unique_comment_author",
                "top_comment_author_share",
                "comment_count_ratio_20d",
                "comment_count_z_20d",
                "board_activity_score",
            ]:
                active = g[g["comment_count"] > 0]
                s = (
                    pd.to_numeric(active[metric], errors="coerce")
                    .replace([np.inf, -np.inf], np.nan)
                    .dropna()
                )

                if s.empty:
                    continue

                lines.append(f"### active only / {metric}")
                lines.append(f"- p50: {s.quantile(0.50):.4f}")
                lines.append(f"- p75: {s.quantile(0.75):.4f}")
                lines.append(f"- p90: {s.quantile(0.90):.4f}")
                lines.append(f"- p95: {s.quantile(0.95):.4f}")
                lines.append(f"- p99: {s.quantile(0.99):.4f}")
                lines.append(f"- max: {s.max():.4f}")
                lines.append("")

        lines.append("## Suggested Reading")
        lines.append("- active_comment_gt_0 기준 p90/p95는 전체 패널 왜곡을 제거한 값입니다.")
        lines.append("- active_comment_ge_5 기준 p90/p95는 최소 절대량 게이트를 통과한 burst 후보 분포입니다.")
        lines.append("- single_only 기준은 종목 고유 반응 후보에 더 가깝습니다.")
        lines.append("- all 기준은 multi_stock thread까지 포함하므로 테마/시장 반응이 섞일 수 있습니다.")
        lines.append("")

        with open(path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))

        print(f"[SAVE] {path}")


class ActiveBoardDiagnosticPipeline:
    def __init__(self, config: ActiveBoardDiagnosticConfig):
        self.config = config

    def run(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        df = ActiveBoardDataLoader(self.config).load()

        distribution = ActiveBoardDistributionAnalyzer(self.config).run(df)

        threshold_probe = ThresholdProbeBuilder(self.config).run(df)

        sampler = CandidateSampler(self.config)
        sampler.save_top_candidates(df)
        sampler.save_sample_candidates(df)

        ActiveBoardReportWriter(self.config).write(
            df=df,
            distribution=distribution,
            threshold_probe=threshold_probe,
        )

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


def build_config_from_args() -> ActiveBoardDiagnosticConfig:
    project_root = Path(__file__).resolve().parent.parent

    default_input_path = (
        project_root
        / "data"
        / "processed"
        / "dci_board_activity"
        / "board_activity_daily_features.csv"
    )

    default_output_dir = (
        project_root
        / "data"
        / "processed"
        / "dci_board_activity_active_diagnostics"
    )

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        type=str,
        default=str(default_input_path),
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(default_output_dir),
    )

    args = parser.parse_args()

    return ActiveBoardDiagnosticConfig(
        project_root=project_root,
        input_path=Path(args.input).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
    )


def main() -> None:
    config = build_config_from_args()
    pipeline = ActiveBoardDiagnosticPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()