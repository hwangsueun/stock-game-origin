#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pr05c0_attach_stock_code_to_stock_event_calendar.py

Purpose
-------
Attach stock_code to stock_event_calendar_2013_2023.csv before pr05c.

Input
-----
- stock_event_calendar_2013_2023.csv
- gdelt_stock_context_cards_judged_v3_all_fixed.csv

Output
------
- stock_event_calendar_2013_2023_with_stock_code.csv
- stock_event_calendar_2013_2023_with_stock_code_unmatched.csv
- stock_event_calendar_2013_2023_with_stock_code_report.txt

Strategy
--------
1. Build stock master from judged GDELT context:
   stock_code, stock_name
2. For each stock event row, search stock_name inside likely text columns.
3. If exactly one stock is matched, attach stock_code.
4. If multiple stocks are matched, explode into multiple rows.
5. If no stock is matched, save to unmatched file.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd


TEXT_COLUMN_CANDIDATES = [
    "stock_name",
    "종목명",
    "related_stock",
    "related_stocks",
    "related_assets",
    "asset",
    "assets",
    "event_name",
    "event_title",
    "title",
    "headline",
    "summary",
    "description",
    "detail",
    "content",
]


@dataclass(frozen=True)
class AttachStockCodeConfig:
    stock_event_csv: Path
    stock_master_csv: Path
    output_csv: Path
    unmatched_csv: Path
    report_txt: Path


class CsvReader:
    @staticmethod
    def read(path: Path) -> pd.DataFrame:
        encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]

        last_error: Optional[Exception] = None

        for enc in encodings:
            try:
                return pd.read_csv(path, encoding=enc)
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"CSV read failed: {path}, last_error={last_error}")


class StockCodeNormalizer:
    @staticmethod
    def normalize(value: Any) -> Optional[str]:
        if pd.isna(value):
            return None

        text = str(value).strip()
        text = re.sub(r"\.0$", "", text)

        match = re.search(r"(\d{6})", text)
        if match:
            return match.group(1)

        if text.isdigit():
            return text.zfill(6)

        return None


class StockMasterBuilder:
    def __init__(self, config: AttachStockCodeConfig):
        self.config = config

    def build(self) -> pd.DataFrame:
        df = CsvReader.read(self.config.stock_master_csv)

        if "stock_code" not in df.columns or "stock_name" not in df.columns:
            raise ValueError(
                "stock_master_csv must contain stock_code and stock_name columns."
            )

        master = df[["stock_code", "stock_name"]].copy()
        master["stock_code"] = master["stock_code"].apply(StockCodeNormalizer.normalize)
        master["stock_name"] = master["stock_name"].astype(str).str.strip()

        master = master[
            master["stock_code"].notna()
            & master["stock_name"].notna()
            & (master["stock_name"] != "")
            & (master["stock_name"].str.lower() != "nan")
        ].copy()

        master = master.drop_duplicates(["stock_code", "stock_name"]).copy()

        # Longer names first to reduce partial matching issues.
        master["name_len"] = master["stock_name"].str.len()
        master = master.sort_values(
            ["name_len", "stock_name"],
            ascending=[False, True],
            kind="mergesort",
        ).reset_index(drop=True)

        return master


