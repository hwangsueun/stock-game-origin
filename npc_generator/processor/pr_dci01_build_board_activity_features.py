# ============================================================
# pr_dci01_build_board_activity_features.py
#
# 입력:
#   data/processed/dci_stock_thread_comment_subset/
#     stock_attributed_posts.csv
#     dci_comments_stock_thread_only.csv
#
# 출력:
#   data/processed/dci_board_activity/
#     post_stock_map.csv
#     board_activity_daily_raw.csv
#     board_activity_daily_features.csv
#     board_activity_distribution_summary.csv
#     board_activity_top_bursts.csv
#     board_activity_report.txt
#
# 목적:
#   댓글 기반 date x stock board activity feature 생성
#   - comment_count
#   - unique_comment_author
#   - top_comment_author_share
#   - comment_ratio_20d
#   - comment_z_20d
#   - board_activity_score
#
# 주의:
#   여기서는 event type 분류 임계값 확정 안 함.
#   분포 확인용 feature만 생성.
# ============================================================

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 0. 설정
# ============================================================

@dataclass
class BoardActivityConfig:
    project_root: Path
    input_dir: Path
    output_dir: Path

    stock_posts_filename: str = "stock_attributed_posts.csv"
    comments_filename: str = "dci_comments_stock_thread_only.csv"

    encoding: str = "utf-8-sig"
    comment_chunksize: int = 300_000

    lookback_days: int = 20
    min_periods: int = 3
    smoothing_k: float = 5.0

    # 임계값 확정용이 아니라, top burst 추출용 진단 필터
    diagnostic_min_abs_comments: int = 5


# ============================================================
# 1. 공통 유틸
# ============================================================

class ThreadKeyBuilder:
    @staticmethod
    def normalize_id(value: object) -> str:
        if pd.isna(value):
            return ""

        s = str(value).strip()

        if re.fullmatch(r"\d+\.0", s):
            s = s[:-2]

        return s

    @classmethod
    def make_key(cls, gall_id: object, post_id: object) -> str:
        gall = cls.normalize_id(gall_id)
        post = cls.normalize_id(post_id)
        return f"{gall}__{post}"


class DateNormalizer:
    DATE_PRIORITY = [
        "activity_date",
        "comment_date_final",
        "post_date_final",
        "date",
    ]

    @classmethod
    def pick_date_series(cls, df: pd.DataFrame) -> pd.Series:
        for col in cls.DATE_PRIORITY:
            if col in df.columns:
                s = pd.to_datetime(df[col], errors="coerce")
                if s.notna().sum() > 0:
                    return s.dt.date.astype("string")

        raise ValueError(f"날짜 컬럼을 찾지 못했습니다. 후보: {cls.DATE_PRIORITY}")


class SafeNumeric:
    @staticmethod
    def to_number(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors="coerce").fillna(0)


# ============================================================
# 2. post-stock mapping 생성
# ============================================================

class PostStockMapBuilder:
    def __init__(self, config: BoardActivityConfig):
        self.config = config

    def build(self) -> pd.DataFrame:
        path = self.config.input_dir / self.config.stock_posts_filename

        if not path.exists():
            raise FileNotFoundError(f"stock_attributed_posts.csv 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)

        required = {
            "gall_id",
            "post_id",
            "matched_stock_codes",
            "matched_stock_names",
        }

        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"stock_attributed_posts.csv 필수 컬럼 누락: {missing}")

        if "thread_key" not in df.columns:
            df["thread_key"] = df.apply(
                lambda r: ThreadKeyBuilder.make_key(r.get("gall_id", ""), r.get("post_id", "")),
                axis=1,
            )

        df["post_activity_date"] = DateNormalizer.pick_date_series(df)

        if "multi_stock_thread" not in df.columns:
            df["multi_stock_thread"] = df["matched_stock_names"].fillna("").str.contains(r"\|").astype(int)

        rows = []

        for _, row in df.iterrows():
            codes = self._split_pipe(row.get("matched_stock_codes", ""))
            names = self._split_pipe(row.get("matched_stock_names", ""))

            max_len = max(len(codes), len(names))

            if max_len == 0:
                continue

            for i in range(max_len):
                stock_code = codes[i] if i < len(codes) else ""
                stock_name = names[i] if i < len(names) else ""

                if not stock_code and not stock_name:
                    continue

                rows.append({
                    "thread_key": row.get("thread_key", ""),
                    "gall_id": ThreadKeyBuilder.normalize_id(row.get("gall_id", "")),
                    "post_id": ThreadKeyBuilder.normalize_id(row.get("post_id", "")),
                    "post_activity_date": row.get("post_activity_date", ""),
                    "stock_code": str(stock_code).strip(),
                    "stock_name": str(stock_name).strip(),
                    "multi_stock_thread": int(float(row.get("multi_stock_thread", 0) or 0)),
                    "post_author": str(row.get("author", "")).strip(),
                    "post_title": str(row.get("title", "")).strip(),
                })

        out = pd.DataFrame(rows)

        if out.empty:
            raise ValueError("post_stock_map이 비었습니다. 종목 귀속 결과를 확인해야 합니다.")

        out = out.drop_duplicates(
            subset=["thread_key", "stock_code", "stock_name"]
        ).reset_index(drop=True)

        save_path = self.config.output_dir / "post_stock_map.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[PostStockMapBuilder] post_stock_map rows: {len(out):,}")
        print(f"[PostStockMapBuilder] saved: {save_path}")

        return out

    @staticmethod
    def _split_pipe(value: object) -> List[str]:
        if pd.isna(value):
            return []

        s = str(value).strip()

        if not s:
            return []

        return [x.strip() for x in s.split("|") if x.strip()]


