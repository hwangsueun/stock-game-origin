# ============================================================
# pr_dci04c_attach_dart_to_event_candidates.py
#
# 입력:
#   data/processed/dci_event_candidates/event_generation_candidates.csv
#   data/processed/dart_event_evidence/dart_event_evidence_daily.csv
#
# 출력:
#   data/processed/dci_event_candidates_final/
#     event_generation_candidates_final.csv
#     event_generation_candidates_for_llm_final.csv
#     event_generation_candidate_final_report.txt
#
# 목적:
#   board + market 후보에 DART factual evidence를 결합
#   factual_news_needed 후보 생성
#
# 원칙:
#   - DART 날짜는 candidate_date보다 미래이면 사용하지 않음
#   - 기본적으로 candidate_date 당일 ~ 직전 3일 공시만 연결
#   - 사실형 뉴스는 DART report_name 범위 안에서만 허용
# ============================================================

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class DartAttachConfig:
    project_root: Path
    candidate_path: Path
    dart_daily_path: Path
    output_dir: Path

    encoding: str = "utf-8-sig"

    dart_window_before_days: int = 3

    min_board_tier: int = 2
    news_min_board_tier: int = 3
    rumor_min_board_tier: int = 3
    rumor_min_unique_author: int = 8


class StockCodeNormalizer:
    @staticmethod
    def normalize(value: object) -> str:
        if pd.isna(value):
            return ""

        s = str(value).strip()

        if re.fullmatch(r"\d+\.0", s):
            s = s[:-2]

        s = s.replace("A", "")
        s = s.replace(".KS", "")
        s = s.replace(".KQ", "")
        s = re.sub(r"\D", "", s)

        if not s:
            return ""

        if len(s) > 6:
            s = s[-6:]

        if len(s) < 6:
            s = s.zfill(6)

        return s


class TextJoiner:
    @staticmethod
    def join_unique(values, limit: int = 20) -> str:
        result = []

        for value in values:
            if pd.isna(value):
                continue

            parts = str(value).split("|")

            for part in parts:
                s = part.strip()

                if not s or s == "nan":
                    continue

                if s not in result:
                    result.append(s)

                if len(result) >= limit:
                    return " | ".join(result)

        return " | ".join(result)


