#!/usr/bin/env python3
"""Audit that event-news selection is independent of future asset prices."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


PRICE_FIELDS = {
    "return_1d_pct", "return_zscore", "material_reaction", "reaction_date",
    "asset_id", "prior_60d_vol_pct", "review_candidate",
}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-candidates", type=Path, required=True)
    parser.add_argument("--daily-queue", type=Path, required=True)
    parser.add_argument("--max-per-date", type=int, default=3)
    args = parser.parse_args()

    all_candidates = load(args.all_candidates)
    daily_queue = load(args.daily_queue)
    failures = Counter()
    candidates_by_date = defaultdict(list)
    queue_by_date = defaultdict(list)

    for row in all_candidates:
        candidates_by_date[row["available_date_conservative"]].append(row)
        if PRICE_FIELDS & set(row):
            failures["price_field_in_selection"] += 1
        if row.get("selection_basis") != "event_facts_only_no_price_reaction":
            failures["invalid_selection_basis"] += 1
        if row.get("generation_eligible") is not False:
            failures["premature_generation_eligibility"] += 1

    seen = set()
    for row in daily_queue:
        queue_by_date[row["available_date_conservative"]].append(row)
        if row["event_id"] in seen:
            failures["duplicate_queue_event"] += 1
        seen.add(row["event_id"])
    for date, rows in queue_by_date.items():
        if len(rows) > args.max_per_date:
            failures["daily_cap_exceeded"] += 1
        source_rows = candidates_by_date[date]
        if any(row.get("is_domestic") for row in source_rows) and not any(
            row.get("is_domestic") for row in rows
        ):
            failures["domestic_candidate_not_reserved"] += 1

    result = {
        "status": "PASS" if not failures else "FAIL",
        "all_candidates": len(all_candidates),
        "daily_queue": len(daily_queue),
        "dates": len(queue_by_date),
        "failures": dict(failures),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
