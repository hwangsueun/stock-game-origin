# ============================================================
# pr_dci04_build_event_generation_candidates.py
#
# 목적:
#   board burst + 가격/거래량 shock 결합
#   event_generation_candidate_type 분류
#
# 개선점:
#   - board 날짜와 시장 거래일이 정확히 같지 않아도 매칭
#   - candidate_date 기준:
#       exact
#       previous_trading_day
#       next_trading_day
#     중 shock가 있는 거래일을 우선 선택
# ============================================================

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List

import numpy as np
import pandas as pd


@dataclass
class EventCandidateConfig:
    project_root: Path
    board_labeled_path: Path
    market_features_path: Optional[Path]
    output_dir: Path

    encoding: str = "utf-8-sig"

    use_scope: str = "single_only"
    min_board_tier: int = 2
    news_min_board_tier: int = 3

    residual_z_cut: float = 2.5
    return_z_cut: float = 2.5
    volume_ratio_cut: float = 2.5

    rumor_min_board_tier: int = 3
    rumor_min_unique_author: int = 8

    discard_low_concentrated_without_market: bool = True


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


class DateNormalizer:
    DATE_CANDIDATES = [
        "merge_date",
        "activity_date",
        "market_context_date",
        "date",
        "trade_date",
        "base_date",
        "일자",
        "날짜",
    ]

    @classmethod
    def normalize_date_column(cls, df: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
        for col in cls.DATE_CANDIDATES:
            if col in df.columns:
                parsed = pd.to_datetime(df[col], errors="coerce")
                if parsed.notna().sum() > 0:
                    out = df.copy()
                    out["candidate_date"] = parsed.dt.normalize()
                    return out, col

        raise ValueError(f"날짜 컬럼을 찾지 못했습니다. 후보: {cls.DATE_CANDIDATES}")


class BoardLabeledLoader:
    def __init__(self, config: EventCandidateConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.board_labeled_path

        if not path.exists():
            raise FileNotFoundError(f"board_activity_labeled.csv 없음: {path}")

        df = pd.read_csv(
            path,
            dtype={
                "scope": "string",
                "stock_code": "string",
                "stock_name": "string",
            },
            encoding=self.config.encoding,
        )

        df, used_date_col = DateNormalizer.normalize_date_column(df)

        required = {
            "scope",
            "stock_code",
            "stock_name",
            "board_burst_score_tier",
            "board_burst_level",
            "board_signal_quality",
            "comment_count",
            "unique_comment_author",
            "comment_count_ratio_20d",
            "board_activity_score",
        }

        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"board labeled 필수 컬럼 누락: {missing}")

        df["stock_code_norm"] = df["stock_code"].map(StockCodeNormalizer.normalize)
        df["stock_name_norm"] = df["stock_name"].fillna("").astype(str).str.strip()

        numeric_cols = [
            "board_burst_score_tier",
            "comment_count",
            "unique_comment_author",
            "top_comment_author_share",
            "comment_count_ratio_20d",
            "board_activity_score",
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

        df = df[
            (df["scope"].astype(str) == self.config.use_scope)
            & (df["board_burst_score_tier"] >= self.config.min_board_tier)
        ].copy()

        df = df.reset_index(drop=True)
        df["candidate_id"] = [
            f"EVT_{i + 1:06d}" for i in range(len(df))
        ]

        print(f"[BoardLabeledLoader] used_date_col: {used_date_col}")
        print(f"[BoardLabeledLoader] board candidates: {len(df):,}")

        return df


class MarketFeatureLoader:
    def __init__(self, config: EventCandidateConfig):
        self.config = config

    def load(self) -> Optional[pd.DataFrame]:
        path = self.config.market_features_path

        if path is None:
            print("[MarketFeatureLoader] board-only mode")
            return None

        if not path.exists():
            raise FileNotFoundError(f"market feature 파일 없음: {path}")

        df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)

        if "date" not in df.columns:
            raise ValueError("market_shock_features.csv에 date 컬럼이 없습니다.")

        df["market_date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()

        if "stock_code" not in df.columns:
            raise ValueError("market_shock_features.csv에 stock_code 컬럼이 없습니다.")

        df["stock_code_norm"] = df["stock_code"].map(StockCodeNormalizer.normalize)

        if "stock_name" in df.columns:
            df["stock_name_market"] = df["stock_name"].fillna("").astype(str).str.strip()
            df["stock_name_norm"] = df["stock_name"].fillna("").astype(str).str.strip()
        else:
            df["stock_name_market"] = ""
            df["stock_name_norm"] = ""

        numeric_cols = [
            "residual_z_abs",
            "return_z_abs",
            "volume_ratio",
            "return_pct",
            "residual_return",
            "has_price_shock",
            "has_volume_shock",
            "has_market_shock",
        ]

        for col in numeric_cols:
            if col not in df.columns:
                df[col] = 0

            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        df["market_residual_z_abs"] = df["residual_z_abs"]
        df["market_return_z_abs"] = df["return_z_abs"]
        df["market_volume_ratio"] = df["volume_ratio"]
        df["market_return_pct"] = df["return_pct"]
        df["market_residual_return"] = df["residual_return"]

        keep_cols = [
            "market_date",
            "stock_code_norm",
            "stock_name_norm",
            "stock_name_market",
            "market_residual_z_abs",
            "market_return_z_abs",
            "market_volume_ratio",
            "market_return_pct",
            "market_residual_return",
            "has_price_shock",
            "has_volume_shock",
            "has_market_shock",
        ]

        df = df[keep_cols].drop_duplicates(
            subset=["market_date", "stock_code_norm"],
            keep="last",
        )

        print(f"[MarketFeatureLoader] rows: {len(df):,}")
        print(f"[MarketFeatureLoader] market dates: {df['market_date'].min()} ~ {df['market_date'].max()}")

        return df


class MarketCalendarMapper:
    def __init__(self, market_df: pd.DataFrame):
        dates = (
            market_df["market_date"]
            .dropna()
            .drop_duplicates()
            .sort_values()
            .tolist()
        )

        self.trading_dates = pd.DatetimeIndex(dates)

    def build_candidate_date_table(self, board_df: pd.DataFrame) -> pd.DataFrame:
        base = board_df[["candidate_id", "candidate_date"]].drop_duplicates().copy()

        rows = []

        for _, row in base.iterrows():
            candidate_id = row["candidate_id"]
            candidate_date = pd.to_datetime(row["candidate_date"], errors="coerce")

            if pd.isna(candidate_date):
                continue

            exact = self._exact(candidate_date)
            prev_day = self._previous(candidate_date)
            next_day = self._next(candidate_date)

            for match_type, market_date in [
                ("exact", exact),
                ("previous_trading_day", prev_day),
                ("next_trading_day", next_day),
            ]:
                if pd.isna(market_date):
                    continue

                rows.append({
                    "candidate_id": candidate_id,
                    "candidate_date": candidate_date,
                    "market_date": market_date,
                    "market_date_match_type": match_type,
                })

        out = pd.DataFrame(rows)

        if out.empty:
            raise ValueError("candidate_date를 market_date로 매핑하지 못했습니다.")

        return out

    def _exact(self, date_value: pd.Timestamp) -> pd.Timestamp:
        if date_value in self.trading_dates:
            return date_value
        return pd.NaT

    def _previous(self, date_value: pd.Timestamp) -> pd.Timestamp:
        idx = self.trading_dates.searchsorted(date_value, side="right") - 1
        if idx < 0:
            return pd.NaT
        return self.trading_dates[idx]

    def _next(self, date_value: pd.Timestamp) -> pd.Timestamp:
        idx = self.trading_dates.searchsorted(date_value, side="left")
        if idx >= len(self.trading_dates):
            return pd.NaT
        return self.trading_dates[idx]


class EvidenceMerger:
    def __init__(self, config: EventCandidateConfig):
        self.config = config

    def merge(self, board_df: pd.DataFrame, market_df: Optional[pd.DataFrame]) -> pd.DataFrame:
        if market_df is None:
            return self._add_empty_market_cols(board_df)

        calendar_mapper = MarketCalendarMapper(market_df)
        date_table = calendar_mapper.build_candidate_date_table(board_df)

        expanded = board_df.merge(
            date_table,
            on=["candidate_id", "candidate_date"],
            how="left",
        )

        # ========================================================
        # 1차: stock_code 기준 merge
        # ========================================================
        code_left = expanded[expanded["stock_code_norm"].astype(str).str.len() > 0].copy()

        if not code_left.empty:
            by_code = code_left.merge(
                market_df[market_df["stock_code_norm"].astype(str).str.len() > 0],
                on=["market_date", "stock_code_norm"],
                how="left",
                suffixes=("", "_market"),
            )
            by_code["market_merge_key_type"] = "stock_code"
        else:
            by_code = pd.DataFrame()

        # ========================================================
        # 2차: stock_name 기준 merge
        #   board 쪽 stock_code가 비어 있는 경우를 처리
        # ========================================================
        name_left = expanded.copy()

        if not by_code.empty:
            matched_ids = set(
                by_code.loc[
                    by_code["has_market_shock"].notna(),
                    ["candidate_id", "market_date"]
                ].apply(lambda r: f"{r['candidate_id']}__{r['market_date']}", axis=1)
            )

            name_left["_tmp_key"] = name_left.apply(
                lambda r: f"{r['candidate_id']}__{r['market_date']}",
                axis=1,
            )

            name_left = name_left[~name_left["_tmp_key"].isin(matched_ids)].copy()
            name_left = name_left.drop(columns=["_tmp_key"])

        name_left = name_left[name_left["stock_name_norm"].astype(str).str.len() > 0].copy()

        if not name_left.empty:
            by_name = name_left.merge(
                market_df[market_df["stock_name_norm"].astype(str).str.len() > 0],
                on=["market_date", "stock_name_norm"],
                how="left",
                suffixes=("", "_market"),
            )
            by_name["market_merge_key_type"] = "stock_name"
        else:
            by_name = pd.DataFrame()

        merged_parts = []

        if not by_code.empty:
            merged_parts.append(by_code)

        if not by_name.empty:
            merged_parts.append(by_name)

        if merged_parts:
            merged = pd.concat(merged_parts, ignore_index=True)
        else:
            merged = expanded.copy()
            merged["market_merge_key_type"] = "none"

        merged = self._fill_market_cols(merged)

        # ========================================================
        # candidate별 best market row 선택
        # shock 있는 행 우선
        # exact > previous > next
        # stock_code merge > stock_name merge
        # ========================================================
        merged["market_match_priority"] = merged["market_date_match_type"].map({
            "exact": 0,
            "previous_trading_day": 1,
            "next_trading_day": 2,
        }).fillna(9)

        merged["market_key_priority"] = merged["market_merge_key_type"].map({
            "stock_code": 0,
            "stock_name": 1,
            "none": 9,
        }).fillna(9)

        merged["market_evidence_priority"] = (
            merged["has_market_shock"] * 100
            + merged["has_price_shock"] * 10
            + merged["has_volume_shock"] * 5
        )

        merged = merged.sort_values(
            [
                "candidate_id",
                "market_evidence_priority",
                "market_match_priority",
                "market_key_priority",
                "market_residual_z_abs",
                "market_volume_ratio",
            ],
            ascending=[True, False, True, True, False, False],
        )

        selected = merged.drop_duplicates("candidate_id", keep="first").copy()

        selected["has_any_market_feature"] = (
            selected["market_date"].notna()
        ).astype(int)

        print(f"[EvidenceMerger] expanded rows: {len(expanded):,}")
        print(f"[EvidenceMerger] merged rows: {len(merged):,}")
        print(f"[EvidenceMerger] selected rows: {len(selected):,}")
        print(f"[EvidenceMerger] has_market_shock: {int((selected['has_market_shock'] == 1).sum()):,}")

        print("[EvidenceMerger] match type counts")
        print(selected["market_date_match_type"].value_counts(dropna=False).to_string())

        print("[EvidenceMerger] merge key type counts")
        print(selected["market_merge_key_type"].value_counts(dropna=False).to_string())

        return selected

    @staticmethod
    def _add_empty_market_cols(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        out["market_date"] = pd.NaT
        out["market_date_match_type"] = "none"
        out["market_residual_z_abs"] = 0.0
        out["market_return_z_abs"] = 0.0
        out["market_volume_ratio"] = 0.0
        out["market_return_pct"] = 0.0
        out["market_residual_return"] = 0.0
        out["has_price_shock"] = 0
        out["has_volume_shock"] = 0
        out["has_market_shock"] = 0
        out["has_any_market_feature"] = 0

        return out

    @staticmethod
    def _fill_market_cols(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        numeric_cols = [
            "market_residual_z_abs",
            "market_return_z_abs",
            "market_volume_ratio",
            "market_return_pct",
            "market_residual_return",
            "has_price_shock",
            "has_volume_shock",
            "has_market_shock",
        ]

        for col in numeric_cols:
            if col not in out.columns:
                out[col] = 0

            out[col] = (
                pd.to_numeric(out[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        return out


class EventCandidateClassifier:
    def __init__(self, config: EventCandidateConfig):
        self.config = config

    def classify(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        out["has_price_shock"] = (
            (out["market_residual_z_abs"] >= self.config.residual_z_cut)
            | (out["market_return_z_abs"] >= self.config.return_z_cut)
            | (out["has_price_shock"] == 1)
        ).astype(int)

        out["has_volume_shock"] = (
            (out["market_volume_ratio"] >= self.config.volume_ratio_cut)
            | (out["has_volume_shock"] == 1)
        ).astype(int)

        out["has_market_evidence"] = (
            (out["has_price_shock"] == 1)
            | (out["has_volume_shock"] == 1)
        ).astype(int)

        out["has_factual_evidence"] = 0

        out["is_news_level_board_burst"] = (
            out["board_burst_score_tier"] >= self.config.news_min_board_tier
        ).astype(int)

        out["is_diverse_board_reaction"] = (
            (out["unique_comment_author"] >= self.config.rumor_min_unique_author)
            & (out["board_signal_quality"].astype(str) == "usable_diverse")
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
            "factual_news_needed": "공시/외부근거 범위 안에서만 사실형 뉴스 작성. 근거 밖 원인 단정 금지.",
            "market_reaction_news": "가격/거래량/커뮤니티 반응만 근거로 작성. 구체적 사건 원인 생성 금지.",
            "rumor_or_speculation": "커뮤니티 추측/소문 표현만 허용. 사실형 뉴스 문장 금지.",
            "community_reaction_only": "뉴스 생성 금지. 종토방 반응/분위기 생성만 허용.",
            "discard": "생성 제외.",
        })

        out["type_reason"] = out.apply(self._build_reason, axis=1)

        return out

    def _classify_row(self, row: pd.Series) -> str:
        is_low_concentrated = int(row.get("is_author_concentrated", 0)) == 1

        has_market = int(row["has_market_evidence"]) == 1
        has_factual = int(row["has_factual_evidence"]) == 1

        board_tier = int(row["board_burst_score_tier"])
        is_news_level = board_tier >= self.config.news_min_board_tier
        is_diverse = int(row["is_diverse_board_reaction"]) == 1

        if (
            self.config.discard_low_concentrated_without_market
            and is_low_concentrated
            and not has_market
            and not has_factual
        ):
            return "discard"

        if has_factual and (has_market or is_news_level):
            return "factual_news_needed"

        if has_market:
            return "market_reaction_news"

        if board_tier >= self.config.rumor_min_board_tier and is_diverse:
            return "rumor_or_speculation"

        if board_tier >= self.config.min_board_tier:
            return "community_reaction_only"

        return "discard"

    @staticmethod
    def _build_reason(row: pd.Series) -> str:
        return (
            f"candidate_date={row.get('candidate_date')} | "
            f"market_date={row.get('market_date')} | "
            f"match={row.get('market_date_match_type')} | "
            f"board_level={row.get('board_burst_level')} | "
            f"comments={row.get('comment_count')} | "
            f"authors={row.get('unique_comment_author')} | "
            f"board_score={row.get('board_activity_score'):.4f} | "
            f"ratio20d={row.get('comment_count_ratio_20d'):.4f} | "
            f"residual_z_abs={row.get('market_residual_z_abs'):.4f} | "
            f"return_z_abs={row.get('market_return_z_abs'):.4f} | "
            f"volume_ratio={row.get('market_volume_ratio'):.4f}"
        )


class EventCandidateWriter:
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
        "fact_claim_allowed",
        "market_reaction_allowed",
        "rumor_expression_allowed",
        "community_reaction_allowed",
        "llm_guardrail",
        "type_reason",
    ]

    def __init__(self, config: EventCandidateConfig):
        self.config = config

    def write(self, df: pd.DataFrame) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        full_path = self.config.output_dir / "event_generation_candidates.csv"
        llm_path = self.config.output_dir / "event_generation_candidates_for_llm.csv"
        report_path = self.config.output_dir / "event_generation_candidate_report.txt"

        out = df.copy()

        out["candidate_date"] = pd.to_datetime(out["candidate_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        out["market_date"] = pd.to_datetime(out["market_date"], errors="coerce").dt.strftime("%Y-%m-%d")

        out.to_csv(full_path, index=False, encoding=self.config.encoding)

        llm_cols = [c for c in self.LLM_COLS if c in out.columns]
        llm_df = out[out["event_generation_candidate_type"] != "discard"][llm_cols].copy()
        llm_df.to_csv(llm_path, index=False, encoding=self.config.encoding)

        self._write_report(out, report_path)

        print(f"[SAVE] full: {full_path}")
        print(f"[SAVE] llm: {llm_path}")
        print(f"[SAVE] report: {report_path}")

    def _write_report(self, df: pd.DataFrame, path: Path) -> None:
        lines = []

        lines.append("# Event Generation Candidate Report")
        lines.append("")
        lines.append("## Thresholds")
        lines.append(f"- use_scope: {self.config.use_scope}")
        lines.append(f"- min_board_tier: {self.config.min_board_tier}")
        lines.append(f"- news_min_board_tier: {self.config.news_min_board_tier}")
        lines.append(f"- residual_z_cut: {self.config.residual_z_cut}")
        lines.append(f"- return_z_cut: {self.config.return_z_cut}")
        lines.append(f"- volume_ratio_cut: {self.config.volume_ratio_cut}")
        lines.append("")
        lines.append("## Counts")
        lines.append(f"- total_rows: {len(df):,}")
        lines.append(f"- non_discard_rows: {(df['event_generation_candidate_type'] != 'discard').sum():,}")
        lines.append("")

        lines.append("## By event_generation_candidate_type")
        for k, v in df["event_generation_candidate_type"].value_counts(dropna=False).items():
            lines.append(f"- {k}: {int(v):,}")

        lines.append("")
        lines.append("## By board level and type")
        cross = (
            df.groupby(["board_burst_level", "event_generation_candidate_type"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["board_burst_level", "event_generation_candidate_type"])
        )

        for _, row in cross.iterrows():
            lines.append(f"- {row['board_burst_level']} / {row['event_generation_candidate_type']}: {int(row['count']):,}")

        lines.append("")
        lines.append("## Market date match type")
        for k, v in df["market_date_match_type"].value_counts(dropna=False).items():
            lines.append(f"- {k}: {int(v):,}")

        lines.append("")
        lines.append("## Evidence")
        lines.append(f"- has_price_shock: {int((df['has_price_shock'] == 1).sum()):,}")
        lines.append(f"- has_volume_shock: {int((df['has_volume_shock'] == 1).sum()):,}")
        lines.append(f"- has_market_evidence: {int((df['has_market_evidence'] == 1).sum()):,}")
        lines.append("- has_factual_evidence: 0")
        lines.append("")

        lines.append("## Suggested Usage")
        lines.append("- factual_news_needed: 사실형 뉴스 생성 가능. 단, 근거 범위 밖 원인 단정 금지.")
        lines.append("- market_reaction_news: 가격/거래량 반응형 뉴스만 생성. 구체 원인 생성 금지.")
        lines.append("- rumor_or_speculation: 소문/추측형 커뮤니티 문장만 생성. 사실형 뉴스 금지.")
        lines.append("- community_reaction_only: 뉴스 생성 금지. 종토방 반응만 생성.")
        lines.append("- discard: 생성 제외.")

        with open(path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))


class EventCandidatePipeline:
    def __init__(self, config: EventCandidateConfig):
        self.config = config

    def run(self) -> None:
        board_df = BoardLabeledLoader(self.config).load()
        market_df = MarketFeatureLoader(self.config).load()

        merged = EvidenceMerger(self.config).merge(
            board_df=board_df,
            market_df=market_df,
        )

        classified = EventCandidateClassifier(self.config).classify(merged)
        EventCandidateWriter(self.config).write(classified)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


def build_config_from_args() -> EventCandidateConfig:
    project_root = Path(__file__).resolve().parent.parent

    default_board_labeled_path = (
        project_root
        / "data"
        / "processed"
        / "dci_board_labeled"
        / "board_activity_labeled.csv"
    )

    default_output_dir = (
        project_root
        / "data"
        / "processed"
        / "dci_event_candidates"
    )

    parser = argparse.ArgumentParser()

    parser.add_argument("--board-labeled", type=str, default=str(default_board_labeled_path))
    parser.add_argument("--market-features", type=str, default="")
    parser.add_argument("--output-dir", type=str, default=str(default_output_dir))

    parser.add_argument("--use-scope", type=str, default="single_only")
    parser.add_argument("--min-board-tier", type=int, default=2)
    parser.add_argument("--news-min-board-tier", type=int, default=3)

    parser.add_argument("--residual-z-cut", type=float, default=2.5)
    parser.add_argument("--return-z-cut", type=float, default=2.5)
    parser.add_argument("--volume-ratio-cut", type=float, default=2.5)

    args = parser.parse_args()

    market_path = None
    if args.market_features.strip():
        market_path = Path(args.market_features).expanduser().resolve()

    return EventCandidateConfig(
        project_root=project_root,
        board_labeled_path=Path(args.board_labeled).expanduser().resolve(),
        market_features_path=market_path,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        use_scope=args.use_scope,
        min_board_tier=args.min_board_tier,
        news_min_board_tier=args.news_min_board_tier,
        residual_z_cut=args.residual_z_cut,
        return_z_cut=args.return_z_cut,
        volume_ratio_cut=args.volume_ratio_cut,
    )


def main() -> None:
    config = build_config_from_args()
    EventCandidatePipeline(config).run()


if __name__ == "__main__":
    main()