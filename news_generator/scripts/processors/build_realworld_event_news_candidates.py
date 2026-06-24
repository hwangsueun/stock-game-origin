#!/usr/bin/env python3
"""Build a price-independent review queue for political and social events."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


PRICE_FIELDS = {
    "return_1d_pct", "return_zscore", "material_reaction", "reaction_date",
    "asset_id", "prior_60d_vol_pct",
}


def read_jsonl(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    return rows


def newsworthiness(event: dict) -> tuple[int, list[str]]:
    reasons = []
    if event.get("source") == "UCDP_GED":
        fatalities = int(event.get("fatalities_best") or 0)
        score = 0
        for threshold, value in ((1000, 100), (250, 90), (100, 80), (50, 70), (25, 60)):
            if fatalities >= threshold:
                score = value
                reasons.append(f"fatalities_best>={threshold}")
                break
        if event.get("event_type") == "one_sided_violence":
            score += 5
            reasons.append("civilian_targeting")
        if int(event.get("civilian_fatalities") or 0) >= 25:
            score += 5
            reasons.append("civilian_fatalities>=25")
        return min(score, 100), reasons

    if event.get("source") == "Wikidata":
        base = {
            "coup_detat": 90,
            "terrorist_attack": 80,
            "natural_disaster": 75,
            "major_accident": 75,
            "referendum": 65,
            "protest": 60,
            "strike": 60,
            "election": 55,
        }.get(event.get("event_type"), 40)
        reasons = [f"wikidata_event_type:{event.get('event_type')}"]
        if event.get("country") == "South Korea":
            base += 20
            reasons.append("domestic_priority")
        return min(base, 100), reasons

    if event.get("source") == "GDELT_GKG":
        # Only contemporaneously-verified, label-cleaned events are scorable.
        if not event.get("label_generation_ready"):
            return 0, ["label_not_generation_ready"]
        domains = int(event.get("n_distinct_domains") or 0)
        ratio = float(event.get("coverage_spike_ratio") or 1.0)
        score = min(50 + domains, 95)
        reasons = [f"independent_domains={domains}", f"coverage_spike_ratio={ratio:.1f}"]
        if event.get("domestic_confidence") == "high":
            score = min(score + 3, 98)
            reasons.append("domestic_high_confidence")
        return score, reasons

    return 0, ["unsupported_source"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-jsonl", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-score", type=int, default=55)
    parser.add_argument("--max-per-date", type=int, default=3)
    args = parser.parse_args()

    events = read_jsonl(args.events_jsonl)
    candidates = []
    seen = set()
    for event in events:
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        if PRICE_FIELDS & set(event):
            raise ValueError(f"price field leaked into event selection: {event['event_id']}")
        score, reasons = newsworthiness(event)
        if score < args.min_score:
            continue
        candidates.append({
            **event,
            "is_domestic": event.get("country") == "South Korea",
            "newsworthiness_score": score,
            "newsworthiness_reasons": reasons,
            "selection_basis": "event_facts_only_no_price_reaction",
            # Preserve each source's own verification verdict: retrospective
            # sources (UCDP, Wikidata) arrive False and stay pending; GKG events
            # that already cleared contemporaneous multi-domain promotion stay True.
            "generation_eligible": bool(event.get("generation_eligible", False)),
        })

    by_date = defaultdict(list)
    for event in candidates:
        by_date[event["available_date_conservative"]].append(event)
    daily_queue = []
    for date, rows in by_date.items():
        rows.sort(key=lambda row: (-row["newsworthiness_score"], row["event_id"]))
        domestic = [row for row in rows if row["is_domestic"]]
        selected = domestic[:1]
        selected_ids = {row["event_id"] for row in selected}
        selected.extend(
            row for row in rows
            if row["event_id"] not in selected_ids
        )
        selected = selected[:args.max_per_date]
        for rank, row in enumerate(selected, start=1):
            daily_queue.append({**row, "daily_priority_rank": rank})
    daily_queue.sort(key=lambda row: (row["available_date_conservative"], row["daily_priority_rank"]))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("all_candidates.jsonl", candidates), ("daily_review_queue.jsonl", daily_queue)):
        with (args.output_dir / name).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    source_counts = Counter(row["source"] for row in daily_queue)
    gen_eligible = sum(1 for row in daily_queue if row.get("generation_eligible"))
    report = [
        "# Real-world event news candidate queue", "",
        f"- input events: {len(events):,}",
        f"- candidates above score: {len(candidates):,}",
        f"- daily review queue: {len(daily_queue):,}",
        f"- dates covered: {len(by_date):,}",
        f"- generation-eligible in queue (GKG contemporaneously verified): {gen_eligible:,}",
        "- selection uses event facts only; price reactions are excluded",
        "- retrospective sources (UCDP, Wikidata) stay pending verification",
        "", "## Queue by source", "",
    ]
    report.extend(f"- {source}: {count:,}" for source, count in source_counts.items())
    (args.output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"input_events={len(events):,}")
    print(f"candidates={len(candidates):,}")
    print(f"daily_review_queue={len(daily_queue):,}")


if __name__ == "__main__":
    main()
