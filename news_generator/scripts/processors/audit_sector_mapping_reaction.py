#!/usr/bin/env python3
"""Audit the fixed stock-sector mapping and derived sector reaction layer.

This is read-only with respect to source artifacts. It writes mapping review
candidates, reaction anomalies, and a Markdown report to a new output folder.
Industry review candidates are prompts for human review, not asserted errors.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


BROAD_SECTORS = {"제조", "금융", "일반서비스"}
NUMERIC_COLUMNS = (
    "sector_return_1d", "sector_return_5d", "market_return_1d",
    "market_return_5d", "relative_return_1d", "relative_return_5d",
)

# Reader-facing industry comparisons can be misleading even when the mapping
# matches an official coarse KRX classification. Keep these as review-only.
INDUSTRY_REVIEW: dict[str, tuple[str, str]] = {
    "000210": ("holding_company_vs_finance", "사업회사 지주사 성격과 금융업종 비교의 대표성이 낮을 수 있음"),
    "003550": ("holding_company_vs_finance", "복합 지주사를 금융업종으로 비교"),
    "004800": ("holding_company_vs_finance", "복합 지주사를 금융업종으로 비교"),
    "006120": ("holding_company_vs_finance", "화학 계열 지주사를 금융업종으로 비교"),
    "007700": ("holding_company_vs_finance", "패션 지주사를 금융업종으로 비교"),
    "027410": ("holding_company_vs_finance", "유통 지주사를 금융업종으로 비교"),
    "034730": ("holding_company_vs_finance", "복합 지주사를 금융업종으로 비교"),
    "060980": ("holding_company_vs_finance", "자동차부품 지주사를 금융업종으로 비교"),
    "078930": ("holding_company_vs_finance", "에너지·유통 지주사를 금융업종으로 비교"),
    "192400": ("holding_company_vs_finance", "생활가전 지주사를 금융업종으로 비교"),
    "363280": ("holding_company_vs_finance", "미디어·건설 지주사를 금융업종으로 비교"),
    "000270": ("broad_manufacturing", "자동차 업황을 제조 전체와 비교"),
    "005380": ("broad_manufacturing", "자동차 업황을 제조 전체와 비교"),
    "047810": ("broad_manufacturing", "항공우주·방산 업황을 제조 전체와 비교"),
    "064350": ("broad_manufacturing", "철도·방산 업황을 제조 전체와 비교"),
    "068270": ("broad_manufacturing", "바이오·제약 업황을 제조 전체와 비교"),
    "137310": ("broad_manufacturing", "진단 업황을 제조 전체와 비교"),
    "207940": ("broad_manufacturing", "바이오 업황을 제조 전체와 비교"),
    "302440": ("broad_manufacturing", "백신 업황을 제조 전체와 비교"),
    "003490": ("broad_service_transport", "항공 운송을 일반서비스와 비교"),
    "011200": ("broad_service_transport", "해운을 일반서비스와 비교"),
    "028670": ("broad_service_transport", "해운을 일반서비스와 비교"),
    "089590": ("broad_service_transport", "항공 운송을 일반서비스와 비교"),
    "180640": ("broad_service_transport", "항공 지주사를 일반서비스와 비교"),
    "035420": ("broad_service_digital", "인터넷 플랫폼을 일반서비스와 비교"),
    "035720": ("broad_service_digital", "인터넷 플랫폼을 일반서비스와 비교"),
    "036570": ("broad_service_digital", "게임을 일반서비스와 비교"),
    "259960": ("broad_service_digital", "게임을 일반서비스와 비교"),
    "030000": ("broad_service_media", "광고를 일반서비스와 비교"),
    "053210": ("broad_service_media", "방송을 일반서비스와 비교"),
    "126560": ("broad_service_media", "미디어를 일반서비스와 비교"),
    "214320": ("broad_service_media", "광고를 일반서비스와 비교"),
    "051600": ("broad_service_engineering", "발전설비 정비를 일반서비스와 비교"),
    "052690": ("broad_service_engineering", "원전 엔지니어링을 일반서비스와 비교"),
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def pct(a: float, b: float) -> float:
    return round((b / a - 1.0) * 100.0, 2)


def append_issue(row: dict[str, Any], issue: str, severity: str = "FAIL") -> None:
    row.setdefault("issues", []).append(issue)
    if severity == "FAIL" or row.get("severity") != "FAIL":
        row["severity"] = severity


def audit_mapping(mapping: list[dict[str, str]], valid_pairs: set[tuple[str, str]]) -> list[dict[str, Any]]:
    code_counts = Counter(str(row["stock_code"]).zfill(6) for row in mapping)
    output: list[dict[str, Any]] = []
    for source in mapping:
        row: dict[str, Any] = dict(source)
        code = str(source["stock_code"]).zfill(6)
        row["stock_code"] = code
        row["issues"] = []
        row["severity"] = "PASS"
        if code_counts[code] != 1:
            append_issue(row, "duplicate_stock_code")
        if not source.get("stock_name"):
            append_issue(row, "missing_stock_name")
        if not source.get("market") or not source.get("index_name"):
            append_issue(row, "missing_market_or_index")
        elif (source["market"], source["index_name"]) not in valid_pairs:
            append_issue(row, "invalid_market_index_pair")
        if source.get("index_name") in BROAD_SECTORS:
            append_issue(row, "broad_sector_reader_suppression_recommended", "WARN")
        if code in INDUSTRY_REVIEW:
            kind, note = INDUSTRY_REVIEW[code]
            row["industry_review_type"] = kind
            row["industry_review_note"] = note
            append_issue(row, "industry_review_candidate", "WARN")
        else:
            row["industry_review_type"] = ""
            row["industry_review_note"] = ""
        row["issues"] = "|".join(row["issues"])
        output.append(row)
    return output


def build_context_index(rows: list[dict[str, str]]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], set[tuple[str, str]], dict[tuple[str, str], set[str]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    codes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        pair = (row["market"], row["index_name"])
        enriched = dict(row)
        enriched["_date"] = date.fromisoformat(row["date"])
        enriched["_close"] = parse_float(row.get("close"))
        enriched["_market_1d"] = parse_float(row.get("market_return_1d"))
        enriched["_market_5d"] = parse_float(row.get("market_return_5d"))
        grouped[pair].append(enriched)
        codes[pair].add(str(row.get("index_code", "")).removesuffix(".0"))
    for values in grouped.values():
        values.sort(key=lambda item: item["_date"])
    return grouped, set(grouped), codes


def audit_reactions(
    reactions: list[dict[str, str]],
    mapping_by_code: dict[str, dict[str, str]],
    context: dict[tuple[str, str], list[dict[str, Any]]],
    context_codes: dict[tuple[str, str], set[str]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    custom_counts = Counter(row["custom_id"] for row in reactions)
    ok_assignments: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for source in reactions:
        row: dict[str, Any] = {
            "custom_id": source.get("custom_id", ""), "stock_code": str(source.get("stock_code", "")).zfill(6),
            "stock_name": source.get("stock_name", ""), "anchor_date": source.get("anchor_date", ""),
            "trade_date": source.get("trade_date", ""), "market": source.get("market", ""),
            "index_name": source.get("index_name", ""), "index_code": source.get("index_code", ""),
            "status": source.get("status", ""), "severity": "PASS", "issues": [],
        }
        code = row["stock_code"]
        mapped = mapping_by_code.get(code)
        if not mapped:
            append_issue(row, "stock_missing_from_mapping")
        elif (row["market"], row["index_name"]) != (mapped["market"], mapped["index_name"]):
            append_issue(row, "reaction_mapping_mismatch")
        if custom_counts[row["custom_id"]] != 1:
            append_issue(row, "duplicate_custom_id")
        if source.get("status") != "ok":
            append_issue(row, f"non_ok_status:{source.get('status') or 'blank'}", "WARN")
        missing = [name for name in NUMERIC_COLUMNS if parse_float(source.get(name)) is None]
        if source.get("status") == "ok" and missing:
            for name in missing:
                append_issue(row, "ok_row_missing_numeric:" + name)
        elif source.get("status") != "ok" and missing:
            for name in missing:
                append_issue(row, "missing_numeric_non_ok:" + name, "WARN")
        if not row["anchor_date"]:
            append_issue(row, "missing_anchor_date")
        if not row["trade_date"]:
            append_issue(row, "missing_trade_date" if source.get("status") == "ok" else "missing_trade_date_non_ok", "WARN" if source.get("status") != "ok" else "FAIL")

        if source.get("status") == "ok":
            ok_assignments[code].add((row["market"], row["index_name"], row["index_code"]))
            pair = (row["market"], row["index_name"])
            if row["index_code"] not in context_codes.get(pair, set()):
                append_issue(row, "invalid_index_code_for_pair")
            try:
                anchor = date.fromisoformat(row["anchor_date"])
                trade = date.fromisoformat(row["trade_date"])
                if trade > anchor:
                    append_issue(row, "trade_date_after_anchor")
                if (anchor - trade).days > 7:
                    append_issue(row, "trade_date_lag_gt_7_days", "WARN")
            except ValueError:
                append_issue(row, "invalid_anchor_or_trade_date")

            series = context.get(pair, [])
            positions = {item["_date"]: pos for pos, item in enumerate(series)}
            pos = positions.get(date.fromisoformat(row["trade_date"])) if row["trade_date"] else None
            if pos is None or pos + 5 >= len(series):
                append_issue(row, "cannot_recompute_from_context")
            else:
                t0, t1, t5 = series[pos], series[pos + 1], series[pos + 5]
                expected = {
                    "sector_return_1d": pct(t0["_close"], t1["_close"]),
                    "sector_return_5d": pct(t0["_close"], t5["_close"]),
                    "market_return_1d": round(t1["_market_1d"], 2),
                    "market_return_5d": round(t5["_market_5d"], 2),
                }
                expected["relative_return_1d"] = round(expected["sector_return_1d"] - expected["market_return_1d"], 2)
                expected["relative_return_5d"] = round(expected["sector_return_5d"] - expected["market_return_5d"], 2)
                for name, value in expected.items():
                    actual = parse_float(source.get(name))
                    if actual is None or abs(actual - value) > 0.011:
                        append_issue(row, f"recompute_mismatch:{name}")

        r1 = parse_float(source.get("sector_return_1d"))
        r5 = parse_float(source.get("sector_return_5d"))
        if r1 is not None and abs(r1) > 10:
            append_issue(row, "extreme_sector_return_1d_gt_10pct", "WARN")
        if r5 is not None and abs(r5) > 25:
            append_issue(row, "extreme_sector_return_5d_gt_25pct", "WARN")
        row["issues"] = "|".join(row["issues"])
        if row["issues"]:
            output.append(row)

    for code, assignments in ok_assignments.items():
        if len(assignments) > 1:
            output.append({"custom_id": "", "stock_code": code, "stock_name": mapping_by_code.get(code, {}).get("stock_name", ""), "anchor_date": "", "trade_date": "", "market": "", "index_name": "", "index_code": "", "status": "", "severity": "FAIL", "issues": "inconsistent_ok_mapping_for_stock"})
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["stock_code", "issues"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--reactions", type=Path, required=True)
    parser.add_argument("--sector-context", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty output directory: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    mapping = read_csv(args.mapping)
    reactions = read_csv(args.reactions)
    context_rows = read_csv(args.sector_context)
    context, valid_pairs, context_codes = build_context_index(context_rows)
    mapping_audit = audit_mapping(mapping, valid_pairs)
    mapping_by_code = {row["stock_code"].zfill(6): row for row in mapping}
    reaction_anomalies = audit_reactions(reactions, mapping_by_code, context, context_codes)
    industry_candidates = [row for row in mapping_audit if row["industry_review_type"]]

    write_csv(args.output_dir / "mapping_audit.csv", mapping_audit)
    write_csv(args.output_dir / "industry_review_candidates.csv", industry_candidates)
    write_csv(args.output_dir / "reaction_anomalies.csv", reaction_anomalies)

    map_status = Counter(row["severity"] for row in mapping_audit)
    reaction_status = Counter(row["status"] for row in reactions)
    anomaly_issues = Counter(issue for row in reaction_anomalies for issue in row["issues"].split("|") if issue)
    broad = Counter(row["index_name"] for row in mapping if row["index_name"] in BROAD_SECTORS)
    end_date = max(row["date"] for row in context_rows)
    numeric_summary: dict[str, tuple[int, float | None, float | None]] = {}
    for column in NUMERIC_COLUMNS:
        values = [value for value in (parse_float(row.get(column)) for row in reactions) if value is not None]
        numeric_summary[column] = (len(reactions) - len(values), min(values) if values else None, max(values) if values else None)
    ok_assignments: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for row in reactions:
        if row.get("status") == "ok":
            ok_assignments[row["stock_code"]].add((row["market"], row["index_name"], row["index_code"]))
    inconsistent_ok_stocks = sum(len(values) > 1 for values in ok_assignments.values())
    recompute_mismatches = sum(count for issue, count in anomaly_issues.items() if issue.startswith("recompute_mismatch:"))
    lines = [
        "# Sector Mapping and Reaction Audit", "",
        f"- Mapping rows: {len(mapping):,}",
        f"- Unique stock codes: {len(mapping_by_code):,}",
        f"- Mapping FAIL/WARN/PASS: {map_status['FAIL']:,}/{map_status['WARN']:,}/{map_status['PASS']:,}",
        f"- Valid market/index pairs in source: {len(valid_pairs):,}",
        f"- Reaction rows: {len(reactions):,}",
        f"- Reaction status: {dict(reaction_status)}",
        f"- Sector context last date: {end_date}",
        f"- Stocks with inconsistent market/index/code among `ok` rows: {inconsistent_ok_stocks:,}",
        f"- Independently recomputed return mismatches: {recompute_mismatches:,}",
        "", "## Broad Sectors", "",
    ]
    lines.extend(f"- {name}: {broad[name]:,} stocks" for name in sorted(BROAD_SECTORS))
    lines.extend([
        f"- Total broad-sector mappings: {sum(broad.values()):,}/{len(mapping):,}",
        "- Policy: suppress these labels in reader-facing comparisons; validity in the KRX source does not make them informative industry context.",
        "", "## Industry Review Candidates", "",
        f"- Candidates: {len(industry_candidates):,}",
        "- These are review candidates, not established mapping errors. The source may intentionally expose only a coarse index.",
        "", "## Reaction Issues", "",
    ])
    lines.extend(f"- {name}: {count:,}" for name, count in anomaly_issues.most_common())
    lines.extend(["", "## Numeric Ranges", ""])
    for name, (missing_count, minimum, maximum) in numeric_summary.items():
        lines.append(f"- {name}: missing {missing_count:,}; min {minimum}; max {maximum}")
    lines.extend(["", "## Reproducibility", "", "Every `ok` row was independently recomputed from the sector context close series and matching market-return fields. Tolerance: 0.011 percentage points.", ""])
    (args.output_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print({"mapping_rows": len(mapping), "mapping_status": dict(map_status), "industry_candidates": len(industry_candidates), "reaction_rows": len(reactions), "reaction_status": dict(reaction_status), "anomaly_issues": dict(anomaly_issues)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
