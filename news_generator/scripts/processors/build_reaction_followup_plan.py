#!/usr/bin/env python3
"""Build a deterministic plan for split market-reaction follow-up articles.

The plan is conservative about attribution. A five-session reaction is not used
when another selected disclosure for the same stock falls inside that window.
Such a row is downgraded to an unconfounded next-session metric when that metric
is independently material; otherwise it is excluded.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


def read_requests(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        request = json.loads(line)
        payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
        events.append(
            {
                "custom_id": request["custom_id"],
                "stock_code": str(payload["stock_code"]).zfill(6),
                "stock_name": payload["stock_name"],
                "anchor_date": payload["anchor_date"],
                "event_family": payload["event_family"],
                "action_type": payload.get("action_type", ""),
                "target_news_id": payload.get("target_news_id", ""),
                "detail_source_facts_ko": payload.get("detail_source_facts_ko", []),
            }
        )
    return events


def initial_horizon(reason: str) -> str:
    if "ret5d" in reason:
        return "5d"
    if "ret1d" in reason:
        return "1d"
    return "volume_1d"


def choose_metric(row: dict[str, str], horizon: str) -> dict[str, Any]:
    if horizon == "5d":
        return {
            "chosen_horizon": "5d",
            "publish_date": row["date_5d"],
            "stock_return_pct": float(row["ret_5d"]),
            "volume_vs_20d_avg_mult": None,
        }
    if horizon == "1d":
        return {
            "chosen_horizon": "1d",
            "publish_date": row["date_1d"],
            "stock_return_pct": float(row["ret_1d"]),
            "volume_vs_20d_avg_mult": None,
        }
    return {
        "chosen_horizon": "volume_1d",
        "publish_date": row["date_1d"],
        "stock_return_pct": None,
        "volume_vs_20d_avg_mult": float(row["vol_mult"]),
    }


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-jsonl", type=Path, required=True)
    parser.add_argument("--price-csv", type=Path, required=True)
    parser.add_argument("--plan-out", type=Path, required=True)
    parser.add_argument("--exclusions-out", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--expect-followups", type=int)
    parser.add_argument("--expect-no-price", type=int)
    args = parser.parse_args()

    events = read_requests(args.requests_jsonl)
    event_by_id = {event["custom_id"]: event for event in events}
    events_by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_stock[event["stock_code"]].append(event)

    with args.price_csv.open(encoding="utf-8-sig") as handle:
        price_by_id = {row["custom_id"]: row for row in csv.DictReader(handle)}

    exclusions: list[dict[str, Any]] = []
    for event in events:
        if event["custom_id"] not in price_by_id:
            exclusions.append(
                {
                    "status": "excluded",
                    "reason": "no_price_context",
                    **event,
                }
            )

    candidates: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    for custom_id, event in event_by_id.items():
        price = price_by_id.get(custom_id)
        if not price or price["material"].lower() != "true":
            continue

        horizon = initial_horizon(price["material_reason"])
        metric = choose_metric(price, horizon)
        intervening: list[dict[str, Any]] = []
        if horizon == "5d":
            start = date.fromisoformat(event["anchor_date"])
            end = date.fromisoformat(price["date_5d"])
            intervening = [
                other
                for other in events_by_stock[event["stock_code"]]
                if other["custom_id"] != custom_id
                and start < date.fromisoformat(other["anchor_date"]) <= end
            ]

        decision = "retained"
        if intervening:
            next_day = date.fromisoformat(price["date_1d"])
            next_day_confounded = any(
                date.fromisoformat(other["anchor_date"]) <= next_day for other in intervening
            )
            ret1_material = abs(float(price["ret_1d"])) >= 5.0
            volume_material = float(price["vol_mult"]) >= 3.0
            if not next_day_confounded and ret1_material:
                metric = choose_metric(price, "1d")
                decision = "downgraded_to_1d"
            elif not next_day_confounded and volume_material:
                metric = choose_metric(price, "volume_1d")
                decision = "downgraded_to_volume_1d"
            else:
                exclusions.append(
                    {
                        "status": "excluded",
                        "reason": "confounded_5d_window",
                        "source_custom_ids": [custom_id],
                        "stock_code": event["stock_code"],
                        "stock_name": event["stock_name"],
                        "anchor_date": event["anchor_date"],
                        "initial_horizon": horizon,
                        "initial_publish_date": price["date_5d"],
                        "intervening_custom_ids": [x["custom_id"] for x in intervening],
                        "intervening_anchor_dates": [x["anchor_date"] for x in intervening],
                    }
                )
                decision_counts["dropped_confounded_5d"] += 1
                continue

        candidates.append(
            {
                "status": "planned",
                "source_custom_ids": [custom_id],
                "stock_code": event["stock_code"],
                "stock_name": event["stock_name"],
                "anchor_date": event["anchor_date"],
                "trade_date": price["trade_date"],
                "event_families": [event["event_family"]],
                "source_events": [event],
                "initial_horizon": horizon,
                "decision": decision,
                "material_reasons": [price["material_reason"]],
                "intervening_custom_ids": [x["custom_id"] for x in intervening],
                **metric,
            }
        )
        decision_counts[decision] += 1

    # One price observation must produce one article even when several disclosures
    # for the same stock share the observation day.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate["stock_code"], candidate["anchor_date"])].append(candidate)

    plan: list[dict[str, Any]] = []
    merged_groups = 0
    for (stock_code, anchor_date), group in sorted(grouped.items()):
        group.sort(key=lambda row: row["source_custom_ids"][0])
        first = group[0]
        if len(group) == 1:
            first["plan_id"] = first["source_custom_ids"][0] + "__reaction_followup"
            plan.append(first)
            continue
        signatures = {
            (row["chosen_horizon"], row["publish_date"], row["stock_return_pct"], row["volume_vs_20d_avg_mult"])
            for row in group
        }
        if len(signatures) != 1:
            raise ValueError(f"same-day candidates have incompatible metrics: {group}")
        merged_groups += 1
        plan.append(
            {
                **first,
                "plan_id": f"market_reaction__{stock_code}__{anchor_date.replace('-', '')}",
                "source_custom_ids": [cid for row in group for cid in row["source_custom_ids"]],
                "event_families": [family for row in group for family in row["event_families"]],
                "source_events": [source for row in group for source in row["source_events"]],
                "material_reasons": [reason for row in group for reason in row["material_reasons"]],
                "decision": "merged_same_day_events",
            }
        )

    plan.sort(key=lambda row: (row["publish_date"], row["stock_code"], row["source_custom_ids"]))
    exclusions.sort(
        key=lambda row: (
            row.get("anchor_date", ""),
            row.get("stock_code", ""),
            row.get("custom_id", row.get("source_custom_ids", [""])[0]),
        )
    )
    no_price_count = sum(row["reason"] == "no_price_context" for row in exclusions)
    summary = {
        "request_count": len(events),
        "price_matched_count": sum(event["custom_id"] in price_by_id for event in events),
        "material_source_count": sum(
            price_by_id.get(event["custom_id"], {}).get("material", "").lower() == "true"
            for event in events
        ),
        "followup_article_count": len(plan),
        "followup_source_count": sum(len(row["source_custom_ids"]) for row in plan),
        "merged_same_day_group_count": merged_groups,
        "decision_counts_before_merge": dict(sorted(decision_counts.items())),
        "excluded_confounded_5d_count": sum(
            row["reason"] == "confounded_5d_window" for row in exclusions
        ),
        "no_price_context_count": no_price_count,
        "exclusion_count": len(exclusions),
    }

    if args.expect_followups is not None and len(plan) != args.expect_followups:
        raise SystemExit(f"expected {args.expect_followups} followups, got {len(plan)}")
    if args.expect_no_price is not None and no_price_count != args.expect_no_price:
        raise SystemExit(f"expected {args.expect_no_price} no-price rows, got {no_price_count}")

    dump_jsonl(args.plan_out, plan)
    dump_jsonl(args.exclusions_out, exclusions)
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