# ============================================================
# 3. 댓글 aggregation
# ============================================================

class CommentActivityAggregator:
    def __init__(self, config: BoardActivityConfig, post_stock_map: pd.DataFrame):
        self.config = config
        self.post_stock_map = post_stock_map.copy()

        self.map_cols = [
            "thread_key",
            "stock_code",
            "stock_name",
            "multi_stock_thread",
        ]

        self.post_stock_map = self.post_stock_map[self.map_cols].drop_duplicates()

    def aggregate(self) -> pd.DataFrame:
        comments_path = self.config.input_dir / self.config.comments_filename

        if not comments_path.exists():
            raise FileNotFoundError(f"댓글 축소 파일 없음: {comments_path}")

        daily_parts = []
        author_parts = []
        thread_parts = []
        gall_parts = []

        total_rows = 0
        merged_rows = 0

        for chunk_idx, chunk in enumerate(pd.read_csv(
            comments_path,
            dtype=str,
            encoding=self.config.encoding,
            chunksize=self.config.comment_chunksize,
            on_bad_lines="skip",
        )):
            t0 = time.time()
            total_rows += len(chunk)

            required = {"gall_id", "post_id"}
            missing = required - set(chunk.columns)

            if missing:
                raise ValueError(f"댓글 파일 필수 컬럼 누락: {missing}")

            if "thread_key" not in chunk.columns:
                chunk["thread_key"] = (
                    chunk["gall_id"].map(ThreadKeyBuilder.normalize_id)
                    + "__"
                    + chunk["post_id"].map(ThreadKeyBuilder.normalize_id)
                )
            else:
                chunk["thread_key"] = chunk["thread_key"].astype(str)

            chunk["activity_date"] = DateNormalizer.pick_date_series(chunk)

            if "author" in chunk.columns:
                chunk["comment_author"] = chunk["author"].fillna("").astype(str).str.strip()
            else:
                chunk["comment_author"] = ""

            if "recommend_count" in chunk.columns:
                chunk["comment_recommend_count"] = SafeNumeric.to_number(chunk["recommend_count"])
            else:
                chunk["comment_recommend_count"] = 0

            merged = chunk.merge(
                self.post_stock_map,
                on="thread_key",
                how="inner",
            )

            merged_rows += len(merged)

            if merged.empty:
                print(
                    f"[CommentAggregator] chunk={chunk_idx:,} "
                    f"rows={len(chunk):,} merged=0 elapsed={time.time() - t0:.1f}s"
                )
                continue

            scoped = self._make_scope_rows(merged)

            daily_parts.append(self._aggregate_daily(scoped))
            author_parts.append(self._aggregate_author(scoped))
            thread_parts.append(self._aggregate_thread(scoped))
            gall_parts.append(self._aggregate_gall(scoped))

            print(
                f"[CommentAggregator] chunk={chunk_idx:,} "
                f"rows={len(chunk):,} "
                f"merged={len(merged):,} "
                f"scoped={len(scoped):,} "
                f"elapsed={time.time() - t0:.1f}s"
            )

        if not daily_parts:
            raise ValueError("댓글 aggregation 결과가 없습니다. thread_key 매칭을 확인해야 합니다.")

        daily = self._combine_daily_parts(daily_parts)
        author = self._combine_author_parts(author_parts)
        thread = self._combine_thread_parts(thread_parts)
        gall = self._combine_gall_parts(gall_parts)

        result = daily.merge(
            author,
            on=["scope", "activity_date", "stock_code", "stock_name"],
            how="left",
        ).merge(
            thread,
            on=["scope", "activity_date", "stock_code", "stock_name"],
            how="left",
        ).merge(
            gall,
            on=["scope", "activity_date", "stock_code", "stock_name"],
            how="left",
        )

        fill_zero_cols = [
            "unique_comment_author",
            "top_comment_author_count",
            "comment_thread_count",
            "active_gall_count",
        ]

        for col in fill_zero_cols:
            if col in result.columns:
                result[col] = result[col].fillna(0)

        result["top_comment_author_share"] = np.where(
            result["comment_count"] > 0,
            result["top_comment_author_count"] / result["comment_count"],
            0,
        )

        print("\n[CommentAggregator DONE]")
        print(f"total_comment_rows_scanned: {total_rows:,}")
        print(f"merged_comment_stock_rows: {merged_rows:,}")
        print(f"daily_activity_rows: {len(result):,}")

        return result

    def _make_scope_rows(self, merged: pd.DataFrame) -> pd.DataFrame:
        all_scope = merged.copy()
        all_scope["scope"] = "all"

        single_scope = merged[merged["multi_stock_thread"].astype(str).isin(["0", "0.0", "False", "false"])].copy()
        single_scope["scope"] = "single_only"

        scoped = pd.concat([all_scope, single_scope], ignore_index=True)

        return scoped

    @staticmethod
    def _aggregate_daily(df: pd.DataFrame) -> pd.DataFrame:
        keys = ["scope", "activity_date", "stock_code", "stock_name"]

        return (
            df.groupby(keys, dropna=False)
            .agg(
                comment_count=("thread_key", "size"),
                comment_recommend_sum=("comment_recommend_count", "sum"),
            )
            .reset_index()
        )

    @staticmethod
    def _aggregate_author(df: pd.DataFrame) -> pd.DataFrame:
        keys = ["scope", "activity_date", "stock_code", "stock_name", "comment_author"]

        author_count = (
            df.groupby(keys, dropna=False)
            .size()
            .reset_index(name="author_comment_count")
        )

        out = (
            author_count.groupby(["scope", "activity_date", "stock_code", "stock_name"], dropna=False)
            .agg(
                unique_comment_author=("comment_author", "nunique"),
                top_comment_author_count=("author_comment_count", "max"),
            )
            .reset_index()
        )

        return out

    @staticmethod
    def _aggregate_thread(df: pd.DataFrame) -> pd.DataFrame:
        keys = ["scope", "activity_date", "stock_code", "stock_name"]

        return (
            df.groupby(keys, dropna=False)
            .agg(comment_thread_count=("thread_key", "nunique"))
            .reset_index()
        )

    @staticmethod
    def _aggregate_gall(df: pd.DataFrame) -> pd.DataFrame:
        keys = ["scope", "activity_date", "stock_code", "stock_name"]

        return (
            df.groupby(keys, dropna=False)
            .agg(active_gall_count=("gall_id", "nunique"))
            .reset_index()
        )

    @staticmethod
    def _combine_daily_parts(parts: List[pd.DataFrame]) -> pd.DataFrame:
        df = pd.concat(parts, ignore_index=True)

        return (
            df.groupby(["scope", "activity_date", "stock_code", "stock_name"], dropna=False)
            .agg(
                comment_count=("comment_count", "sum"),
                comment_recommend_sum=("comment_recommend_sum", "sum"),
            )
            .reset_index()
        )

    @staticmethod
    def _combine_author_parts(parts: List[pd.DataFrame]) -> pd.DataFrame:
        df = pd.concat(parts, ignore_index=True)

        return (
            df.groupby(["scope", "activity_date", "stock_code", "stock_name"], dropna=False)
            .agg(
                unique_comment_author=("unique_comment_author", "max"),
                top_comment_author_count=("top_comment_author_count", "max"),
            )
            .reset_index()
        )

    @staticmethod
    def _combine_thread_parts(parts: List[pd.DataFrame]) -> pd.DataFrame:
        df = pd.concat(parts, ignore_index=True)

        return (
            df.groupby(["scope", "activity_date", "stock_code", "stock_name"], dropna=False)
            .agg(comment_thread_count=("comment_thread_count", "max"))
            .reset_index()
        )

    @staticmethod
    def _combine_gall_parts(parts: List[pd.DataFrame]) -> pd.DataFrame:
        df = pd.concat(parts, ignore_index=True)

        return (
            df.groupby(["scope", "activity_date", "stock_code", "stock_name"], dropna=False)
            .agg(active_gall_count=("active_gall_count", "max"))
            .reset_index()
        )