class CandidateLoader:
    def __init__(self, config: DartAttachConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.candidate_path

        if not path.exists():
            raise FileNotFoundError(f"candidate 파일 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)

        if "candidate_date" not in df.columns:
            if "merge_date" in df.columns:
                df["candidate_date"] = df["merge_date"]
            else:
                raise ValueError("candidate_date 또는 merge_date 컬럼이 필요합니다.")

        df["candidate_date"] = pd.to_datetime(df["candidate_date"], errors="coerce").dt.normalize()

        if "candidate_id" not in df.columns:
            df = df.reset_index(drop=True)
            df["candidate_id"] = [f"EVT_{i + 1:06d}" for i in range(len(df))]

        if "stock_code" not in df.columns:
            df["stock_code"] = ""

        if "stock_name" not in df.columns:
            df["stock_name"] = ""

        df["stock_code_norm"] = df["stock_code"].map(StockCodeNormalizer.normalize)
        df["stock_name_norm"] = df["stock_name"].fillna("").astype(str).str.strip()

        numeric_cols = [
            "board_burst_score_tier",
            "comment_count",
            "unique_comment_author",
            "top_comment_author_share",
            "comment_count_ratio_20d",
            "board_activity_score",
            "has_price_shock",
            "has_volume_shock",
            "has_market_evidence",
            "market_residual_z_abs",
            "market_return_z_abs",
            "market_volume_ratio",
            "is_author_concentrated",
        ]

        for col in numeric_cols:
            if col not in df.columns:
                df[col] = 0

            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        print(f"[CandidateLoader] rows: {len(df):,}")
        print(f"[CandidateLoader] date range: {df['candidate_date'].min()} ~ {df['candidate_date'].max()}")

        return df


class DartDailyLoader:
    def __init__(self, config: DartAttachConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.dart_daily_path

        if not path.exists():
            raise FileNotFoundError(f"DART daily 파일 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)

        if "dart_date" not in df.columns:
            raise ValueError("dart_event_evidence_daily.csv에 dart_date 컬럼이 없습니다.")

        df["dart_date"] = pd.to_datetime(df["dart_date"], errors="coerce").dt.normalize()

        if "stock_code" not in df.columns:
            df["stock_code"] = ""

        if "stock_name" not in df.columns:
            df["stock_name"] = ""

        df["stock_code_norm"] = df["stock_code"].map(StockCodeNormalizer.normalize)
        df["stock_name_norm"] = df["stock_name"].fillna("").astype(str).str.strip()

        numeric_cols = [
            "has_dart_event",
            "dart_event_count",
            "dart_max_materiality_score",
        ]

        for col in numeric_cols:
            if col not in df.columns:
                df[col] = 0

            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        text_cols = [
            "dart_event_groups",
            "dart_report_names",
            "dart_rcept_nos",
        ]

        for col in text_cols:
            if col not in df.columns:
                df[col] = ""

            df[col] = df[col].fillna("").astype(str)

        df = df[
            df["dart_date"].notna()
            & (
                df["stock_code_norm"].astype(str).str.len().gt(0)
                | df["stock_name_norm"].astype(str).str.len().gt(0)
            )
        ].copy()

        print(f"[DartDailyLoader] rows: {len(df):,}")
        print(f"[DartDailyLoader] date range: {df['dart_date'].min()} ~ {df['dart_date'].max()}")

        return df


class DartEvidenceAttacher:
    def __init__(self, config: DartAttachConfig):
        self.config = config

    def attach(self, candidates: pd.DataFrame, dart_daily: pd.DataFrame) -> pd.DataFrame:
        date_window = self._build_candidate_dart_date_window(candidates)

        expanded = candidates.merge(
            date_window,
            on=["candidate_id", "candidate_date"],
            how="left",
        )

        by_code = self._merge_by_code(expanded, dart_daily)
        by_name = self._merge_by_name(expanded, dart_daily)

        merged_parts = []

        if not by_code.empty:
            merged_parts.append(by_code)

        if not by_name.empty:
            merged_parts.append(by_name)

        if merged_parts:
            matched = pd.concat(merged_parts, ignore_index=True)
        else:
            matched = pd.DataFrame()

        if not matched.empty:
            matched = matched[
                matched["has_dart_event"].fillna(0).astype(float) > 0
            ].copy()

            matched = matched.drop_duplicates(
                subset=[
                    "candidate_id",
                    "dart_date",
                    "dart_report_names",
                    "dart_rcept_nos",
                    "dart_merge_key_type",
                ],
                keep="first",
            )

        agg = self._aggregate_candidate_dart(matched)

        out = candidates.merge(
            agg,
            on="candidate_id",
            how="left",
        )

        out = self._fill_empty_dart_cols(out)

        print(f"[DartEvidenceAttacher] expanded rows: {len(expanded):,}")
        print(f"[DartEvidenceAttacher] matched rows: {len(matched):,}")
        print(f"[DartEvidenceAttacher] candidates with dart: {int((out['has_dart_event'] == 1).sum()):,}")

        return out

    def _build_candidate_dart_date_window(self, candidates: pd.DataFrame) -> pd.DataFrame:
        rows = []

        base = candidates[["candidate_id", "candidate_date"]].drop_duplicates()

        for _, row in base.iterrows():
            candidate_id = row["candidate_id"]
            candidate_date = pd.to_datetime(row["candidate_date"], errors="coerce")

            if pd.isna(candidate_date):
                continue

            for lag in range(0, self.config.dart_window_before_days + 1):
                dart_date = candidate_date - pd.Timedelta(days=lag)

                if lag == 0:
                    match_type = "exact"
                else:
                    match_type = f"previous_{lag}d"

                rows.append({
                    "candidate_id": candidate_id,
                    "candidate_date": candidate_date,
                    "dart_date": dart_date,
                    "dart_date_lag_days": lag,
                    "dart_date_match_type": match_type,
                })

        return pd.DataFrame(rows)

    @staticmethod
    def _merge_by_code(expanded: pd.DataFrame, dart_daily: pd.DataFrame) -> pd.DataFrame:
        left = expanded[
            expanded["stock_code_norm"].astype(str).str.len().gt(0)
        ].copy()

        right = dart_daily[
            dart_daily["stock_code_norm"].astype(str).str.len().gt(0)
        ].copy()

        if left.empty or right.empty:
            return pd.DataFrame()

        out = left.merge(
            right,
            on=["dart_date", "stock_code_norm"],
            how="left",
            suffixes=("", "_dart"),
        )

        out["dart_merge_key_type"] = "stock_code"

        return out

    @staticmethod
    def _merge_by_name(expanded: pd.DataFrame, dart_daily: pd.DataFrame) -> pd.DataFrame:
        left = expanded[
            expanded["stock_name_norm"].astype(str).str.len().gt(0)
        ].copy()

        right = dart_daily[
            dart_daily["stock_name_norm"].astype(str).str.len().gt(0)
        ].copy()

        if left.empty or right.empty:
            return pd.DataFrame()

        out = left.merge(
            right,
            on=["dart_date", "stock_name_norm"],
            how="left",
            suffixes=("", "_dart"),
        )

        out["dart_merge_key_type"] = "stock_name"

        return out

    def _aggregate_candidate_dart(self, matched: pd.DataFrame) -> pd.DataFrame:
        if matched.empty:
            return pd.DataFrame(columns=[
                "candidate_id",
                "has_dart_event",
                "dart_event_count",
                "dart_max_materiality_score",
                "dart_dates",
                "dart_date_match_types",
                "dart_merge_key_types",
                "dart_event_groups",
                "dart_report_names",
                "dart_rcept_nos",
            ])

        matched = matched.copy()

        matched["has_dart_event"] = (
            pd.to_numeric(matched["has_dart_event"], errors="coerce")
            .fillna(0)
            .astype(int)
        )

        matched = matched[matched["has_dart_event"] == 1].copy()

        grouped = (
            matched.groupby("candidate_id", dropna=False)
            .agg(
                has_dart_event=("has_dart_event", "max"),
                dart_event_count=("dart_event_count", "sum"),
                dart_max_materiality_score=("dart_max_materiality_score", "max"),
                dart_dates=("dart_date", lambda x: TextJoiner.join_unique(
                    pd.to_datetime(x, errors="coerce").dropna().dt.strftime("%Y-%m-%d"),
                    limit=10,
                )),
                dart_date_match_types=("dart_date_match_type", lambda x: TextJoiner.join_unique(x, 10)),
                dart_merge_key_types=("dart_merge_key_type", lambda x: TextJoiner.join_unique(x, 5)),
                dart_event_groups=("dart_event_groups", lambda x: TextJoiner.join_unique(x, 20)),
                dart_report_names=("dart_report_names", lambda x: TextJoiner.join_unique(x, 20)),
                dart_rcept_nos=("dart_rcept_nos", lambda x: TextJoiner.join_unique(x, 20)),
            )
            .reset_index()
        )

        grouped["has_dart_event"] = 1

        return grouped

    @staticmethod
    def _fill_empty_dart_cols(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        numeric_cols = [
            "has_dart_event",
            "dart_event_count",
            "dart_max_materiality_score",
        ]

        for col in numeric_cols:
            if col not in out.columns:
                out[col] = 0

            out[col] = (
                pd.to_numeric(out[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        text_cols = [
            "dart_dates",
            "dart_date_match_types",
            "dart_merge_key_types",
            "dart_event_groups",
            "dart_report_names",
            "dart_rcept_nos",
        ]

        for col in text_cols:
            if col not in out.columns:
                out[col] = ""

            out[col] = out[col].fillna("").astype(str)

        return out


class FinalEventClassifier:
    def __init__(self, config: DartAttachConfig):
        self.config = config

    def classify(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        out["has_factual_evidence"] = (
            out["has_dart_event"].fillna(0).astype(float) > 0
        ).astype(int)

        out["has_market_evidence"] = (
            (out.get("has_market_evidence", 0).astype(float) > 0)
            | (out.get("has_price_shock", 0).astype(float) > 0)
            | (out.get("has_volume_shock", 0).astype(float) > 0)
        ).astype(int)

        out["is_news_level_board_burst"] = (
            out["board_burst_score_tier"].astype(float) >= self.config.news_min_board_tier
        ).astype(int)

        out["is_diverse_board_reaction"] = (
            (out["unique_comment_author"].astype(float) >= self.config.rumor_min_unique_author)
            & (out["board_signal_quality"].fillna("").astype(str) == "usable_diverse")
        ).astype(int)

        out["event_generation_candidate_type"] = out.apply(
            self._classify_row,
            axis=1,
        )

        out["fact_claim_allowed"] = (
            out["event_generation_candidate_type"] == "factual_news_needed"
        ).astype(int)

        out["market_reaction_allowed"] = (
            out["event_generation_candidate_type"].isin(
                ["factual_news_needed", "market_reaction_news"]
            )
        ).astype(int)

        out["rumor_expression_allowed"] = (
            out["event_generation_candidate_type"] == "rumor_or_speculation"
        ).astype(int)

        out["community_reaction_allowed"] = (
            out["event_generation_candidate_type"].isin(
                [
                    "factual_news_needed",
                    "market_reaction_news",
                    "rumor_or_speculation",
                    "community_reaction_only",
                ]
            )
        ).astype(int)

        out["llm_guardrail"] = out["event_generation_candidate_type"].map({
            "factual_news_needed": (
                "DART 공시명과 공시일 범위 안에서만 사실형 뉴스 작성. "
                "공시에 없는 원인, 수치, 계약상대, 실적 규모 생성 금지."
            ),
            "market_reaction_news": (
                "가격/거래량/커뮤니티 반응만 근거로 작성. "
                "구체적 사건 원인 생성 금지."
            ),
            "rumor_or_speculation": (
                "커뮤니티 추측/소문 표현만 허용. "
                "사실형 뉴스 문장 금지."
            ),
            "community_reaction_only": (
                "뉴스 생성 금지. 종토방 반응/분위기 생성만 허용."
            ),
            "discard": "생성 제외.",
        })

        out["factual_basis_text"] = out.apply(
            self._build_factual_basis_text,
            axis=1,
        )

        out["type_reason"] = out.apply(
            self._build_type_reason,
            axis=1,
        )

        return out

    def _classify_row(self, row: pd.Series) -> str:
        board_tier = int(float(row.get("board_burst_score_tier", 0)))
        has_dart = int(float(row.get("has_factual_evidence", 0))) == 1
        has_market = int(float(row.get("has_market_evidence", 0))) == 1
        is_diverse = int(float(row.get("is_diverse_board_reaction", 0))) == 1
        is_author_concentrated = int(float(row.get("is_author_concentrated", 0))) == 1

        # DART evidence가 있으면 사실형 후보
        if has_dart and board_tier >= self.config.min_board_tier:
            return "factual_news_needed"

        if has_market:
            return "market_reaction_news"

        if is_author_concentrated:
            return "discard"

        if board_tier >= self.config.rumor_min_board_tier and is_diverse:
            return "rumor_or_speculation"

        if board_tier >= self.config.min_board_tier:
            return "community_reaction_only"

        return "discard"

    @staticmethod
    def _build_factual_basis_text(row: pd.Series) -> str:
        if int(float(row.get("has_factual_evidence", 0))) != 1:
            return ""

        return (
            f"DART 공시일: {row.get('dart_dates', '')} / "
            f"공시유형: {row.get('dart_event_groups', '')} / "
            f"공시명: {row.get('dart_report_names', '')}"
        )

    @staticmethod
    def _build_type_reason(row: pd.Series) -> str:
        return (
            f"candidate_date={row.get('candidate_date')} | "
            f"stock={row.get('stock_name')}({row.get('stock_code')}) | "
            f"board_level={row.get('board_burst_level')} | "
            f"comments={row.get('comment_count')} | "
            f"authors={row.get('unique_comment_author')} | "
            f"market_evidence={row.get('has_market_evidence')} | "
            f"dart_evidence={row.get('has_factual_evidence')} | "
            f"dart_dates={row.get('dart_dates')} | "
            f"dart_reports={row.get('dart_report_names')}"
        )


class FinalCandidateWriter:
    LLM_COLS = [
        "candidate_id",
        "candidate_date",
        "market_date",
        "market_date_match_type",
        "stock_code",
        "stock_name",
        "event_generation_candidate_type",

        "board_burst_level",
        "board_signal_quality",
        "comment_count",
        "unique_comment_author",
        "top_comment_author_share",
        "comment_count_ratio_20d",
        "board_activity_score",

        "has_price_shock",
        "has_volume_shock",
        "has_market_evidence",
        "market_residual_z_abs",
        "market_return_z_abs",
        "market_volume_ratio",
        "market_return_pct",

        "has_factual_evidence",
        "has_dart_event",
        "dart_event_count",
        "dart_max_materiality_score",
        "dart_dates",
        "dart_event_groups",
        "dart_report_names",
        "dart_rcept_nos",
        "factual_basis_text",

        "fact_claim_allowed",
        "market_reaction_allowed",
        "rumor_expression_allowed",
        "community_reaction_allowed",
        "llm_guardrail",
        "type_reason",
    ]

    def __init__(self, config: DartAttachConfig):
        self.config = config

    def write(self, df: pd.DataFrame) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        full_path = self.config.output_dir / "event_generation_candidates_final.csv"
        llm_path = self.config.output_dir / "event_generation_candidates_for_llm_final.csv"
        report_path = self.config.output_dir / "event_generation_candidate_final_report.txt"

        out = df.copy()

        for col in ["candidate_date", "market_date"]:
            if col in out.columns:
                out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")

        out.to_csv(full_path, index=False, encoding=self.config.encoding)

        llm_cols = [c for c in self.LLM_COLS if c in out.columns]
        llm_df = out[out["event_generation_candidate_type"] != "discard"][llm_cols].copy()
        llm_df.to_csv(llm_path, index=False, encoding=self.config.encoding)

        self._write_report(out, report_path)

        print(f"[SAVE] full: {full_path}")
        print(f"[SAVE] llm: {llm_path}")
        print(f"[SAVE] report: {report_path}")

    def _write_report(self, df: pd.DataFrame, report_path: Path) -> None:
        lines = []

        lines.append("# Final Event Generation Candidate Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- candidate_path: {self.config.candidate_path}")
        lines.append(f"- dart_daily_path: {self.config.dart_daily_path}")
        lines.append("")
        lines.append("## Config")
        lines.append(f"- dart_window_before_days: {self.config.dart_window_before_days}")
        lines.append(f"- min_board_tier: {self.config.min_board_tier}")
        lines.append(f"- news_min_board_tier: {self.config.news_min_board_tier}")
        lines.append("")
        lines.append("## Counts")
        lines.append(f"- total_rows: {len(df):,}")
        lines.append(f"- non_discard_rows: {(df['event_generation_candidate_type'] != 'discard').sum():,}")
        lines.append("")
        lines.append("## By event_generation_candidate_type")

        for k, v in df["event_generation_candidate_type"].value_counts(dropna=False).items():
            lines.append(f"- {k}: {int(v):,}")

        lines.append("")
        lines.append("## Evidence")
        lines.append(f"- has_factual_evidence: {int((df['has_factual_evidence'] == 1).sum()):,}")
        lines.append(f"- has_market_evidence: {int((df['has_market_evidence'] == 1).sum()):,}")
        lines.append(f"- has_price_shock: {int((df['has_price_shock'].astype(float) == 1).sum()):,}")
        lines.append(f"- has_volume_shock: {int((df['has_volume_shock'].astype(float) == 1).sum()):,}")
        lines.append("")
        lines.append("## By DART event group")

        dart_group_rows = df[df["has_factual_evidence"] == 1]["dart_event_groups"].fillna("").astype(str)

        group_counter = {}

        for value in dart_group_rows:
            for part in value.split("|"):
                key = part.strip()
                if not key:
                    continue
                group_counter[key] = group_counter.get(key, 0) + 1

        for k, v in sorted(group_counter.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- {k}: {v:,}")

        lines.append("")
        lines.append("## Top DART report names")

        report_counter = {}

        for value in df[df["has_factual_evidence"] == 1]["dart_report_names"].fillna("").astype(str):
            for part in value.split("|"):
                key = part.strip()
                if not key:
                    continue
                report_counter[key] = report_counter.get(key, 0) + 1

        for k, v in sorted(report_counter.items(), key=lambda x: x[1], reverse=True)[:30]:
            lines.append(f"- {k}: {v:,}")

        lines.append("")
        lines.append("## Suggested Usage")
        lines.append("- factual_news_needed: DART 공시 근거 기반 사실형 뉴스 생성 가능.")
        lines.append("- market_reaction_news: 가격/거래량 반응형 뉴스만 생성. 원인 단정 금지.")
        lines.append("- rumor_or_speculation: 커뮤니티 추측형 문장만 생성. 사실형 뉴스 금지.")
        lines.append("- community_reaction_only: 뉴스 금지. 종토방 분위기만 생성.")
        lines.append("- discard: 생성 제외.")

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))


class DartAttachPipeline:
    def __init__(self, config: DartAttachConfig):
        self.config = config

    def run(self) -> None:
        candidates = CandidateLoader(self.config).load()
        dart_daily = DartDailyLoader(self.config).load()

        attached = DartEvidenceAttacher(self.config).attach(
            candidates=candidates,
            dart_daily=dart_daily,
        )

        final = FinalEventClassifier(self.config).classify(attached)

        FinalCandidateWriter(self.config).write(final)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


def build_config_from_args() -> DartAttachConfig:
    project_root = Path(__file__).resolve().parent.parent

    default_candidate_path = (
        project_root
        / "data"
        / "processed"
        / "dci_event_candidates"
        / "event_generation_candidates.csv"
    )

    default_dart_daily_path = (
        project_root
        / "data"
        / "processed"
        / "dart_event_evidence"
        / "dart_event_evidence_daily.csv"
    )

    default_output_dir = (
        project_root
        / "data"
        / "processed"
        / "dci_event_candidates_final"
    )

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--candidate",
        type=str,
        default=str(default_candidate_path),
    )

    parser.add_argument(
        "--dart-daily",
        type=str,
        default=str(default_dart_daily_path),
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(default_output_dir),
    )

    parser.add_argument("--dart-window-before-days", type=int, default=3)

    args = parser.parse_args()

    return DartAttachConfig(
        project_root=project_root,
        candidate_path=Path(args.candidate).expanduser().resolve(),
        dart_daily_path=Path(args.dart_daily).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        dart_window_before_days=args.dart_window_before_days,
    )


def main() -> None:
    config = build_config_from_args()
    DartAttachPipeline(config).run()


if __name__ == "__main__":
    main()