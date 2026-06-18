from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


CLAIM_ORDER = {
    "insufficient_evidence": 0,
    "no_market_claim": 1,
    "reaction_only": 2,
    "plausible_market_context": 3,
    "likely_contributor": 4,
    "strongest_attributable_disclosed_factor": 5,
    "primary_market_driver_candidate": 5,  # legacy alias
}

ROUTINE_FAMILIES = {
    "dividend",
    "treasury_stock",
}

WEAK_FAMILIES = {
    "other_company_event",
}

EXPECTED_FAMILIES = {
    "earnings",
    "guidance",
    "contract",
    "investment",
    "asset_transaction",
    "equity_investment",
    "capital_financing",
    "business_transfer",
    "legal_regulatory",
    "trading_status",
    "listing_risk",
    "major_management_matter",
    "dividend",
    "treasury_stock",
    "other_company_event",
}


@dataclass(frozen=True)
class AuditConfig:
    bundle_csv: Path
    bundle_jsonl: Path
    judge_inputs_jsonl: Path
    output_dir: Path
    sample_size: int = 120


class JsonlReader:
    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        if not path.exists():
            return rows

        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSONL at {path}:{line_no}: {e}") from e

        return rows


class BoolNormalizer:
    TRUE_VALUES = {"true", "1", "yes", "y", "t", "True", "TRUE"}
    FALSE_VALUES = {"false", "0", "no", "n", "f", "False", "FALSE"}

    @classmethod
    def to_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if pd.isna(value):
            return False
        text = str(value).strip()
        if text in cls.TRUE_VALUES:
            return True
        if text in cls.FALSE_VALUES:
            return False
        return False


