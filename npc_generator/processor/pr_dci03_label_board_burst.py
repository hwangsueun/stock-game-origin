# ============================================================
# pr_dci03_label_board_burst.py
#
# 입력:
#   data/processed/dci_board_activity/board_activity_daily_features.csv
#
# 출력:
#   data/processed/dci_board_labeled/
#     board_activity_labeled.csv
#     board_burst_candidates.csv
#     board_burst_threshold_report.txt
#
# 목적:
#   댓글 기반 board activity에 대해 burst level / diversity / spam-risk 라벨 생성
#
# 핵심 기준:
#   single_only 우선
#   z-score는 사용하지 않음
# ============================================================

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class BoardBurstLabelConfig:
    project_root: Path
    input_path: Path
    output_dir: Path
    encoding: str = "utf-8-sig"

    # 기본 활동량 게이트
    min_abs_comments: int = 5

    # single_only active 분포 기반
    moderate_min_comments: int = 10
    strong_min_comments: int = 20
    extreme_min_comments: int = 45

    moderate_ratio_cut: float = 2.0
    strong_ratio_cut: float = 3.5
    extreme_ratio_cut: float = 5.1

    moderate_score_cut: float = 1.25
    strong_score_cut: float = 2.8
    extreme_score_cut: float = 4.25

    moderate_author_cut: int = 3
    strong_author_cut: int = 8
    extreme_author_cut: int = 16

    # 도배성 보조 판정
    spam_top_author_share_cut: float = 0.8
    spam_unique_author_max: int = 2


class BoardActivityLoader:
    def __init__(self, config: BoardBurstLabelConfig):
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
            "comment_thread_count_ratio_20d",
            "unique_comment_author_ratio_20d",
            "board_activity_score",
            "post_count",
            "unique_post_author",
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
        print(f"[LOAD] input: {self.config.input_path}")

        return df


