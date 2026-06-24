#!/usr/bin/env python3
"""Audit real-world event reaction candidates before source verification."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from build_realworld_event_reaction_candidates import ASSETS, COUNTRY_EXPOSURES


FORBIDDEN_EVENT_FIELDS = {
    "source_article", "source_headline", "source_original", "article_text", "raw_text",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-jsonl", type=Path, required=True)
    parser.add_argument("--reactions-csv", type=Path, required=True)
    args = parser.parse_args()

    failures = Counter()
    events = {}
    with args.events_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            event_id = event.get("event_id")
            if not event_id or event_id in events:
                failures["duplicate_or_empty_event_id"] += 1
                continue
            events[event_id] = event
            if FORBIDDEN_EVENT_FIELDS & set(event):
                failures["source_text_persisted"] += 1
            if event.get("license") not in {"CC-BY-4.0", "CC0-1.0"}:
                failures["unexpected_license"] += 1
            if event.get("generation_eligible") is not False:
                failures["event_prematurely_generation_eligible"] += 1
            expected = COUNTRY_EXPOSURES.get(event.get("country"))
            expected_assets = set(expected[1]) if expected else set()
            if set(event.get("predeclared_assets") or []) != expected_assets:
                failures["invalid_predeclared_assets"] += 1

    pairs = set()
    review_windows = set()
    with args.reactions_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            event = events.get(row.get("event_id"))
            if event is None:
                failures["reaction_missing_event"] += 1
                continue
            pair = (row["event_id"], row["asset_id"])
            if pair in pairs:
                failures["duplicate_event_asset_pair"] += 1
            pairs.add(pair)
            if row["asset_id"] not in event["predeclared_assets"]:
                failures["post_hoc_asset_mapping"] += 1
            if row["asset_id"] not in ASSETS:
                failures["unknown_asset"] += 1
            if row["reaction_date"] < row["available_date_conservative"]:
                failures["reaction_before_availability"] += 1
            if row.get("causal_claim_allowed") != "False":
                failures["causal_claim_allowed"] += 1
            if row.get("generation_eligible") != "False":
                failures["reaction_prematurely_generation_eligible"] += 1
            if row.get("review_candidate") == "True":
                window = (row["reaction_date"], row["asset_id"])
                if window in review_windows:
                    failures["multiple_review_candidates_same_window"] += 1
                review_windows.add(window)
                if row.get("material_reaction") != "True":
                    failures["review_candidate_below_threshold"] += 1
                ratio = row.get("dominance_ratio")
                if int(row["same_window_candidate_count"]) > 1 and (
                    not ratio or float(ratio) < 2.0
                ):
                    failures["review_candidate_not_dominant"] += 1

    result = {
        "status": "PASS" if not failures else "FAIL",
        "events": len(events),
        "event_asset_pairs": len(pairs),
        "review_windows": len(review_windows),
        "failures": dict(failures),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
