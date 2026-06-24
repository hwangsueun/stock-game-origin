#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Attach conservative, same-day GDELT media context to stock news events.

Only medium/high-confidence sector signals with at least three underlying GKG
records are considered.  The theme's primary sector must match the stock's
year-specific business profile.  One best signal is emitted per event; this is
media measurement metadata, never evidence that the disclosure caused a move.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr05a_attach_gdelt_context_to_stocks import GdeltContextLoader  # noqa: E402

DEFAULT_GDELT = BASE_DIR / "data/processed/gdelt_context/gdelt_context_summary_v5_all_months.csv"

# The available yearly profiles cover these large-cap names, but automated tag
# extraction over-assigns conglomerates.  Use a reviewed business exposure
# allowlist so a subsidiary mention cannot attach an unrelated sector theme.
CURATED_SECTOR_TAGS = {
    "000210": {"sector_construction", "sector_chemical"},
    "000240": {"sector_auto", "sector_chemical"},
    "000270": {"sector_auto"},
    "000660": {"sector_semiconductor"},
    "000990": {"sector_semiconductor"},
    "002380": {"sector_construction", "sector_chemical"},
    "003490": {"sector_airline"},
    "003550": {"sector_technology"},
    "004000": {"sector_chemical"},
    "004800": {"sector_chemical"},
    "005380": {"sector_auto"},
    "006120": {"sector_chemical", "sector_bio_pharma"},
    "006400": {"sector_battery"},
    "006800": {"sector_financial"},
    "007700": {"sector_consumer"},
    "008770": {"sector_consumer"},
    "009150": {"sector_technology", "sector_semiconductor"},
    "010060": {"sector_chemical"},
    "010130": {"sector_steel"},
    "010950": {"sector_chemical"},
    "011170": {"sector_chemical"},
    "011200": {"sector_shipping"},
    "011780": {"sector_chemical"},
}


def iter_events(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if "body" in d:
            bp = json.loads(d["body"]["messages"][1]["content"])["brief_payload"]
            yield d["custom_id"], str(bp.get("stock_code", "")).zfill(6), bp.get("stock_name"), bp.get("anchor_date")
        else:
            yield d.get("bundle_id"), str(d.get("stock_code", "")).zfill(6), d.get("stock_name"), d.get("anchor_date")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events-jsonl", type=Path, required=True)
    ap.add_argument("--gdelt-summary-csv", type=Path, default=DEFAULT_GDELT)
    ap.add_argument("--out", type=Path, default=BASE_DIR / "data/interim/context_layers/gdelt_event_context.csv")
    ap.add_argument("--min-raw-count", type=int, default=3)
    args = ap.parse_args()

    events = list(iter_events(args.events_jsonl))
    gdelt = GdeltContextLoader.load(args.gdelt_summary_csv)
    gdelt = gdelt[
        gdelt["evidence_class"].eq("sector_linkable")
        & gdelt["confidence_level"].isin(["medium", "high"])
        & gdelt["raw_count"].ge(args.min_raw_count)
    ].copy()
    by_date = {date: group for date, group in gdelt.groupby("date", sort=False)}

    fields = ["custom_id", "stock_code", "stock_name", "anchor_date", "theme", "theme_label",
              "matched_sector_tags", "confidence_level", "stock_link_score", "raw_count",
              "weighted_count", "avg_tone", "source", "status", "reason"]
    counts = {"matched": 0, "no_match": 0, "no_profile": 0}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for cid, code, name, anchor in events:
            stock_tags = CURATED_SECTOR_TAGS.get(code)
            base = dict(custom_id=cid, stock_code=code, stock_name=name, anchor_date=anchor)
            if stock_tags is None:
                counts["no_profile"] += 1
                w.writerow({**base, "status": "no_profile", "reason": "not_in_curated_large_cap_allowlist"})
                continue

            candidates = []
            for _, row in by_date.get(anchor, pd.DataFrame()).iterrows():
                primary = set(row.get("_primary_hard_tags", set()))
                matched = primary & stock_tags
                if not matched:
                    continue
                score = float(row.get("stock_link_score", 0.0))
                raw_count = int(row.get("raw_count", 0))
                confidence_bonus = 1 if row.get("confidence_level") == "high" else 0
                candidates.append((confidence_bonus, score, raw_count, abs(float(row.get("avg_tone", 0.0))), row, matched))

            if not candidates:
                counts["no_match"] += 1
                w.writerow({**base, "status": "no_match", "reason": "no_strong_same_day_primary_sector_match"})
                continue

            _, _, _, _, row, matched = max(candidates, key=lambda x: x[:4])
            theme = str(row["theme"])
            theme_label = theme.replace(" 관련 보도 증가", "").replace(" 보도 증가", "")
            counts["matched"] += 1
            w.writerow({
                **base,
                "theme": theme,
                "theme_label": theme_label,
                "matched_sector_tags": "|".join(sorted(matched)),
                "confidence_level": row["confidence_level"],
                "stock_link_score": round(float(row["stock_link_score"]), 4),
                "raw_count": int(row["raw_count"]),
                "weighted_count": round(float(row["weighted_count"]), 2),
                "avg_tone": round(float(row["avg_tone"]), 2),
                "source": row["source"],
                "status": "matched",
                "reason": "same_day_primary_sector_profile_match",
            })

    print(f"[done] events={len(events)} eligible_gdelt_rows={len(gdelt)} status={counts} -> {args.out}")


if __name__ == "__main__":
    main()