class BoardBurstLabeler:
    def __init__(self, config: BoardBurstLabelConfig):
        self.config = config

    def label(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        out["is_active_board"] = (
            out["comment_count"] > 0
        ).astype(int)

        out["min_abs_pass"] = (
            out["comment_count"] >= self.config.min_abs_comments
        ).astype(int)

        out["is_single_scope"] = (
            out["scope"].astype(str) == "single_only"
        ).astype(int)

        out["is_author_concentrated"] = (
            (out["top_comment_author_share"] >= self.config.spam_top_author_share_cut)
            & (out["unique_comment_author"] <= self.config.spam_unique_author_max)
            & (out["comment_count"] >= self.config.min_abs_comments)
        ).astype(int)

        out["author_diversity_level"] = out.apply(
            self._label_author_diversity,
            axis=1,
        )

        out["board_burst_level"] = out.apply(
            self._label_burst_level,
            axis=1,
        )

        out["board_burst_score_tier"] = out["board_burst_level"].map({
            "none": 0,
            "weak": 1,
            "moderate": 2,
            "strong": 3,
            "extreme": 4,
        }).fillna(0).astype(int)

        out["is_board_burst_candidate"] = (
            out["board_burst_score_tier"] >= 2
        ).astype(int)

        out["is_strong_board_burst_candidate"] = (
            out["board_burst_score_tier"] >= 3
        ).astype(int)

        out["board_signal_quality"] = out.apply(
            self._label_signal_quality,
            axis=1,
        )

        return out

    def _label_author_diversity(self, row: pd.Series) -> str:
        unique_author = row["unique_comment_author"]

        if unique_author >= self.config.extreme_author_cut:
            return "high"
        if unique_author >= self.config.strong_author_cut:
            return "medium"
        if unique_author >= self.config.moderate_author_cut:
            return "low"
        if unique_author > 0:
            return "very_low"
        return "none"

    def _label_burst_level(self, row: pd.Series) -> str:
        comment_count = row["comment_count"]
        ratio = row["comment_count_ratio_20d"]
        score = row["board_activity_score"]
        unique_author = row["unique_comment_author"]

        if comment_count < self.config.min_abs_comments:
            return "none"

        # 극단 burst:
        # 절대량도 크고, 자기 기준 증가율 또는 score도 높아야 함
        if (
            comment_count >= self.config.extreme_min_comments
            and ratio >= self.config.extreme_ratio_cut
            and score >= self.config.extreme_score_cut
            and unique_author >= self.config.extreme_author_cut
        ):
            return "extreme"

        # strong burst
        if (
            comment_count >= self.config.strong_min_comments
            and ratio >= self.config.strong_ratio_cut
            and score >= self.config.strong_score_cut
            and unique_author >= self.config.strong_author_cut
        ):
            return "strong"

        # moderate burst
        if (
            comment_count >= self.config.moderate_min_comments
            and ratio >= self.config.moderate_ratio_cut
            and score >= self.config.moderate_score_cut
            and unique_author >= self.config.moderate_author_cut
        ):
            return "moderate"

        # 약한 활동 증가
        if (
            comment_count >= self.config.min_abs_comments
            and (
                ratio >= self.config.moderate_ratio_cut
                or score >= self.config.moderate_score_cut
                or unique_author >= self.config.moderate_author_cut
            )
        ):
            return "weak"

        return "none"

    def _label_signal_quality(self, row: pd.Series) -> str:
        if row["board_burst_level"] == "none":
            return "none"

        if row["is_author_concentrated"] == 1:
            return "low_concentrated"

        if row["author_diversity_level"] in {"medium", "high"}:
            return "usable_diverse"

        if row["author_diversity_level"] == "low":
            return "usable_thin"

        return "weak"


class BoardBurstOutputWriter:
    def __init__(self, config: BoardBurstLabelConfig):
        self.config = config

    def write(self, labeled: pd.DataFrame) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        labeled_path = self.config.output_dir / "board_activity_labeled.csv"
        candidates_path = self.config.output_dir / "board_burst_candidates.csv"
        report_path = self.config.output_dir / "board_burst_threshold_report.txt"

        labeled.to_csv(labeled_path, index=False, encoding=self.config.encoding)

        candidates = labeled[
            labeled["is_board_burst_candidate"] == 1
        ].copy()

        candidates = candidates.sort_values(
            [
                "board_burst_score_tier",
                "board_activity_score",
                "comment_count",
                "unique_comment_author",
                "comment_count_ratio_20d",
            ],
            ascending=[False, False, False, False, False],
        )

        candidates.to_csv(candidates_path, index=False, encoding=self.config.encoding)

        self._write_report(labeled, candidates, report_path)

        print(f"[SAVE] labeled: {labeled_path}")
        print(f"[SAVE] candidates: {candidates_path}")
        print(f"[SAVE] report: {report_path}")

    def _write_report(self, labeled: pd.DataFrame, candidates: pd.DataFrame, report_path: Path) -> None:
        lines = []

        lines.append("# Board Burst Threshold Report")
        lines.append("")
        lines.append("## Thresholds")
        lines.append(f"- min_abs_comments: {self.config.min_abs_comments}")
        lines.append(f"- moderate: comments>={self.config.moderate_min_comments}, ratio>={self.config.moderate_ratio_cut}, score>={self.config.moderate_score_cut}, authors>={self.config.moderate_author_cut}")
        lines.append(f"- strong: comments>={self.config.strong_min_comments}, ratio>={self.config.strong_ratio_cut}, score>={self.config.strong_score_cut}, authors>={self.config.strong_author_cut}")
        lines.append(f"- extreme: comments>={self.config.extreme_min_comments}, ratio>={self.config.extreme_ratio_cut}, score>={self.config.extreme_score_cut}, authors>={self.config.extreme_author_cut}")
        lines.append("")
        lines.append("## Total")
        lines.append(f"- total_rows: {len(labeled):,}")
        lines.append(f"- burst_candidates: {len(candidates):,}")
        lines.append("")

        lines.append("## By scope / burst level")
        scope_level = (
            labeled.groupby(["scope", "board_burst_level"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["scope", "board_burst_level"])
        )
        for _, row in scope_level.iterrows():
            lines.append(f"- {row['scope']} / {row['board_burst_level']}: {int(row['count']):,}")

        lines.append("")
        lines.append("## By scope / signal quality")
        scope_quality = (
            labeled.groupby(["scope", "board_signal_quality"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["scope", "board_signal_quality"])
        )
        for _, row in scope_quality.iterrows():
            lines.append(f"- {row['scope']} / {row['board_signal_quality']}: {int(row['count']):,}")

        lines.append("")
        lines.append("## Candidate summary")
        if not candidates.empty:
            for scope, g in candidates.groupby("scope", dropna=False):
                lines.append(f"### {scope}")
                lines.append(f"- rows: {len(g):,}")
                lines.append(f"- unique_stocks: {g[['stock_code', 'stock_name']].drop_duplicates().shape[0]:,}")
                lines.append(f"- comment_count_p50: {g['comment_count'].quantile(0.50):.4f}")
                lines.append(f"- comment_count_p90: {g['comment_count'].quantile(0.90):.4f}")
                lines.append(f"- ratio_p50: {g['comment_count_ratio_20d'].quantile(0.50):.4f}")
                lines.append(f"- ratio_p90: {g['comment_count_ratio_20d'].quantile(0.90):.4f}")
                lines.append(f"- score_p50: {g['board_activity_score'].quantile(0.50):.4f}")
                lines.append(f"- score_p90: {g['board_activity_score'].quantile(0.90):.4f}")
                lines.append("")
        else:
            lines.append("- no candidates")

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))


class BoardBurstLabelPipeline:
    def __init__(self, config: BoardBurstLabelConfig):
        self.config = config

    def run(self) -> None:
        df = BoardActivityLoader(self.config).load()
        labeled = BoardBurstLabeler(self.config).label(df)
        BoardBurstOutputWriter(self.config).write(labeled)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


def build_config_from_args() -> BoardBurstLabelConfig:
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
        / "dci_board_labeled"
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

    parser.add_argument("--min-abs-comments", type=int, default=5)

    parser.add_argument("--moderate-min-comments", type=int, default=10)
    parser.add_argument("--strong-min-comments", type=int, default=20)
    parser.add_argument("--extreme-min-comments", type=int, default=45)

    parser.add_argument("--moderate-ratio-cut", type=float, default=2.0)
    parser.add_argument("--strong-ratio-cut", type=float, default=3.5)
    parser.add_argument("--extreme-ratio-cut", type=float, default=5.1)

    parser.add_argument("--moderate-score-cut", type=float, default=1.25)
    parser.add_argument("--strong-score-cut", type=float, default=2.8)
    parser.add_argument("--extreme-score-cut", type=float, default=4.25)

    parser.add_argument("--moderate-author-cut", type=int, default=3)
    parser.add_argument("--strong-author-cut", type=int, default=8)
    parser.add_argument("--extreme-author-cut", type=int, default=16)

    args = parser.parse_args()

    return BoardBurstLabelConfig(
        project_root=project_root,
        input_path=Path(args.input).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),

        min_abs_comments=args.min_abs_comments,

        moderate_min_comments=args.moderate_min_comments,
        strong_min_comments=args.strong_min_comments,
        extreme_min_comments=args.extreme_min_comments,

        moderate_ratio_cut=args.moderate_ratio_cut,
        strong_ratio_cut=args.strong_ratio_cut,
        extreme_ratio_cut=args.extreme_ratio_cut,

        moderate_score_cut=args.moderate_score_cut,
        strong_score_cut=args.strong_score_cut,
        extreme_score_cut=args.extreme_score_cut,

        moderate_author_cut=args.moderate_author_cut,
        strong_author_cut=args.strong_author_cut,
        extreme_author_cut=args.extreme_author_cut,
    )


def main() -> None:
    config = build_config_from_args()
    BoardBurstLabelPipeline(config).run()


if __name__ == "__main__":
    main()