# ============================================================
# 4. 게시글 aggregation
# ============================================================

class PostActivityAggregator:
    def __init__(self, post_stock_map: pd.DataFrame):
        self.post_stock_map = post_stock_map.copy()

    def aggregate(self) -> pd.DataFrame:
        df = self.post_stock_map.copy()
        df = df.rename(columns={"post_activity_date": "activity_date"})

        all_scope = df.copy()
        all_scope["scope"] = "all"

        single_scope = df[df["multi_stock_thread"].astype(str).isin(["0", "0.0", "False", "false"])].copy()
        single_scope["scope"] = "single_only"

        scoped = pd.concat([all_scope, single_scope], ignore_index=True)

        keys = ["scope", "activity_date", "stock_code", "stock_name"]

        out = (
            scoped.groupby(keys, dropna=False)
            .agg(
                post_count=("thread_key", "nunique"),
                unique_post_author=("post_author", "nunique"),
            )
            .reset_index()
        )

        return out


# ============================================================
# 5. daily raw 병합
# ============================================================

class DailyActivityMerger:
    def __init__(self, config: BoardActivityConfig):
        self.config = config

    def merge(self, comment_daily: pd.DataFrame, post_daily: pd.DataFrame) -> pd.DataFrame:
        keys = ["scope", "activity_date", "stock_code", "stock_name"]

        out = comment_daily.merge(
            post_daily,
            on=keys,
            how="outer",
        )

        numeric_cols = [
            "comment_count",
            "comment_recommend_sum",
            "unique_comment_author",
            "top_comment_author_count",
            "comment_thread_count",
            "active_gall_count",
            "top_comment_author_share",
            "post_count",
            "unique_post_author",
        ]

        for col in numeric_cols:
            if col not in out.columns:
                out[col] = 0
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)

        out["activity_date"] = pd.to_datetime(out["activity_date"], errors="coerce")

        out = out.dropna(subset=["activity_date"]).copy()
        out = out.sort_values(["scope", "stock_code", "stock_name", "activity_date"]).reset_index(drop=True)

        save_path = self.config.output_dir / "board_activity_daily_raw.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[DailyActivityMerger] raw rows: {len(out):,}")
        print(f"[DailyActivityMerger] saved: {save_path}")

        return out