class BundleAudit:
    def __init__(self, config: AuditConfig) -> None:
        self.config = config
        self.output_dir = config.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        df = self._load_bundle_csv()
        bundles = JsonlReader.read_jsonl(self.config.bundle_jsonl)
        judge_inputs = JsonlReader.read_jsonl(self.config.judge_inputs_jsonl)

        df = self._normalize_dataframe(df)

        summary_tables = self._build_summary_tables(df, judge_inputs)
        suspicious = self._find_suspicious_cases(df)
        review_sample = self._build_review_sample(df)

        self._write_tables(summary_tables, suspicious, review_sample)
        self._write_markdown_report(df, judge_inputs, summary_tables, suspicious)

        print("=" * 100)
        print("[audit] pr05e stock evidence bundles")
        print(f"bundle_csv: {self.config.bundle_csv}")
        print(f"bundle_jsonl: {self.config.bundle_jsonl}")
        print(f"judge_inputs_jsonl: {self.config.judge_inputs_jsonl}")
        print(f"output_dir: {self.output_dir}")
        print("=" * 100)
        print(f"bundles_csv_rows: {len(df):,}")
        print(f"bundles_jsonl_rows: {len(bundles):,}")
        print(f"judge_inputs_rows: {len(judge_inputs):,}")
        print(f"suspicious_rows: {len(suspicious):,}")
        print()
        print("[outputs]")
        for name in [
            "audit_summary.md",
            "family_claim_distribution.csv",
            "rank_claim_distribution.csv",
            "judge_allowed_distribution.csv",
            "suspicious_bundles.csv",
            "manual_review_sample.csv",
        ]:
            print(f"  {self.output_dir / name}")

    def _load_bundle_csv(self) -> pd.DataFrame:
        if not self.config.bundle_csv.exists():
            raise FileNotFoundError(f"Bundle CSV not found: {self.config.bundle_csv}")
        return pd.read_csv(self.config.bundle_csv, dtype=str).fillna("")

    def _normalize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        required_defaults = {
            "bundle_id": "",
            "event_group_id": "",
            "stock_code": "",
            "stock_name": "",
            "anchor_date": "",
            "event_family": "",
            "candidate_topic": "",
            "primary_topic_source": "",
            "bundle_candidate_rank": "",
            "rank_reason": "",
            "corroboration_level": "",
            "max_allowed_market_claim_level_pre_llm": "",
            "needs_bundle_llm_judge": "",
            "judge_input_allowed": "",
            "dart_count": "0",
            "stock_event_count": "0",
            "stock_event_context_count": "0",
            "price_volume_count": "0",
            "gdelt_count": "0",
            "macro_count": "0",
            "has_dart": "",
            "has_official_evidence": "",
            "has_stock_event_trigger": "",
            "has_stock_event_context": "",
            "has_price_reaction": "",
            "has_strong_price_reaction": "",
            "has_gdelt_support": "",
            "has_macro_background": "",
            "directional_consistency": "",
        }

        for col, default in required_defaults.items():
            if col not in df.columns:
                df[col] = default

        bool_cols = [
            "needs_bundle_llm_judge",
            "judge_input_allowed",
            "has_dart",
            "has_official_evidence",
            "has_stock_event_trigger",
            "has_stock_event_context",
            "has_price_reaction",
            "has_strong_price_reaction",
            "has_gdelt_support",
            "has_macro_background",
        ]

        for col in bool_cols:
            df[col + "_bool"] = df[col].map(BoolNormalizer.to_bool)

        count_cols = [
            "dart_count",
            "stock_event_count",
            "stock_event_context_count",
            "price_volume_count",
            "gdelt_count",
            "macro_count",
        ]

        for col in count_cols:
            df[col + "_num"] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

        df["claim_order"] = (
            df["max_allowed_market_claim_level_pre_llm"]
            .map(CLAIM_ORDER)
            .fillna(-1)
            .astype(int)
        )

        df["bundle_candidate_rank_num"] = (
            pd.to_numeric(df["bundle_candidate_rank"], errors="coerce")
            .fillna(999)
            .astype(int)
        )

        df["stock_year"] = (
            df["stock_code"].astype(str).str.zfill(6)
            + "_"
            + df["anchor_date"].astype(str).str.slice(0, 4)
        )

        return df

    def _build_summary_tables(
        self,
        df: pd.DataFrame,
        judge_inputs: list[dict[str, Any]],
    ) -> dict[str, pd.DataFrame]:
        tables: dict[str, pd.DataFrame] = {}

        tables["family_claim_distribution"] = pd.crosstab(
            df["event_family"],
            df["max_allowed_market_claim_level_pre_llm"],
            margins=True,
        ).reset_index()

        tables["rank_claim_distribution"] = pd.crosstab(
            df["bundle_candidate_rank"],
            df["max_allowed_market_claim_level_pre_llm"],
            margins=True,
        ).reset_index()

        tables["judge_allowed_distribution"] = pd.crosstab(
            df["event_family"],
            df["judge_input_allowed_bool"],
            margins=True,
        ).reset_index()

        tables["family_rank_distribution"] = pd.crosstab(
            df["event_family"],
            df["bundle_candidate_rank"],
            margins=True,
        ).reset_index()

        tables["source_presence_by_claim"] = (
            df.groupby("max_allowed_market_claim_level_pre_llm", dropna=False)
            .agg(
                bundles=("bundle_id", "count"),
                has_dart=("has_dart_bool", "sum"),
                has_stock_event_trigger=("has_stock_event_trigger_bool", "sum"),
                has_stock_event_context=("has_stock_event_context_bool", "sum"),
                has_price_reaction=("has_price_reaction_bool", "sum"),
                has_strong_price_reaction=("has_strong_price_reaction_bool", "sum"),
                has_gdelt_support=("has_gdelt_support_bool", "sum"),
                has_macro_background=("has_macro_background_bool", "sum"),
            )
            .reset_index()
        )

        judge_ids = []
        for row in judge_inputs:
            custom_id = row.get("custom_id", "")
            if custom_id.startswith("bundle_judge_"):
                judge_ids.append(custom_id.replace("bundle_judge_", "", 1))

        judge_id_df = pd.DataFrame({"bundle_id": judge_ids})
        judge_id_df["exists_in_csv"] = judge_id_df["bundle_id"].isin(set(df["bundle_id"]))

        tables["judge_input_integrity"] = pd.DataFrame(
            [
                {
                    "csv_rows": len(df),
                    "judge_input_rows": len(judge_inputs),
                    "judge_custom_ids": len(judge_ids),
                    "judge_ids_missing_in_csv": int((~judge_id_df["exists_in_csv"]).sum())
                    if len(judge_id_df)
                    else 0,
                    "csv_judge_allowed_true": int(df["judge_input_allowed_bool"].sum()),
                }
            ]
        )

        return tables

    def _find_suspicious_cases(self, df: pd.DataFrame) -> pd.DataFrame:
        flags: list[pd.Series] = []
        reasons: list[str] = []

        def add_flag(mask: pd.Series, reason: str) -> None:
            flags.append(mask.fillna(False))
            reasons.append(reason)

        add_flag(
            (~df["has_price_reaction_bool"]) & (df["claim_order"] >= CLAIM_ORDER["reaction_only"]),
            "claim_level_requires_price_reaction_but_no_price_reaction",
        )

        add_flag(
            (df["directional_consistency"].str.lower() == "inconsistent")
            & (df["claim_order"] > CLAIM_ORDER["reaction_only"]),
            "directional_inconsistency_not_downward_capped",
        )

        add_flag(
            df["event_family"].isin(ROUTINE_FAMILIES)
            & (df["claim_order"] >= CLAIM_ORDER["plausible_market_context"]),
            "routine_family_has_plausible_or_higher_ceiling",
        )

        add_flag(
            df["event_family"].isin(WEAK_FAMILIES)
            & (df["claim_order"] >= CLAIM_ORDER["plausible_market_context"]),
            "weak_family_has_plausible_or_higher_ceiling",
        )

        add_flag(
            (~df["has_official_evidence_bool"])
            & (~df["has_stock_event_trigger_bool"])
            & (df["claim_order"] >= CLAIM_ORDER["plausible_market_context"]),
            "plausible_or_higher_without_official_or_stock_trigger",
        )

        add_flag(
            df["judge_input_allowed_bool"]
            & (df["bundle_candidate_rank_num"] >= 6)
            & (~df["has_price_reaction_bool"])
            & (~df["has_stock_event_trigger_bool"])
            & (~df["has_stock_event_context_bool"]),
            "low_rank_weak_bundle_allowed_to_judge",
        )

        add_flag(
            ~df["event_family"].isin(EXPECTED_FAMILIES),
            "unknown_or_unclassified_event_family",
        )

        add_flag(
            df["judge_input_allowed_bool"]
            & df["event_family"].isin(ROUTINE_FAMILIES)
            & (~df["has_price_reaction_bool"])
            & (~df["has_stock_event_trigger_bool"])
            & (~df["has_stock_event_context_bool"]),
            "routine_bundle_allowed_without_extra_context",
        )

        reason_rows = []
        for idx, row in df.iterrows():
            row_reasons = [
                reason for mask, reason in zip(flags, reasons)
                if bool(mask.loc[idx])
            ]
            if row_reasons:
                out = row.copy()
                out["audit_flags"] = " | ".join(row_reasons)
                reason_rows.append(out)

        if not reason_rows:
            return pd.DataFrame(columns=list(df.columns) + ["audit_flags"])

        suspicious = pd.DataFrame(reason_rows)

        preferred_cols = [
            "audit_flags",
            "bundle_id",
            "event_group_id",
            "stock_code",
            "stock_name",
            "anchor_date",
            "event_family",
            "candidate_topic",
            "primary_topic_source",
            "bundle_candidate_rank",
            "rank_reason",
            "corroboration_level",
            "max_allowed_market_claim_level_pre_llm",
            "judge_input_allowed",
            "dart_count",
            "stock_event_count",
            "stock_event_context_count",
            "price_volume_count",
            "gdelt_count",
            "macro_count",
            "has_dart",
            "has_official_evidence",
            "has_stock_event_trigger",
            "has_stock_event_context",
            "has_price_reaction",
            "has_strong_price_reaction",
            "directional_consistency",
        ]

        return suspicious[[c for c in preferred_cols if c in suspicious.columns]]

    def _build_review_sample(self, df: pd.DataFrame) -> pd.DataFrame:
        group_cols = [
            "event_family",
            "max_allowed_market_claim_level_pre_llm",
            "judge_input_allowed_bool",
        ]

        sampled_parts = []
        for _, group in df.groupby(group_cols, dropna=False):
            n = min(3, len(group))
            sampled_parts.append(group.sample(n=n, random_state=42))

        if sampled_parts:
            sample = pd.concat(sampled_parts, ignore_index=True)
        else:
            sample = df.head(0).copy()

        if len(sample) > self.config.sample_size:
            sample = sample.sample(n=self.config.sample_size, random_state=42)

        preferred_cols = [
            "bundle_id",
            "event_group_id",
            "stock_code",
            "stock_name",
            "anchor_date",
            "event_family",
            "candidate_topic",
            "primary_topic_source",
            "bundle_candidate_rank",
            "rank_reason",
            "corroboration_level",
            "max_allowed_market_claim_level_pre_llm",
            "judge_input_allowed",
            "dart_count",
            "stock_event_count",
            "stock_event_context_count",
            "price_volume_count",
            "gdelt_count",
            "macro_count",
            "has_dart",
            "has_official_evidence",
            "has_stock_event_trigger",
            "has_stock_event_context",
            "has_price_reaction",
            "has_strong_price_reaction",
            "directional_consistency",
            "allowed_claims",
            "forbidden_claims",
        ]

        return sample[[c for c in preferred_cols if c in sample.columns]]

    def _write_tables(
        self,
        summary_tables: dict[str, pd.DataFrame],
        suspicious: pd.DataFrame,
        review_sample: pd.DataFrame,
    ) -> None:
        for name, table in summary_tables.items():
            table.to_csv(self.output_dir / f"{name}.csv", index=False, encoding="utf-8-sig")

        suspicious.to_csv(
            self.output_dir / "suspicious_bundles.csv",
            index=False,
            encoding="utf-8-sig",
        )

        review_sample.to_csv(
            self.output_dir / "manual_review_sample.csv",
            index=False,
            encoding="utf-8-sig",
        )

    def _write_markdown_report(
        self,
        df: pd.DataFrame,
        judge_inputs: list[dict[str, Any]],
        summary_tables: dict[str, pd.DataFrame],
        suspicious: pd.DataFrame,
    ) -> None:
        total = len(df)
        judge_allowed = int(df["judge_input_allowed_bool"].sum())
        judge_blocked = total - judge_allowed
        plausible = int(
            (df["max_allowed_market_claim_level_pre_llm"] == "plausible_market_context").sum()
        )
        no_market = int(
            (df["max_allowed_market_claim_level_pre_llm"] == "no_market_claim").sum()
        )

        suspicious_flag_counts = (
            suspicious["audit_flags"]
            .str.get_dummies(sep=" | ")
            .sum()
            .sort_values(ascending=False)
            if len(suspicious) and "audit_flags" in suspicious.columns
            else pd.Series(dtype=int)
        )

        judge_ratio = judge_allowed / total if total else 0.0
        plausible_ratio = plausible / total if total else 0.0

        lines = []
        lines.append("# pr05e Stock Evidence Bundle Audit")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- total_bundles: {total:,}")
        lines.append(f"- judge_input_allowed: {judge_allowed:,} ({judge_ratio:.1%})")
        lines.append(f"- judge_input_blocked: {judge_blocked:,}")
        lines.append(f"- bundle_judge_inputs_jsonl_rows: {len(judge_inputs):,}")
        lines.append(f"- plausible_market_context: {plausible:,} ({plausible_ratio:.1%})")
        lines.append(f"- no_market_claim: {no_market:,}")
        lines.append(f"- suspicious_bundles: {len(suspicious):,}")
        lines.append("")

        lines.append("## Initial Interpretation")
        lines.append("")
        if judge_ratio >= 0.85:
            lines.append(
                "- judge_input_allowed ratio is high. The current filter is weak as a cost-control gate."
            )
        else:
            lines.append(
                "- judge_input_allowed ratio is moderate. Cost-control gate is working to some degree."
            )

        if plausible_ratio >= 0.60:
            lines.append(
                "- plausible_market_context ratio is high. Check whether strong-family policy is too broad."
            )
        else:
            lines.append(
                "- plausible_market_context ratio is not excessively dominant."
            )

        lines.append(
            "- If price_volume evidence is absent, reaction_only or higher should generally not appear."
        )
        lines.append("")

        lines.append("## Suspicious Flag Counts")
        lines.append("")
        if len(suspicious_flag_counts):
            for flag, count in suspicious_flag_counts.items():
                lines.append(f"- {flag}: {int(count):,}")
        else:
            lines.append("- No suspicious flags detected by current audit rules.")
        lines.append("")

        lines.append("## Top Event Families")
        lines.append("")
        family_counts = df["event_family"].value_counts(dropna=False).head(20)
        for family, count in family_counts.items():
            lines.append(f"- {family}: {int(count):,}")
        lines.append("")

        lines.append("## Claim Levels")
        lines.append("")
        claim_counts = df["max_allowed_market_claim_level_pre_llm"].value_counts(dropna=False)
        for claim, count in claim_counts.items():
            lines.append(f"- {claim}: {int(count):,}")
        lines.append("")

        lines.append("## Output Files")
        lines.append("")
        lines.append("- family_claim_distribution.csv")
        lines.append("- rank_claim_distribution.csv")
        lines.append("- judge_allowed_distribution.csv")
        lines.append("- family_rank_distribution.csv")
        lines.append("- source_presence_by_claim.csv")
        lines.append("- judge_input_integrity.csv")
        lines.append("- suspicious_bundles.csv")
        lines.append("- manual_review_sample.csv")
        lines.append("")

        (self.output_dir / "audit_summary.md").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bundle-csv",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.csv",
    )
    parser.add_argument(
        "--bundle-jsonl",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.jsonl",
    )
    parser.add_argument(
        "--judge-inputs-jsonl",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/bundle_judge_inputs.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/audit",
    )
    parser.add_argument("--sample-size", type=int, default=120)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = AuditConfig(
        bundle_csv=Path(args.bundle_csv),
        bundle_jsonl=Path(args.bundle_jsonl),
        judge_inputs_jsonl=Path(args.judge_inputs_jsonl),
        output_dir=Path(args.output_dir),
        sample_size=args.sample_size,
    )

    BundleAudit(config).run()


if __name__ == "__main__":
    main()
