from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class EventContextPilotConfig:
    year: int = 2020

    project_root: Path = Path(".")
    input_dir: Path = Path("data/raw")
    output_dir: Path = Path("data/raw")

    macro_event_file: str = "macro_event_candidates_daily.csv"
    sector_event_file: str = "sector_event_candidates_daily.csv"
    external_event_file_template: str = "external_event_candidates_{year}.csv"

    max_events_per_day: int = 10

    forbidden_terms: str = (
        "장중 급등|장 초반 강세|시초가|고가|저가|고점 대비 하락|저점 반등|"
        "갭상승|갭하락|장 마감 직전 매수세"
    )

    group_caps: Dict[str, int] = None
    event_type_cap: int = 2
    asset_id_cap: int = 1

    def __post_init__(self):
        if self.group_caps is None:
            self.group_caps = {
            "disclosure": 3,
            "gdelt_news": 3,
            "sector": 3,
            "macro_market": 2,
            "fx_commodity": 2,
            "rate_bond": 2,
            "macro_indicator": 2,
            "other": 1,
        }

    def resolve_paths(self) -> "EventContextPilotConfig":
        self.project_root = self.project_root.resolve()

        if not self.input_dir.is_absolute():
            self.input_dir = self.project_root / self.input_dir

        if not self.output_dir.is_absolute():
            self.output_dir = self.project_root / self.output_dir

        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def external_event_file(self) -> str:
        return self.external_event_file_template.format(year=self.year)


class EventCandidateLoader:
    REQUIRED_COLUMNS = [
        "date",
        "source_table",
        "signal_group",
        "asset_class",
        "asset_id",
        "market",
        "sector",
        "ticker",
        "company_name",
        "event_type",
        "event_frame",
        "direction",
        "strength",
        "evidence_1",
        "evidence_2",
        "evidence_3",
        "news_style",
        "forbidden_terms",
    ]

    TEXT_COLUMNS = [
        "source_table",
        "signal_group",
        "asset_class",
        "asset_id",
        "market",
        "sector",
        "ticker",
        "company_name",
        "event_type",
        "event_frame",
        "direction",
        "evidence_1",
        "evidence_2",
        "evidence_3",
        "news_style",
        "forbidden_terms",
    ]

    def __init__(self, config: EventContextPilotConfig):
        self.config = config

    def load_all(self) -> pd.DataFrame:
        frames = []

        specs = [
            (self.config.macro_event_file, "macro_event_candidates_daily"),
            (self.config.sector_event_file, "sector_event_candidates_daily"),
            (self.config.external_event_file, f"external_event_candidates_{self.config.year}"),
        ]

        for file_name, source_name in specs:
            path = self.config.input_dir / file_name

            if not path.exists():
                raise FileNotFoundError(f"필수 입력 파일 없음: {path}")

            df = self._load_one(path, source_name)
            frames.append(df)

        all_df = pd.concat(frames, ignore_index=True)

        all_df = all_df.dropna(subset=["date"])
        all_df = all_df[all_df["date"].dt.year == self.config.year].copy()
        all_df = all_df[all_df["event_frame"].astype(str).str.strip().ne("")].copy()

        all_df = all_df.reset_index(drop=True)

        print(f"[전체 후보 rows] {len(all_df):,}")
        print("[signal_group 분포]")
        print(all_df["signal_group"].value_counts(dropna=False))

        return all_df

    def _load_one(self, path: Path, source_name: str) -> pd.DataFrame:
        df = pd.read_csv(path)

        print(f"[로드] {path.name} rows={len(df):,} cols={len(df.columns)}")

        df = df.copy()

        for col in self.REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = ""

        df = df[self.REQUIRED_COLUMNS].copy()

        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        df["source_table"] = df["source_table"].replace("", source_name)
        df["source_table"] = df["source_table"].fillna(source_name)

        for col in self.TEXT_COLUMNS:
            df[col] = df[col].fillna("").astype(str).str.strip()
            df[col] = df[col].replace({"<NA>": "", "nan": "", "None": ""})

        df["strength"] = (
            pd.to_numeric(df["strength"], errors="coerce")
            .fillna(1)
            .astype(int)
            .clip(lower=1, upper=5)
        )

        df["direction"] = df["direction"].replace("", "neutral")
        df["forbidden_terms"] = df["forbidden_terms"].replace("", self.config.forbidden_terms)

        df["signal_group"] = df.apply(self._ensure_signal_group, axis=1)

        return df

    def _ensure_signal_group(self, row: pd.Series) -> str:
        current = str(row.get("signal_group", "")).strip()

        if current:
            return current

        source_table = str(row.get("source_table", ""))
        asset_class = str(row.get("asset_class", ""))
        event_type = str(row.get("event_type", ""))

        if "disclosure" in source_table:
            return "disclosure"

        if "gdelt" in source_table:
            return "gdelt_news"

        if "sector" in source_table or asset_class == "국내 업종":
            return "sector"

        if asset_class in ["국내 주식시장", "국내 성장주 시장", "미국 기술주 시장", "미국 대형주 시장"]:
            return "macro_market"

        if asset_class in ["환율", "원자재"]:
            return "fx_commodity"

        if asset_class in ["금리", "채권"]:
            return "rate_bond"

        if asset_class == "거시지표":
            return "macro_indicator"

        if event_type in ["rate_move", "spread_move"]:
            return "rate_bond"

        if event_type in ["fx_move", "oil_move", "safe_asset_move"]:
            return "fx_commodity"

        if event_type == "market_close_move":
            return "macro_market"

        return "other"


