#!/usr/bin/env python3
"""Audit the event-level GDELT sidecar without treating it as article evidence."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any


WRITER_LEAK_MARKERS = (
    "gdelt",
    "avg_tone",
    "raw_count",
    "weighted_count",
    "평균 톤",
    "관련 보도 증가",
)


def read_events(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        request = json.loads(line)
        payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
        result[request["custom_id"]] = {
            "stock_code": str(payload["stock_code"]).zfill(6),
            "stock_name": payload["stock_name"],
            "anchor_date": payload["anchor_date"],
        }
    return result


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p25": percentile(values, 0.25),
        "median": median(values) if values else None,
        "p75": percentile(values, 0.75),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def duplicate_summary(rows: list[dict[str, str]], fields: tuple[str, ...]) -> dict[str, int]:
    counts = Counter(tuple(row[field] for field in fields) for row in rows)
    duplicates = [count for count in counts.values() if count > 1]
    return {
        "duplicate_group_count": len(duplicates),
        "rows_in_duplicate_groups": sum(duplicates),
        "duplicate_excess_count": sum(count - 1 for count in duplicates),
        "max_group_size": max(duplicates, default=1),
    }


def inspect_writer_requests(paths: list[Path]) -> list[dict[str, Any]]:
    results = []
    for path in paths:
        row_count = 0
        marker_rows = 0
        structured_context_rows = 0
        marker_examples = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row_count += 1
            request = json.loads(line)
            lowered = line.lower()
            if any(marker.lower() in lowered for marker in WRITER_LEAK_MARKERS):
                marker_rows += 1
                if len(marker_examples) < 5:
                    marker_examples.append(request.get("custom_id", ""))
            try:
                payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            market_context = payload.get("market_context") or {}
            if "gdelt_context" in payload or "gdelt_context" in market_context:
                structured_context_rows += 1
        results.append(
            {
                "path": str(path),
                "row_count": row_count,
                "rows_with_gdelt_or_measurement_markers": marker_rows,
                "rows_with_structured_gdelt_context": structured_context_rows,
                "marker_examples": marker_examples,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gdelt-event-csv", type=Path, required=True)
    parser.add_argument("--events-jsonl", type=Path, required=True)
    parser.add_argument("--stock-universe-csv", type=Path, required=True)
    parser.add_argument("--writer-requests-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    args = parser.parse_args()

    events = read_events(args.events_jsonl)
    with args.gdelt_event_csv.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    with args.stock_universe_csv.open(encoding="utf-8-sig") as handle:
        universe_rows = list(csv.DictReader(handle))

    universe_codes = {str(row["stock_code"]).zfill(6) for row in universe_rows}
    sidecar_codes = {row["stock_code"] for row in rows}
    matched = [row for row in rows if row["status"] == "matched"]
    matched_codes = {row["stock_code"] for row in matched}
    status_counts = Counter(row["status"] for row in rows)
    status_stock_counts = {
        status: len({row["stock_code"] for row in rows if row["status"] == status})
        for status in sorted(status_counts)
    }
    profiled_codes = {row["stock_code"] for row in rows if row["status"] != "no_profile"}

    orphan_ids = [row["custom_id"] for row in rows if row["custom_id"] not in events]
    missing_ids = sorted(set(events) - {row["custom_id"] for row in rows})
    stock_mismatches = []
    date_mismatches = []
    name_mismatches = []
    invalid_dates = []
    for row in rows:
        event = events.get(row["custom_id"])
        if event:
            if row["stock_code"] != event["stock_code"]:
                stock_mismatches.append(row["custom_id"])
            if row["anchor_date"] != event["anchor_date"]:
                date_mismatches.append(row["custom_id"])
            if row["stock_name"] != event["stock_name"]:
                name_mismatches.append(row["custom_id"])
        try:
            date.fromisoformat(row["anchor_date"])
        except ValueError:
            invalid_dates.append(row["custom_id"])

    raw_counts = [float(row["raw_count"]) for row in matched]
    weighted_counts = [float(row["weighted_count"]) for row in matched]
    tones = [float(row["avg_tone"]) for row in matched]
    scores = [float(row["stock_link_score"]) for row in matched]
    q1 = percentile(raw_counts, 0.25) or 0.0
    q3 = percentile(raw_counts, 0.75) or 0.0
    raw_outlier_threshold = q3 + 1.5 * (q3 - q1)

    extreme_raw = sorted(
        [row for row in matched if float(row["raw_count"]) > raw_outlier_threshold],
        key=lambda row: float(row["raw_count"]),
        reverse=True,
    )
    extreme_tone = sorted(
        [row for row in matched if abs(float(row["avg_tone"])) >= 5.0],
        key=lambda row: abs(float(row["avg_tone"])),
        reverse=True,
    )
    invalid_numeric = []
    for row in matched:
        try:
            raw = float(row["raw_count"])
            weighted = float(row["weighted_count"])
            tone = float(row["avg_tone"])
            score = float(row["stock_link_score"])
            if raw < 3 or weighted < 0 or not -100 <= tone <= 100 or not 0 <= score <= 1:
                invalid_numeric.append(row["custom_id"])
        except ValueError:
            invalid_numeric.append(row["custom_id"])

    theme_increase_claims = [row for row in matched if "보도 증가" in row["theme"]]
    theme_labels_with_interpretation = [row for row in matched if "업황" in row["theme_label"]]
    injection_risk_samples = []
    for row in sorted(matched, key=lambda item: float(item["raw_count"]), reverse=True)[:5]:
        injection_risk_samples.append(
            {
                "custom_id": row["custom_id"],
                "sidecar_theme": row["theme"],
                "unsafe_hypothetical_rendering": (
                    f"{row['theme_label']} 관련 보도 {row['raw_count']}건, "
                    f"평균 톤 {row['avg_tone']}."
                ),
                "risk": "unnatural measurement prose; raw_count is not verified unique-article count",
            }
        )
    writer_audit = inspect_writer_requests(args.writer_requests_jsonl)

    report = {
        "policy": {
            "writer_usage": "forbidden",
            "allowed_usage": "internal_sidecar_only",
            "reason": "GDELT counts and tone are media measurements, not event facts or causal evidence",
        },
        "inputs": {
            "gdelt_event_csv": str(args.gdelt_event_csv),
            "events_jsonl": str(args.events_jsonl),
            "stock_universe_csv": str(args.stock_universe_csv),
        },
        "coverage": {
            "event_count": len(events),
            "sidecar_row_count": len(rows),
            "universe_stock_count": len(universe_codes),
            "sidecar_stock_count": len(sidecar_codes),
            "sidecar_universe_coverage_pct": round(100 * len(sidecar_codes & universe_codes) / len(universe_codes), 2),
            "matched_event_count": len(matched),
            "matched_event_coverage_pct": round(100 * len(matched) / len(events), 2),
            "matched_stock_count": len(matched_codes),
            "matched_stock_coverage_pct": round(100 * len(matched_codes & universe_codes) / len(universe_codes), 2),
            "status_counts": dict(status_counts),
            "status_distinct_stock_counts": status_stock_counts,
            "profiled_stock_count": len(profiled_codes),
            "profiled_stock_coverage_pct": round(100 * len(profiled_codes & universe_codes) / len(universe_codes), 2),
            "universe_stocks_absent_from_sidecar": sorted(universe_codes - sidecar_codes),
            "universe_stocks_without_match_count": len(universe_codes - matched_codes),
        },
        "event_integrity": {
            "orphan_sidecar_id_count": len(orphan_ids),
            "missing_event_id_count": len(missing_ids),
            "stock_code_mismatch_count": len(stock_mismatches),
            "stock_name_mismatch_count": len(name_mismatches),
            "anchor_date_mismatch_count": len(date_mismatches),
            "invalid_date_count": len(invalid_dates),
            "orphan_sidecar_ids": orphan_ids[:20],
            "missing_event_ids": missing_ids[:20],
        },
        "duplicates": {
            "custom_id": duplicate_summary(rows, ("custom_id",)),
            "matched_stock_date_theme": duplicate_summary(matched, ("stock_code", "anchor_date", "theme")),
            "matched_cross_stock_reused_signal": duplicate_summary(
                matched, ("anchor_date", "theme", "raw_count", "weighted_count", "avg_tone")
            ),
            "matched_measurement": duplicate_summary(
                matched, ("stock_code", "anchor_date", "theme", "raw_count", "weighted_count", "avg_tone")
            ),
        },
        "measurements": {
            "raw_count": numeric_summary(raw_counts),
            "weighted_count": numeric_summary(weighted_counts),
            "avg_tone": numeric_summary(tones),
            "stock_link_score": numeric_summary(scores),
            "raw_count_outlier_threshold_iqr": raw_outlier_threshold,
            "raw_count_outlier_count": len(extreme_raw),
            "abs_tone_ge_5_count": len(extreme_tone),
            "abs_tone_ge_10_count": sum(abs(value) >= 10 for value in tones),
            "invalid_numeric_count": len(invalid_numeric),
            "confidence_counts": dict(Counter(row["confidence_level"] for row in matched)),
            "raw_equals_weighted_count": sum(
                float(row["raw_count"]) == float(row["weighted_count"]) for row in matched
            ),
            "top_raw_count_samples": extreme_raw[:10],
            "extreme_tone_samples": extreme_tone[:10],
        },
        "natural_language_injection_risk": {
            "matched_rows_unsafe_for_direct_writer_injection": len(matched),
            "theme_rows_claiming_increase_without_exposed_baseline": len(theme_increase_claims),
            "theme_labels_containing_interpretive_업황": len(theme_labels_with_interpretation),
            "unsafe_hypothetical_rendering_samples": injection_risk_samples,
            "risk_notes": [
                "'관련 보도 증가' exposes a comparison claim although the sidecar has no writer-facing baseline or comparison window.",
                "raw_count is a GKG record count, not necessarily a count of unique news articles.",
                "avg_tone is an aggregate GDELT measurement and should not be rendered as human news prose or sentiment.",
                "A same-day sector match does not establish that the company event caused, or was caused by, the media signal.",
            ],
            "writer_request_audit": writer_audit,
        },
        "recommendations": [
            "Keep the complete GDELT object outside brief_payload and market_context supplied to article writers.",
            "Key internal joins by custom_id and verify stock_code plus anchor_date before use.",
            "Deduplicate internal analytics by stock_code, anchor_date, theme before aggregation.",
            "Describe raw_count as GKG records in internal UI; never label it as unique articles without URL-level deduplication.",
            "Do not convert avg_tone into positive/negative market interpretation or causal article copy.",
        ],
    }

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    coverage = report["coverage"]
    integrity = report["event_integrity"]
    measurements = report["measurements"]
    lines = [
        "# GDELT Sidecar Quality Audit",
        "",
        "## Policy conclusion",
        "",
        "Keep this layer as internal sidecar metadata. Do not expose theme, record count, or average tone to the article writer.",
        "",
        "## Coverage",
        "",
        f"- Event rows: {coverage['sidecar_row_count']:,} / {coverage['event_count']:,}",
        f"- Universe row coverage: {coverage['sidecar_stock_count']} / {coverage['universe_stock_count']} stocks ({coverage['sidecar_universe_coverage_pct']}%)",
        f"- Actual matched signals: {coverage['matched_event_count']} events ({coverage['matched_event_coverage_pct']}%), {coverage['matched_stock_count']} stocks ({coverage['matched_stock_coverage_pct']}%)",
        f"- Curated-profile coverage: {coverage['profiled_stock_count']} / {coverage['universe_stock_count']} stocks ({coverage['profiled_stock_coverage_pct']}%)",
        f"- Statuses: {json.dumps(coverage['status_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- Stocks without any matched signal: {coverage['universe_stocks_without_match_count']}",
        "",
        "## Event and date integrity",
        "",
        f"- Orphan sidecar IDs: {integrity['orphan_sidecar_id_count']}",
        f"- Missing event IDs: {integrity['missing_event_id_count']}",
        f"- Stock/date/name mismatches: {integrity['stock_code_mismatch_count']} / {integrity['anchor_date_mismatch_count']} / {integrity['stock_name_mismatch_count']}",
        f"- Invalid dates: {integrity['invalid_date_count']}",
        "",
        "## Duplicates",
        "",
        f"- custom_id duplicate groups: {report['duplicates']['custom_id']['duplicate_group_count']}",
        f"- matched stock/date/theme duplicate groups: {report['duplicates']['matched_stock_date_theme']['duplicate_group_count']}",
        f"- cross-stock reused date/theme/measurement groups: {report['duplicates']['matched_cross_stock_reused_signal']['duplicate_group_count']} ({report['duplicates']['matched_cross_stock_reused_signal']['rows_in_duplicate_groups']} rows)",
        f"- exact matched measurement duplicate groups: {report['duplicates']['matched_measurement']['duplicate_group_count']}",
        "",
        "## Measurement extremes",
        "",
        f"- raw_count min/median/p95/max: {measurements['raw_count']['min']:.0f} / {measurements['raw_count']['median']:.1f} / {measurements['raw_count']['p95']:.1f} / {measurements['raw_count']['max']:.0f}",
        f"- IQR raw-count outliers: {measurements['raw_count_outlier_count']} (threshold > {measurements['raw_count_outlier_threshold_iqr']:.2f})",
        f"- avg_tone min/median/max: {measurements['avg_tone']['min']:.2f} / {measurements['avg_tone']['median']:.2f} / {measurements['avg_tone']['max']:.2f}",
        f"- |avg_tone| >= 5 / >= 10: {measurements['abs_tone_ge_5_count']} / {measurements['abs_tone_ge_10_count']}",
        f"- Invalid numeric rows: {measurements['invalid_numeric_count']}",
        f"- Confidence distribution: {json.dumps(measurements['confidence_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- raw_count exactly equals weighted_count: {measurements['raw_equals_weighted_count']} / {len(matched)}",
        "",
        "## Natural-language injection risk",
        "",
        f"- Direct-writer-unsafe matched rows: {len(matched)} / {len(matched)}",
        f"- Themes asserting `보도 증가` without an exposed baseline: {len(theme_increase_claims)}",
        f"- Theme labels containing interpretive `업황`: {len(theme_labels_with_interpretation)}",
        "- `raw_count` counts GKG records, not proven unique news articles.",
        "- `avg_tone` is a machine aggregate, not natural article copy or causal evidence.",
        "- Unsafe rendering examples (illustrative only):",
    ]
    for sample in injection_risk_samples[:3]:
        lines.append(f"  - `{sample['unsafe_hypothetical_rendering']}`")
    lines.extend([
        "",
        "## Writer isolation check",
        "",
    ])
    for item in writer_audit:
        lines.append(
            f"- `{item['path']}`: {item['row_count']} rows, marker leaks {item['rows_with_gdelt_or_measurement_markers']}, structured leaks {item['rows_with_structured_gdelt_context']}"
        )
    lines.extend(
        [
            "",
            "## Recommended gates",
            "",
            "1. Keep GDELT fields out of writer `brief_payload` and `market_context`.",
            "2. Verify `custom_id`, `stock_code`, and `anchor_date` on every internal join.",
            "3. Deduplicate internal analytics on `(stock_code, anchor_date, theme)`.",
            "4. Label `raw_count` as GKG records internally; do not call it article count without URL-level deduplication.",
            "5. Never translate `avg_tone` into positive/negative outlook, market reaction, or causality.",
        ]
    )
    args.report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"coverage": coverage, "event_integrity": integrity, "duplicates": report["duplicates"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