# ============================================================
# 6. rolling feature 생성
# ============================================================

class RollingFeatureBuilder:
    BASE_METRICS = [
        "comment_count",
        "comment_thread_count",
        "unique_comment_author",
        "post_count",
        "comment_recommend_sum",
    ]

    def __init__(self, config: BoardActivityConfig):
        self.config = config

    def build(self, daily_raw: pd.DataFrame) -> pd.DataFrame:
        df = daily_raw.copy()

        df["activity_date"] = pd.to_datetime(df["activity_date"], errors="coerce")
        df = df.dropna(subset=["activity_date"]).copy()

        min_date = df["activity_date"].min()
        max_date = df["activity_date"].max()

        full_dates = pd.date_range(min_date, max_date, freq="D")

        panels = []

        group_cols = ["scope", "stock_code", "stock_name"]

        for group_key, g in df.groupby(group_cols, dropna=False):
            scope, stock_code, stock_name = group_key

            base = pd.DataFrame({"activity_date": full_dates})
            base["scope"] = scope
            base["stock_code"] = stock_code
            base["stock_name"] = stock_name

            merged = base.merge(
                g,
                on=["scope", "stock_code", "stock_name", "activity_date"],
                how="left",
            )

            fill_cols = [
                "comment_count",
                "comment_recommend_sum",
                "unique_comment_author",
                "top_comment_author_count",
                "comment_thread_count",
                "active_gall_count",
                "top_comment_author_share",
                "post_count",
                "unique_post_author",
            ]

            for col in fill_cols:
                if col not in merged.columns:
                    merged[col] = 0
                merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0)

            merged = self._add_rolling_features(merged)

            panels.append(merged)

        out = pd.concat(panels, ignore_index=True)

        out["board_activity_score"] = self._compute_board_activity_score(out)

        out["diagnostic_comment_min_abs_pass"] = (
            out["comment_count"] >= self.config.diagnostic_min_abs_comments
        ).astype(int)

        out = out.sort_values(["scope", "stock_code", "stock_name", "activity_date"]).reset_index(drop=True)

        save_path = self.config.output_dir / "board_activity_daily_features.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[RollingFeatureBuilder] feature rows: {len(out):,}")
        print(f"[RollingFeatureBuilder] saved: {save_path}")

        return out

    def _add_rolling_features(self, g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("activity_date").copy()

        for metric in self.BASE_METRICS:
            s = pd.to_numeric(g[metric], errors="coerce").fillna(0)

            prev_mean = (
                s.shift(1)
                .rolling(
                    window=self.config.lookback_days,
                    min_periods=self.config.min_periods,
                )
                .mean()
            )

            prev_std = (
                s.shift(1)
                .rolling(
                    window=self.config.lookback_days,
                    min_periods=self.config.min_periods,
                )
                .std()
            )

            prev_mean_filled = prev_mean.fillna(0)
            prev_std_safe = prev_std.replace(0, np.nan)

            ratio = (s + self.config.smoothing_k) / (prev_mean_filled + self.config.smoothing_k)
            z = ((s - prev_mean_filled) / prev_std_safe).replace([np.inf, -np.inf], np.nan).fillna(0)

            g[f"{metric}_prev{self.config.lookback_days}_mean"] = prev_mean
            g[f"{metric}_ratio_{self.config.lookback_days}d"] = ratio
            g[f"{metric}_z_{self.config.lookback_days}d"] = z

        return g

    @staticmethod
    def _compute_board_activity_score(df: pd.DataFrame) -> pd.Series:
        comment_ratio = df["comment_count_ratio_20d"].replace([np.inf, -np.inf], np.nan).fillna(1)
        thread_ratio = df["comment_thread_count_ratio_20d"].replace([np.inf, -np.inf], np.nan).fillna(1)
        author_ratio = df["unique_comment_author_ratio_20d"].replace([np.inf, -np.inf], np.nan).fillna(1)

        comment_component = np.log1p(np.maximum(comment_ratio - 1, 0)) * np.log1p(df["comment_count"])
        thread_component = np.log1p(np.maximum(thread_ratio - 1, 0)) * np.log1p(df["comment_thread_count"])
        author_component = np.log1p(np.maximum(author_ratio - 1, 0)) * np.log1p(df["unique_comment_author"])

        score = (
            0.60 * comment_component
            + 0.25 * thread_component
            + 0.15 * author_component
        )

        return score.replace([np.inf, -np.inf], np.nan).fillna(0)


# ============================================================
# 7. 분포 요약 / top burst
# ============================================================

class BoardActivityDiagnostics:
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

    PERCENTILES = [0.5, 0.75, 0.9, 0.95, 0.99]

    def __init__(self, config: BoardActivityConfig):
        self.config = config

    def run(self, features: pd.DataFrame) -> None:
        self._save_distribution_summary(features)
        self._save_top_bursts(features)
        self._save_report(features)

    def _save_distribution_summary(self, features: pd.DataFrame) -> None:
        rows = []

        for scope, g in features.groupby("scope"):
            for metric in self.METRICS:
                if metric not in g.columns:
                    continue

                s = pd.to_numeric(g[metric], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()

                if s.empty:
                    continue

                row = {
                    "scope": scope,
                    "metric": metric,
                    "count": len(s),
                    "mean": s.mean(),
                    "std": s.std(),
                    "min": s.min(),
                    "max": s.max(),
                }

                for p in self.PERCENTILES:
                    row[f"p{int(p * 100)}"] = s.quantile(p)

                rows.append(row)

        out = pd.DataFrame(rows)

        save_path = self.config.output_dir / "board_activity_distribution_summary.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[Diagnostics] distribution saved: {save_path}")

    def _save_top_bursts(self, features: pd.DataFrame) -> None:
        out = features[
            features["comment_count"] >= self.config.diagnostic_min_abs_comments
        ].copy()

        out = out.sort_values(
            ["board_activity_score", "comment_count", "unique_comment_author"],
            ascending=[False, False, False],
        )

        out = out.head(1000)

        save_path = self.config.output_dir / "board_activity_top_bursts.csv"
        out.to_csv(save_path, index=False, encoding=self.config.encoding)

        print(f"[Diagnostics] top bursts saved: {save_path}")

    def _save_report(self, features: pd.DataFrame) -> None:
        report_path = self.config.output_dir / "board_activity_report.txt"

        lines = []
        lines.append("# Board Activity Feature Report")
        lines.append("")
        lines.append("## Config")
        lines.append(f"- lookback_days: {self.config.lookback_days}")
        lines.append(f"- min_periods: {self.config.min_periods}")
        lines.append(f"- smoothing_k: {self.config.smoothing_k}")
        lines.append(f"- diagnostic_min_abs_comments: {self.config.diagnostic_min_abs_comments}")
        lines.append("")
        lines.append("## Rows")
        lines.append(f"- total_feature_rows: {len(features):,}")
        lines.append("")

        for scope, g in features.groupby("scope"):
            lines.append(f"## Scope: {scope}")
            lines.append(f"- rows: {len(g):,}")
            lines.append(f"- active_rows_comment_count_gt_0: {(g['comment_count'] > 0).sum():,}")
            lines.append(f"- active_rows_min_abs_pass: {(g['comment_count'] >= self.config.diagnostic_min_abs_comments).sum():,}")
            lines.append(f"- unique_stocks: {g[['stock_code', 'stock_name']].drop_duplicates().shape[0]:,}")
            lines.append("")

            for metric in [
                "comment_count",
                "unique_comment_author",
                "top_comment_author_share",
                "comment_count_ratio_20d",
                "board_activity_score",
            ]:
                s = pd.to_numeric(g[metric], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()

                if s.empty:
                    continue

                lines.append(f"### {metric}")
                lines.append(f"- p50: {s.quantile(0.50):.4f}")
                lines.append(f"- p90: {s.quantile(0.90):.4f}")
                lines.append(f"- p95: {s.quantile(0.95):.4f}")
                lines.append(f"- p99: {s.quantile(0.99):.4f}")
                lines.append("")

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))

        print(f"[Diagnostics] report saved: {report_path}")