class StockEventStockCodeAttacher:
    def __init__(self, config: AttachStockCodeConfig):
        self.config = config
        self.stock_master = StockMasterBuilder(config).build()

    def run(self) -> None:
        event_df = CsvReader.read(self.config.stock_event_csv)
        text_cols = self._resolve_text_columns(event_df)

        if not text_cols:
            raise ValueError(
                "No searchable text columns found in stock_event_csv. "
                f"Available columns: {list(event_df.columns)}"
            )

        attached_rows: List[Dict[str, Any]] = []
        unmatched_rows: List[Dict[str, Any]] = []
        multi_match_event_count = 0

        for idx, row in event_df.iterrows():
            matches = self._find_matches(row, text_cols)

            if not matches:
                unmatched = row.to_dict()
                unmatched["_source_row_index"] = idx
                unmatched["_match_status"] = "unmatched"
                unmatched_rows.append(unmatched)
                continue

            if len(matches) > 1:
                multi_match_event_count += 1

            for stock_code, stock_name, matched_text_col in matches:
                new_row = row.to_dict()
                new_row["stock_code"] = stock_code
                new_row["stock_name"] = stock_name
                new_row["_source_row_index"] = idx
                new_row["_matched_text_column"] = matched_text_col
                new_row["_match_status"] = (
                    "matched_single" if len(matches) == 1 else "matched_multi_exploded"
                )
                attached_rows.append(new_row)

        attached_df = pd.DataFrame(attached_rows)
        unmatched_df = pd.DataFrame(unmatched_rows)

        self.config.output_csv.parent.mkdir(parents=True, exist_ok=True)
        self.config.unmatched_csv.parent.mkdir(parents=True, exist_ok=True)
        self.config.report_txt.parent.mkdir(parents=True, exist_ok=True)

        attached_df.to_csv(self.config.output_csv, index=False, encoding="utf-8-sig")
        unmatched_df.to_csv(self.config.unmatched_csv, index=False, encoding="utf-8-sig")

        self._write_report(
            event_df=event_df,
            attached_df=attached_df,
            unmatched_df=unmatched_df,
            text_cols=text_cols,
            multi_match_event_count=multi_match_event_count,
        )

        print("=" * 100)
        print("[stock_event stock_code attach 완료]")
        print(f"input_rows: {len(event_df)}")
        print(f"attached_rows: {len(attached_df)}")
        print(f"unmatched_rows: {len(unmatched_df)}")
        print(f"multi_match_event_count: {multi_match_event_count}")
        print(f"output_csv: {self.config.output_csv}")
        print(f"unmatched_csv: {self.config.unmatched_csv}")
        print(f"report_txt: {self.config.report_txt}")
        print("=" * 100)

    def _resolve_text_columns(self, df: pd.DataFrame) -> List[str]:
        columns = set(df.columns)
        resolved = [col for col in TEXT_COLUMN_CANDIDATES if col in columns]

        # Fallback: object/string columns.
        if not resolved:
            for col in df.columns:
                if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
                    resolved.append(col)

        return resolved

    def _find_matches(
        self,
        row: pd.Series,
        text_cols: Sequence[str],
    ) -> List[Tuple[str, str, str]]:
        combined_parts: List[Tuple[str, str]] = []

        for col in text_cols:
            value = row.get(col)
            if pd.isna(value):
                continue

            text = str(value).strip()
            if not text or text.lower() == "nan":
                continue

            combined_parts.append((col, text))

        if not combined_parts:
            return []

        matches: List[Tuple[str, str, str]] = []
        seen_codes = set()

        for _, master_row in self.stock_master.iterrows():
            stock_code = str(master_row["stock_code"])
            stock_name = str(master_row["stock_name"])

            if stock_code in seen_codes:
                continue

            for col, text in combined_parts:
                if self._contains_stock_name(text, stock_name):
                    matches.append((stock_code, stock_name, col))
                    seen_codes.add(stock_code)
                    break

        return matches

    def _contains_stock_name(self, text: str, stock_name: str) -> bool:
        if not stock_name:
            return False

        # Exact substring match first.
        if stock_name in text:
            return True

        # Remove common spacing differences.
        compact_text = re.sub(r"\s+", "", text)
        compact_name = re.sub(r"\s+", "", stock_name)

        if compact_name and compact_name in compact_text:
            return True

        return False

    def _write_report(
        self,
        event_df: pd.DataFrame,
        attached_df: pd.DataFrame,
        unmatched_df: pd.DataFrame,
        text_cols: Sequence[str],
        multi_match_event_count: int,
    ) -> None:
        lines: List[str] = []

        lines.append("# stock_event_calendar stock_code attach report")
        lines.append("")
        lines.append("## Inputs")
        lines.append(f"- stock_event_csv: {self.config.stock_event_csv}")
        lines.append(f"- stock_master_csv: {self.config.stock_master_csv}")
        lines.append("")
        lines.append("## Outputs")
        lines.append(f"- output_csv: {self.config.output_csv}")
        lines.append(f"- unmatched_csv: {self.config.unmatched_csv}")
        lines.append("")
        lines.append("## Summary")
        lines.append(f"- input_event_rows: {len(event_df)}")
        lines.append(f"- attached_rows: {len(attached_df)}")
        lines.append(f"- unmatched_rows: {len(unmatched_df)}")
        lines.append(f"- matched_source_event_rows: {attached_df['_source_row_index'].nunique() if not attached_df.empty else 0}")
        lines.append(f"- unmatched_source_event_rows: {len(unmatched_df)}")
        lines.append(f"- multi_match_event_count: {multi_match_event_count}")
        lines.append(f"- stock_master_rows: {len(self.stock_master)}")
        lines.append("")
        lines.append("## Search Text Columns")
        for col in text_cols:
            lines.append(f"- {col}")
        lines.append("")

        if not attached_df.empty:
            lines.append("## Top Matched Stocks")
            top = (
                attached_df.groupby(["stock_code", "stock_name"], dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                .head(30)
            )
            for record in top.to_dict(orient="records"):
                lines.append(
                    f"- {record['stock_code']} {record['stock_name']}: {record['count']}"
                )
            lines.append("")

            lines.append("## Match Status Distribution")
            for key, value in attached_df["_match_status"].value_counts(dropna=False).items():
                lines.append(f"- {key}: {value}")
            lines.append("")

        if not unmatched_df.empty:
            lines.append("## Unmatched Sample")
            sample = unmatched_df.head(10)
            lines.append("```")
            lines.append(sample.to_string(index=False))
            lines.append("```")
            lines.append("")

        self.config.report_txt.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


class CliParser:
    @staticmethod
    def build() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Attach stock_code to stock_event_calendar before pr05c."
        )

        parser.add_argument(
            "--stock-event-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/market_event/"
                "stock_event_calendar_2013_2023.csv"
            ),
        )

        parser.add_argument(
            "--stock-master-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/news_generator/data/processed/gdelt_context/"
                "gdelt_stock_context_cards_judged_v3_all_fixed.csv"
            ),
        )

        parser.add_argument(
            "--output-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/market_event/"
                "stock_event_calendar_2013_2023_with_stock_code.csv"
            ),
        )

        parser.add_argument(
            "--unmatched-csv",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/market_event/"
                "stock_event_calendar_2013_2023_with_stock_code_unmatched.csv"
            ),
        )

        parser.add_argument(
            "--report-txt",
            type=Path,
            default=Path(
                "/Users/hgs/Desktop/IISE CD/data/raw/market_event/"
                "stock_event_calendar_2013_2023_with_stock_code_report.txt"
            ),
        )

        return parser


def build_config(args: argparse.Namespace) -> AttachStockCodeConfig:
    return AttachStockCodeConfig(
        stock_event_csv=args.stock_event_csv,
        stock_master_csv=args.stock_master_csv,
        output_csv=args.output_csv,
        unmatched_csv=args.unmatched_csv,
        report_txt=args.report_txt,
    )


def main() -> None:
    parser = CliParser.build()
    args = parser.parse_args()

    config = build_config(args)
    attacher = StockEventStockCodeAttacher(config)
    attacher.run()


if __name__ == "__main__":
    main()