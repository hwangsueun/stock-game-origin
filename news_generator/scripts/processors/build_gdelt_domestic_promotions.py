#!/usr/bin/env python3
"""Promote contemporaneously-verified GKG events to generation eligibility (item 5).

A candidate is promoted to ``generation_eligible=true`` only when it clears a
strict, price-independent gate:

  * it is a *discrete event day* for its entity, not just routine coverage. A
    persistent entity (e.g. the National Assembly, which is in the news almost
    every day) only counts on days where its independent-domain coverage spikes
    above its own historical level; an episodic entity counts whenever it clears
    the absolute floor, because its mere appearance is the event,
  * corroborated by >= ``--promote-min-domains`` independent news domains on the
    publication day (the contemporaneous verification),
  * high domestic-confidence entity,
  * a non-empty GKG theme signature (atomic event facts present),
  * a clean entity label (no garbled / generic one-token extractions),
  * at most ``--max-per-date`` promotions per day (keeps the game timeline from
    being flooded by a single high-news day).

Price reaction is never consulted. This writes an additive ``promoted_events``
file; the source ledger is left untouched so the gate can be re-run / tightened.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path


# Entity labels that are too garbled or generic to anchor a discrete event.
_BAD_PREFIX = ("us ", "the ", "a ", "an ", "and ", "of ", "uns ")
_BAD_ENTITY = {
    "market committee", "security council", "supreme court", "high court",
    "district court", "central bank", "ministry", "government", "parliament",
    "national police", "police", "prosecution", "committee", "administration",
}


def _clean_entity(entity: str) -> bool:
    el = entity.lower().strip()
    if len(el) < 3:
        return False
    if el in _BAD_ENTITY:
        return False
    if el.startswith(_BAD_PREFIX):
        return False
    if not re.search(r"[a-z]", el):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/normalized_events.jsonl"))
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg"))
    parser.add_argument("--promote-min-domains", type=int, default=6)
    parser.add_argument("--max-per-date", type=int, default=3)
    parser.add_argument("--persistent-day-count", type=int, default=40,
                        help="entities active on >= this many days are 'persistent' "
                             "and need a spike to count as an event day")
    parser.add_argument("--spike-multiple", type=float, default=1.5,
                        help="persistent-entity event day needs domains >= this "
                             "multiple of the entity's median daily coverage")
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.ledger_jsonl.open(encoding="utf-8") if line.strip()]

    # Per-entity baseline so routine coverage is separated from event spikes.
    per_entity_domains: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        per_entity_domains[r["salient_entity"]].append(r["n_distinct_domains"])
    entity_day_count = {e: len(v) for e, v in per_entity_domains.items()}
    entity_median = {e: statistics.median(v) for e, v in per_entity_domains.items()}

    def is_discrete_event(r: dict) -> tuple[bool, float]:
        ent = r["salient_entity"]
        nd = r["n_distinct_domains"]
        med = entity_median[ent] or 1
        ratio = nd / med
        if entity_day_count[ent] >= args.persistent_day_count:
            # persistent entity: require a real spike over its own baseline
            return (nd >= args.spike_multiple * med, ratio)
        # episodic entity: appearance at the floor is itself the event
        return (True, ratio)

    eligible = []
    for r in rows:
        if r["n_distinct_domains"] < args.promote_min_domains:
            continue
        if r["domestic_confidence"] != "high" or not r["theme_signature"]:
            continue
        if not _clean_entity(r["salient_entity"]):
            continue
        spike, ratio = is_discrete_event(r)
        if not spike:
            continue
        r = {**r, "entity_day_count": entity_day_count[r["salient_entity"]],
             "coverage_spike_ratio": round(ratio, 2)}
        eligible.append(r)

    by_date = defaultdict(list)
    for r in eligible:
        by_date[r["ref_date"]].append(r)

    promoted = []
    for date, items in by_date.items():
        # most anomalous coverage first, then broadest independent coverage.
        items.sort(key=lambda r: (-r["coverage_spike_ratio"],
                                  -r["n_distinct_domains"], r["salient_entity"]))
        for r in items[:args.max_per_date]:
            promoted.append({
                **r,
                "generation_eligible": True,
                "verification_status": "contemporaneously_verified_multi_domain",
                "promotion_rule": (
                    f"domains>={args.promote_min_domains} & high_domestic & "
                    f"clean_entity & theme_present & top{args.max_per_date}_per_day"),
                "promotion_basis": "contemporaneous_independent_domains_no_price",
            })
    promoted.sort(key=lambda r: (r["ref_date"], -r["n_distinct_domains"]))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "promoted_events.jsonl"
    with out.open("w", encoding="utf-8") as h:
        for r in promoted:
            h.write(json.dumps(r, ensure_ascii=False) + "\n")

    years = Counter(r["ref_date"][:4] for r in promoted)
    ents = Counter(r["salient_entity"] for r in promoted)
    dates = {r["ref_date"] for r in promoted}
    report = [
        "# GKG promoted events (generation-eligible)", "",
        "Events that cleared the strict contemporaneous-verification gate "
        "(item 5). `generation_eligible=true`. No price reaction was used.", "",
        f"- ledger candidates: {len(rows):,}",
        f"- cleared gate (discrete-event spike, domains>={args.promote_min_domains}, "
        f"high-domestic, clean entity, theme present): {len(eligible):,}",
        f"- promoted (<= {args.max_per_date}/day): {len(promoted):,}",
        f"- distinct dates: {len(dates):,}",
        f"- persistent-entity spike gate: >= {args.spike_multiple}x own median "
        f"coverage (entities active >= {args.persistent_day_count} days)", "",
        "## Promoted per year", "",
    ]
    report.extend(f"- {y}: {years[y]:,}" for y in sorted(years))
    report += ["", "## Most-promoted entities (ongoing-coverage check)", ""]
    report.extend(f"- {e}: {c}" for e, c in ents.most_common(20))
    (args.output_dir / "PROMOTED_EVENTS_REPORT.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8")

    print(f"ledger={len(rows):,} cleared_gate={len(eligible):,} "
          f"promoted={len(promoted):,} dates={len(dates):,}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