# ============================================================
# 8. 파이프라인
# ============================================================

class BoardActivityPipeline:
    def __init__(self, config: BoardActivityConfig):
        self.config = config

    def run(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._validate_inputs()

        post_stock_map = PostStockMapBuilder(self.config).build()

        comment_daily = CommentActivityAggregator(
            config=self.config,
            post_stock_map=post_stock_map,
        ).aggregate()

        post_daily = PostActivityAggregator(post_stock_map).aggregate()

        daily_raw = DailyActivityMerger(self.config).merge(
            comment_daily=comment_daily,
            post_daily=post_daily,
        )

        features = RollingFeatureBuilder(self.config).build(daily_raw)

        BoardActivityDiagnostics(self.config).run(features)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")

    def _validate_inputs(self) -> None:
        stock_posts_path = self.config.input_dir / self.config.stock_posts_filename
        comments_path = self.config.input_dir / self.config.comments_filename

        print("[Input Check]")

        for label, path in {
            "stock_posts_path": stock_posts_path,
            "comments_path": comments_path,
        }.items():
            if not path.exists():
                raise FileNotFoundError(f"{label} 없음: {path}")

            size_mb = path.stat().st_size / 1024 / 1024
            print(f"  {label}: {path} ({size_mb:,.1f} MB)")


# ============================================================
# 9. CLI
# ============================================================

def build_config_from_args() -> BoardActivityConfig:
    project_root = Path(__file__).resolve().parent.parent

    default_input_dir = project_root / "data" / "processed" / "dci_stock_thread_comment_subset"
    default_output_dir = project_root / "data" / "processed" / "dci_board_activity"

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input-dir",
        type=str,
        default=str(default_input_dir),
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(default_output_dir),
    )

    parser.add_argument(
        "--comment-chunksize",
        type=int,
        default=300_000,
    )

    parser.add_argument(
        "--lookback-days",
        type=int,
        default=20,
    )

    parser.add_argument(
        "--min-periods",
        type=int,
        default=3,
    )

    parser.add_argument(
        "--smoothing-k",
        type=float,
        default=5.0,
    )

    parser.add_argument(
        "--diagnostic-min-abs-comments",
        type=int,
        default=5,
    )

    args = parser.parse_args()

    return BoardActivityConfig(
        project_root=project_root,
        input_dir=Path(args.input_dir).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        comment_chunksize=args.comment_chunksize,
        lookback_days=args.lookback_days,
        min_periods=args.min_periods,
        smoothing_k=args.smoothing_k,
        diagnostic_min_abs_comments=args.diagnostic_min_abs_comments,
    )


def main() -> None:
    config = build_config_from_args()
    pipeline = BoardActivityPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()