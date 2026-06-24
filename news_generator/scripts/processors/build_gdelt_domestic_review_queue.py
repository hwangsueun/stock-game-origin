#!/usr/bin/env python3
"""Build the contemporaneous source-verification review queue (handoff item 4).

Reads the GKG domestic event ledger and turns it into a compact, human-reviewable
queue. An event is "corroborated" when at least ``--min-domains`` *independent*
news domains carried it on the same publication day -- that distinct-domain count
is the contemporaneous verification signal. Price reaction is never used.

The queue is capped per day and ranked so a reviewer can promote events
(item 5) without scrolling through tens of thousands of low-coverage spikes. The
full ledger stays untouched; this only selects and orders.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/normalized_events.jsonl"))
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg"))
    parser.add_argument("--min-domains", type=int, default=4,
                        help="independent domains required to enter the queue")
    parser.add_argument("--max-per-date", type=int, default=6)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.ledger_jsonl.open(encoding="utf-8") if line.strip()]

    verified = [r for r in rows if r["n_distinct_domains"] >= args.min_domains]
    # Independent-domain corroboration is the gate; re-affirm it explicitly so the
    # queue record carries the verification verdict.
    by_date = defaultdict(list)
    for r in verified:
        by_date[r["ref_date"]].append(r)

    queue = []
    for date, items in by_date.items():
        # high domestic-confidence first, then broadest independent coverage.
        items.sort(key=lambda r: (0 if r["domestic_confidence"] == "high" else 1,
                                   -r["n_distinct_domains"], r["salient_entity"]))
        for rank, r in enumerate(items[:args.max_per_date], start=1):
            queue.append({
                "event_id": r["event_id"],
                "ref_date": r["ref_date"],
                "available_date_conservative": r["available_date_conservative"],
                "salient_entity": r["salient_entity"],
                "domestic_confidence": r["domestic_confidence"],
                "theme_signature": r["theme_signature"],
                "n_distinct_domains": r["n_distinct_domains"],
                "n_articles": r["n_articles"],
                "independent_domains": r["source_domains"],
                "corroboration_verdict": "corroborated_multi_domain",
                "avg_tone": r["avg_tone"],
                "evidence": r["evidence"],
                "daily_priority_rank": rank,
                "generation_eligible": False,
                "verification_status": "requires_human_promotion",
            })
    queue.sort(key=lambda r: (r["ref_date"], r["daily_priority_rank"]))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "source_verification_queue.jsonl"
    with out.open("w", encoding="utf-8") as h:
        for r in queue:
            h.write(json.dumps(r, ensure_ascii=False) + "\n")

    high = sum(1 for r in queue if r["domestic_confidence"] == "high")
    years = Counter(r["ref_date"][:4] for r in queue)
    dates = {r["ref_date"] for r in queue}
    report = [
        "# GKG domestic source-verification queue", "",
        "Each row is an event corroborated by multiple *independent* news domains "
        "on its publication day (item 4). Promotion to generation (item 5) remains "
        "a separate human decision; price reaction is never used as a gate.", "",
        f"- input ledger candidates: {len(rows):,}",
        f"- corroborated (>= {args.min_domains} independent domains): {len(verified):,}",
        f"- queued (<= {args.max_per_date}/day): {len(queue):,}",
        f"- high domestic-confidence in queue: {high:,}",
        f"- distinct review dates: {len(dates):,}", "",
        "## Queue per year", "",
    ]
    report.extend(f"- {y}: {years[y]:,}" for y in sorted(years))
    (args.output_dir / "SOURCE_VERIFICATION_QUEUE_REPORT.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8")

    print(f"ledger={len(rows):,} corroborated={len(verified):,} "
          f"queued={len(queue):,} high_domestic={high:,} dates={len(dates):,}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
