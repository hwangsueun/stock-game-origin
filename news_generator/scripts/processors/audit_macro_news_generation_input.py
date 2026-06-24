#!/usr/bin/env python3
"""Audit the daily macro-news generation input before paid LLM generation."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


BLOCKED_REFERENCE_DATE_SOURCES = {
    "dubai_oil",
    "dubai_oil_price",
    "cpi",
    "leading_index",
    "export_amount",
    "export_amount_usd_thousand",
    "import_amount",
    "import_amount_usd_thousand",
    "trade_balance",
    "trade_balance_usd_thousand",
    "industrial_production",
    "industrial_production_index",
    "mining_manufacturing_production",
    "mining_manufacturing_production_index",
    "retail_sales",
    "retail_sales_index",
    "facility_investment",
    "facility_investment_index",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--news-per-day", type=int, default=10)
    args = parser.parse_args()

    failures = Counter()
    days = 0
    events_total = 0
    seen_event_ids: set[str] = set()
    sector_breadth_total = 0
    major_stock_total = 0
    official_release_total = 0

    with args.input_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            days += 1
            record = json.loads(line)
            events = record.get("macro_events") or []
            events_total += len(events)

            if record.get("news_count_target") != args.news_per_day:
                failures["wrong_target"] += 1
            if len(events) != args.news_per_day:
                failures["wrong_event_count"] += 1

            breadth_events = [event for event in events if "sector_breadth" in str(event.get("event_id"))]
            major_events = [event for event in events if "major_stock" in str(event.get("event_id"))]
            official_events = [
                event for event in events
                if "official_release_calendar" in set(event.get("source_columns") or [])
            ]
            sector_breadth_total += len(breadth_events)
            major_stock_total += len(major_events)
            official_release_total += len(official_events)
            if len(breadth_events) > 2:
                failures["too_many_sector_breadth_events"] += 1
            if len(major_events) > 1:
                failures["too_many_major_stock_events"] += 1

            for event in breadth_events:
                evidence = event.get("evidence") or {}
                component_sum = sum(int(evidence.get(key) or 0) for key in ("advancers", "decliners", "unchanged"))
                if component_sum != int(evidence.get("sector_count") or -1):
                    failures["sector_breadth_count_mismatch"] += 1

            for event in major_events:
                evidence = event.get("evidence") or {}
                if event.get("event_role") != "headline":
                    failures["major_stock_not_headline"] += 1
                if "시장 전체 원인으로 확대하지 않음" not in str(evidence.get("usage_rule") or ""):
                    failures["major_stock_missing_usage_rule"] += 1

            for event in official_events:
                evidence = event.get("evidence") or {}
                if event.get("event_role") != "headline":
                    failures["official_release_not_headline"] += 1
                if evidence.get("verification_status") != "official_source_verified":
                    failures["official_release_not_verified"] += 1
                if not str(evidence.get("source_url") or "").startswith("https://"):
                    failures["official_release_missing_url"] += 1
                if not evidence.get("required_attribution"):
                    failures["official_release_missing_attribution"] += 1
                if not evidence.get("allowed_release_verbs"):
                    failures["official_release_missing_verbs"] += 1
                if not evidence.get("source_release_date"):
                    failures["official_release_missing_source_date"] += 1

            for event in events:
                event_id = str(event.get("event_id") or "")
                if not event_id or event_id in seen_event_ids:
                    failures["blank_or_duplicate_event_id"] += 1
                seen_event_ids.add(event_id)

                sources = set(event.get("source_columns") or [])
                if not sources:
                    failures["empty_sources"] += 1
                if sources & BLOCKED_REFERENCE_DATE_SOURCES:
                    failures["reference_date_source_exposed"] += 1
                if "fallback_generic" in event_id:
                    failures["generic_fallback"] += 1
                if not event.get("key_figures"):
                    failures["empty_key_figures"] += 1

    result = {
        "status": "PASS" if not failures else "FAIL",
        "days": days,
        "events": events_total,
        "sector_breadth_events": sector_breadth_total,
        "major_stock_events": major_stock_total,
        "official_release_events": official_release_total,
        "news_per_day": args.news_per_day,
        "failures": dict(failures),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