class EventPriorityScorer:
    def __init__(self):
        self.event_type_bonus = {
            # disclosure
            "legal_risk": 24,
            "listing_risk": 24,
            "litigation": 22,
            "capital_increase": 18,
            "supply_contract": 18,
            "share_buyback": 16,
            "restructuring": 15,
            "major_management_issue": 13,
            "ownership_change": 10,
            "earnings": 10,
            "dividend": 8,
            "bonus_issue": 8,
            "share_disposal": 8,

            # gdelt
            "gdelt_theme_event": 10,

            # macro
            "market_close_move": 14,
            "rate_move": 14,
            "spread_move": 13,
            "oil_move": 13,
            "fx_move": 13,
            "safe_asset_move": 8,
            "inflation_update": 10,
            "leading_index_update": 8,
            "trade_update": 7,
            "trade_balance_update": 7,
            "real_activity_update": 8,
            "consumption_update": 8,
            "investment_update": 8,

            # sector
            "sector_relative_move": 11,
            "sector_close_move": 10,
            "sector_leader": 9,
            "sector_laggard": 9,
            "sector_5d_trend": 8,
        }

        self.signal_group_bonus = {
            "disclosure": 8,
            "gdelt_news": 7,
            "macro_market": 5,
            "fx_commodity": 5,
            "rate_bond": 5,
            "sector": 4,
            "macro_indicator": 3,
            "other": 0,
        }

        self.direction_bonus = {
            "negative": 3,
            "positive": 2,
            "neutral": 0,
        }

    def add_priority_score(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        out["priority_score"] = out["strength"] * 20

        out["priority_score"] += (
            out["event_type"]
            .map(self.event_type_bonus)
            .fillna(0)
            .astype(int)
        )

        out["priority_score"] += (
            out["signal_group"]
            .map(self.signal_group_bonus)
            .fillna(0)
            .astype(int)
        )

        out["priority_score"] += (
            out["direction"]
            .map(self.direction_bonus)
            .fillna(0)
            .astype(int)
        )

        out["event_id"] = out.apply(self._make_event_id, axis=1)

        return out

    def _make_event_id(self, row: pd.Series) -> str:
        raw = "|".join(
            [
                str(row.get("date", "")),
                str(row.get("source_table", "")),
                str(row.get("signal_group", "")),
                str(row.get("asset_id", "")),
                str(row.get("ticker", "")),
                str(row.get("company_name", "")),
                str(row.get("event_type", "")),
                str(row.get("event_frame", "")),
            ]
        )

        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


class DailyEventSelector:
    def __init__(self, config: EventContextPilotConfig):
        self.config = config

    def select(self, df: pd.DataFrame) -> pd.DataFrame:
        selected_parts = []

        for date, day_df in df.groupby("date"):
            selected = self._select_one_day(date, day_df)
            if not selected.empty:
                selected_parts.append(selected)

        if not selected_parts:
            return pd.DataFrame()

        result = pd.concat(selected_parts, ignore_index=True)
        result = result.sort_values(["date", "selection_rank"]).reset_index(drop=True)

        return result

    def _select_one_day(self, date: pd.Timestamp, day_df: pd.DataFrame) -> pd.DataFrame:
        day_df = day_df.sort_values(
            ["priority_score", "strength"],
            ascending=[False, False],
        ).reset_index(drop=True)

        selected_indices: List[int] = []
        used_event_ids = set()
        group_counts: Dict[str, int] = {}
        event_type_counts: Dict[str, int] = {}
        asset_id_counts: Dict[str, int] = {}

        # 외부 데이터가 아예 밀려나지 않도록 disclosure/gdelt 각각 1개 우선 선점
        for mandatory_group in ["disclosure", "gdelt_news"]:
            if len(selected_indices) >= self.config.max_events_per_day:
                break

            group_df = day_df[day_df["signal_group"].eq(mandatory_group)]

            if group_df.empty:
                continue

            for idx, row in group_df.iterrows():
                if self._can_select(
                    row=row,
                    used_event_ids=used_event_ids,
                    group_counts=group_counts,
                    event_type_counts=event_type_counts,
                    asset_id_counts=asset_id_counts,
                    relax_group=False,
                ):
                    self._mark_selected(
                        idx=idx,
                        row=row,
                        selected_indices=selected_indices,
                        used_event_ids=used_event_ids,
                        group_counts=group_counts,
                        event_type_counts=event_type_counts,
                        asset_id_counts=asset_id_counts,
                    )
                    break

        # 일반 우선순위 선정
        for idx, row in day_df.iterrows():
            if len(selected_indices) >= self.config.max_events_per_day:
                break

            if idx in selected_indices:
                continue

            if self._can_select(
                row=row,
                used_event_ids=used_event_ids,
                group_counts=group_counts,
                event_type_counts=event_type_counts,
                asset_id_counts=asset_id_counts,
                relax_group=False,
            ):
                self._mark_selected(
                    idx=idx,
                    row=row,
                    selected_indices=selected_indices,
                    used_event_ids=used_event_ids,
                    group_counts=group_counts,
                    event_type_counts=event_type_counts,
                    asset_id_counts=asset_id_counts,
                )

        # 후보가 10개 미만이면 group cap만 완화해서 채움
        for idx, row in day_df.iterrows():
            if len(selected_indices) >= self.config.max_events_per_day:
                break

            if idx in selected_indices:
                continue

            if self._can_select(
                row=row,
                used_event_ids=used_event_ids,
                group_counts=group_counts,
                event_type_counts=event_type_counts,
                asset_id_counts=asset_id_counts,
                relax_group=True,
            ):
                self._mark_selected(
                    idx=idx,
                    row=row,
                    selected_indices=selected_indices,
                    used_event_ids=used_event_ids,
                    group_counts=group_counts,
                    event_type_counts=event_type_counts,
                    asset_id_counts=asset_id_counts,
                )

        selected = day_df.loc[selected_indices].copy()

        if selected.empty:
            return selected

        selected = selected.sort_values(
            ["priority_score", "strength"],
            ascending=[False, False],
        ).reset_index(drop=True)

        selected["selected_for_daily_news"] = True
        selected["selection_rank"] = range(1, len(selected) + 1)

        return selected

    def _can_select(
        self,
        row: pd.Series,
        used_event_ids: set,
        group_counts: Dict[str, int],
        event_type_counts: Dict[str, int],
        asset_id_counts: Dict[str, int],
        relax_group: bool,
    ) -> bool:
        event_id = str(row["event_id"])
        signal_group = str(row["signal_group"])
        event_type = str(row["event_type"])
        asset_id = str(row["asset_id"])

        if event_id in used_event_ids:
            return False

        if not relax_group:
            group_cap = self.config.group_caps.get(signal_group, self.config.group_caps["other"])
            if group_counts.get(signal_group, 0) >= group_cap:
                return False

        if event_type_counts.get(event_type, 0) >= self.config.event_type_cap:
            return False

        if asset_id_counts.get(asset_id, 0) >= self.config.asset_id_cap:
            return False

        return True

    def _mark_selected(
        self,
        idx: int,
        row: pd.Series,
        selected_indices: List[int],
        used_event_ids: set,
        group_counts: Dict[str, int],
        event_type_counts: Dict[str, int],
        asset_id_counts: Dict[str, int],
    ) -> None:
        selected_indices.append(idx)

        event_id = str(row["event_id"])
        signal_group = str(row["signal_group"])
        event_type = str(row["event_type"])
        asset_id = str(row["asset_id"])

        used_event_ids.add(event_id)
        group_counts[signal_group] = group_counts.get(signal_group, 0) + 1
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        asset_id_counts[asset_id] = asset_id_counts.get(asset_id, 0) + 1


class EventContextPilotBuilder:
    def __init__(self, config: EventContextPilotConfig):
        self.config = config
        self.loader = EventCandidateLoader(config)
        self.scorer = EventPriorityScorer()
        self.selector = DailyEventSelector(config)

    def run(self) -> None:
        all_candidates = self.loader.load_all()
        all_candidates = self.scorer.add_priority_score(all_candidates)

        selected = self.selector.select(all_candidates)
        rejected = self._build_rejected(all_candidates, selected)
        summary = self._build_summary(all_candidates, selected)

        self._save(all_candidates, selected, rejected, summary)

    def _build_rejected(self, all_candidates: pd.DataFrame, selected: pd.DataFrame) -> pd.DataFrame:
        if selected.empty:
            return all_candidates.copy()

        selected_ids = set(selected["event_id"].astype(str))
        rejected = all_candidates[~all_candidates["event_id"].astype(str).isin(selected_ids)].copy()

        return rejected.reset_index(drop=True)

    def _build_summary(self, all_candidates: pd.DataFrame, selected: pd.DataFrame) -> pd.DataFrame:
        all_count = (
            all_candidates.groupby("date")
            .size()
            .reset_index(name="all_candidate_count")
        )

        selected_count = (
            selected.groupby("date")
            .size()
            .reset_index(name="selected_count")
        )

        summary = all_count.merge(selected_count, on="date", how="left")
        summary["selected_count"] = summary["selected_count"].fillna(0).astype(int)

        group_pivot = (
            selected.groupby(["date", "signal_group"])
            .size()
            .reset_index(name="count")
            .pivot_table(
                index="date",
                columns="signal_group",
                values="count",
                aggfunc="sum",
                fill_value=0,
            )
            .reset_index()
        )

        summary = summary.merge(group_pivot, on="date", how="left")

        for col in summary.columns:
            if col != "date":
                summary[col] = summary[col].fillna(0).astype(int)

        return summary

    def _save(
        self,
        all_candidates: pd.DataFrame,
        selected: pd.DataFrame,
        rejected: pd.DataFrame,
        summary: pd.DataFrame,
    ) -> None:
        year = self.config.year

        all_path = self.config.output_dir / f"event_context_daily_all_candidates_{year}.csv"
        selected_path = self.config.output_dir / f"event_context_daily_{year}.csv"
        rejected_path = self.config.output_dir / f"event_context_daily_rejected_{year}.csv"
        summary_path = self.config.output_dir / f"event_context_daily_summary_{year}.csv"

        all_candidates = self._format_date(all_candidates)
        selected = self._format_date(selected)
        rejected = self._format_date(rejected)
        summary = self._format_date(summary)

        all_candidates.to_csv(all_path, index=False, encoding="utf-8-sig")
        selected.to_csv(selected_path, index=False, encoding="utf-8-sig")
        rejected.to_csv(rejected_path, index=False, encoding="utf-8-sig")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

        print("=" * 80)
        print(f"[저장 완료] {all_path}")
        print(f"[저장 완료] {selected_path}")
        print(f"[저장 완료] {rejected_path}")
        print(f"[저장 완료] {summary_path}")
        print(f"[전체 후보 수] {len(all_candidates):,}")
        print(f"[선정 후보 수] {len(selected):,}")
        print(f"[제외 후보 수] {len(rejected):,}")
        print("=" * 80)

        if not selected.empty:
            print("[선정 후보 signal_group 분포]")
            print(selected["signal_group"].value_counts())

            print("[선정 후보 샘플]")
            print(selected.head(30))

    def _format_date(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")

        return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument("--year", type=int, default=2020)
    parser.add_argument("--project-root", type=str, default=".")
    parser.add_argument("--input-dir", type=str, default="data/raw")
    parser.add_argument("--output-dir", type=str, default="data/raw")
    parser.add_argument("--max-events-per-day", type=int, default=10)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = EventContextPilotConfig(
        year=args.year,
        project_root=Path(args.project_root),
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        max_events_per_day=args.max_events_per_day,
    ).resolve_paths()

    print("=" * 80)
    print("[pr02 2020년 파일럿 이벤트 컨텍스트 생성 시작]")
    print(f"project_root: {config.project_root}")
    print(f"year: {config.year}")
    print(f"input_dir: {config.input_dir}")
    print(f"output_dir: {config.output_dir}")
    print(f"max_events_per_day: {config.max_events_per_day}")
    print("=" * 80)

    builder = EventContextPilotBuilder(config)
    builder.run()


if __name__ == "__main__":
    main()