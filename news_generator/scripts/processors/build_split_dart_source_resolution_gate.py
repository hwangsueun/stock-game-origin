#!/usr/bin/env python3
"""Resolve one exact DART source for each full split-article request.

Resolution is intentionally conservative:
1. A selected detail-fact set contained in exactly one related evidence group
   resolves only when that evidence ID is also a candidate for the normalized
   request event family.
2. With no exact group, exactly one correction-aware candidate for the same
   bundle and event family is an explicit fallback.
3. Cross-family exact groups, multiple exact groups, or multiple candidates
   remain unresolved.

All outputs are new validation artifacts. No source file is modified.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PIPELINE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REQUESTS = PIPELINE_ROOT / (
    "news_generator/data/interim/context_layers/requests_context_v8_sector.jsonl"
)
DEFAULT_BRIEFS = PIPELINE_ROOT / (
    "news_generator/data/interim/pr05f_stock_news_briefs_v3_all_stocks/"
    "stock_news_briefs.jsonl"
)
DEFAULT_CANDIDATES = PIPELINE_ROOT / (
    "news_generator/data/interim/"
    "pr05f_dart_disclosure_detail_facts_v6_correction_fix_audit/"
    "dart_disclosure_detail_candidates.csv"
)
DEFAULT_OUTPUT_DIR = PIPELINE_ROOT / (
    "news_generator/data/interim/split_dart_source_resolution_gate_v1"
)

EXPECTED_METHOD_COUNTS = {
    "resolved_exact_group": 1363,
    "resolved_single_candidate_fallback": 45,
    "unresolved_ambiguous_exact": 17,
    "unresolved_cross_family_fact": 174,
    "unresolved_multiple_candidates": 35,
}


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def report_family(report_name: str) -> str:
    if "배당" in report_name:
        return "dividend"
    if "판매" in report_name or "공급계약" in report_name:
        return "contract"
    if "매출액" in report_name or "손익구조" in report_name:
        return "earnings"
    if "시설투자" in report_name or "타법인" in report_name or "유형자산" in report_name:
        return "investment"
    return "other"


def normalize_event_family(event_family: str) -> str:
    if event_family in {"asset_transaction", "equity_investment"}:
        return "investment"
    return event_family


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
    return rows


def request_payload(request: dict[str, Any]) -> dict[str, Any]:
    messages = request["body"]["messages"]
    user_message = next(message for message in messages if message.get("role") == "user")
    return json.loads(user_message["content"])["brief_payload"]


def normalize_evidence_id(value: Any) -> str:
    evidence_id = clean(value)
    return evidence_id.removeprefix("DART_")


def exact_group_matches(
    selected_facts: list[str], brief: dict[str, Any]
) -> list[str]:
    matches: list[str] = []
    for group in brief.get("related_fact_groups_ko") or []:
        if group.get("source") != "dart_disclosure_detail":
            continue
        group_facts = group.get("facts_ko") or []
        if selected_facts and all(fact in group_facts for fact in selected_facts):
            evidence_id = normalize_evidence_id(group.get("evidence_id"))
            if evidence_id and evidence_id not in matches:
                matches.append(evidence_id)
    return matches


def load_candidates(
    path: Path,
) -> tuple[
    dict[tuple[str, str], list[dict[str, str]]],
    dict[tuple[str, str], set[str]],
]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    families_by_evidence: dict[tuple[str, str], set[str]] = defaultdict(set)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            family = report_family(row["report_name"])
            key = (row["bundle_id"], family)
            if not any(item["rcept_no"] == row["rcept_no"] for item in grouped[key]):
                grouped[key].append(row)
            families_by_evidence[(row["bundle_id"], row["rcept_no"])].add(family)
    for rows in grouped.values():
        rows.sort(key=lambda row: row["rcept_no"])
    return grouped, families_by_evidence


def resolve(
    request: dict[str, Any],
    brief: dict[str, Any],
    candidates: list[dict[str, str]],
    families_by_evidence: dict[tuple[str, str], set[str]],
) -> dict[str, str]:
    payload = request_payload(request)
    selected_facts = [clean(fact) for fact in payload["detail_source_facts_ko"]]
    raw_exact_matches = exact_group_matches(selected_facts, brief)
    candidate_ids = [row["rcept_no"] for row in candidates]
    exact_matches = [item for item in raw_exact_matches if item in candidate_ids]
    raw_exact_families = sorted(
        {
            family
            for evidence_id in raw_exact_matches
            for family in families_by_evidence.get(
                (payload["brief_id"], evidence_id), set()
            )
        }
    )

    resolved_rcept_no = ""
    resolved_report_name = ""
    if len(exact_matches) == 1:
        status = "resolved"
        method = "resolved_exact_group"
        resolved_rcept_no = exact_matches[0]
        candidate = next(
            (row for row in candidates if row["rcept_no"] == resolved_rcept_no), None
        )
        resolved_report_name = candidate["report_name"] if candidate else ""
    elif len(exact_matches) > 1:
        status = "unresolved"
        method = "unresolved_ambiguous_exact"
    elif raw_exact_matches:
        status = "unresolved"
        method = "unresolved_cross_family_fact"
    elif len(candidates) == 1:
        status = "resolved"
        method = "resolved_single_candidate_fallback"
        resolved_rcept_no = candidates[0]["rcept_no"]
        resolved_report_name = candidates[0]["report_name"]
    elif len(candidates) > 1:
        status = "unresolved"
        method = "unresolved_multiple_candidates"
    else:
        status = "unresolved"
        method = "unresolved_no_candidate"

    return {
        "custom_id": request["custom_id"],
        "target_news_id": payload["target_news_id"],
        "brief_id": payload["brief_id"],
        "stock_code": str(payload["stock_code"]).zfill(6),
        "stock_name": payload["stock_name"],
        "anchor_date": payload["anchor_date"],
        "event_family": payload["event_family"],
        "normalized_event_family": normalize_event_family(payload["event_family"]),
        "selected_detail_facts_json": json.dumps(selected_facts, ensure_ascii=False),
        "raw_exact_match_count": str(len(raw_exact_matches)),
        "raw_exact_evidence_ids_json": json.dumps(raw_exact_matches, ensure_ascii=False),
        "raw_exact_candidate_families_json": json.dumps(
            raw_exact_families, ensure_ascii=False
        ),
        "exact_match_count": str(len(exact_matches)),
        "exact_evidence_ids_json": json.dumps(exact_matches, ensure_ascii=False),
        "family_candidate_count": str(len(candidates)),
        "family_candidate_rcept_nos_json": json.dumps(candidate_ids, ensure_ascii=False),
        "resolution_status": status,
        "resolution_method": method,
        "resolved_rcept_no": resolved_rcept_no,
        "resolved_report_name": resolved_report_name,
    }


def validate(
    requests: list[dict[str, Any]], rows: list[dict[str, str]], enforce_expected: bool
) -> Counter[str]:
    if len(rows) != len(requests):
        raise AssertionError("Gate output row count differs from request count")
    if len({row["custom_id"] for row in rows}) != len(rows):
        raise AssertionError("custom_id is not unique")

    for row in rows:
        method = row["resolution_method"]
        resolved = bool(row["resolved_rcept_no"])
        if row["resolution_status"] == "resolved" and not resolved:
            raise AssertionError(f"Resolved row lacks rcept_no: {row['custom_id']}")
        if row["resolution_status"] == "unresolved" and resolved:
            raise AssertionError(f"Unresolved row has rcept_no: {row['custom_id']}")
        if method == "resolved_exact_group" and row["exact_match_count"] != "1":
            raise AssertionError(f"Exact resolution is not unique: {row['custom_id']}")
        if method == "resolved_single_candidate_fallback":
            if row["exact_match_count"] != "0" or row["family_candidate_count"] != "1":
                raise AssertionError(f"Invalid fallback resolution: {row['custom_id']}")
            if row["raw_exact_match_count"] != "0":
                raise AssertionError(f"Cross-family fact used as fallback: {row['custom_id']}")
        if method == "unresolved_cross_family_fact":
            source_families = json.loads(row["raw_exact_candidate_families_json"])
            if row["raw_exact_match_count"] == "0" or row["exact_match_count"] != "0":
                raise AssertionError(f"Invalid cross-family block: {row['custom_id']}")
            if row["normalized_event_family"] in source_families:
                raise AssertionError(f"Same-family evidence blocked: {row['custom_id']}")

    counts = Counter(row["resolution_method"] for row in rows)
    if enforce_expected and dict(counts) != EXPECTED_METHOD_COUNTS:
        raise AssertionError(
            f"Resolution counts changed: actual={dict(counts)}, "
            f"expected={EXPECTED_METHOD_COUNTS}"
        )
    return counts


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    path: Path, total: int, counts: Counter[str], unresolved: list[dict[str, str]]
) -> None:
    resolved_count = sum(
        count for method, count in counts.items() if method.startswith("resolved_")
    )
    lines = [
        "# Split DART Source Resolution Gate",
        "",
        f"- Total requests: {total:,}",
        f"- Resolved: {resolved_count:,}",
        f"- Unresolved: {total - resolved_count:,}",
        f"- Resolution coverage: {100 * resolved_count / total:.2f}%",
        "",
        "## Method Counts",
        "",
    ]
    for method, count in sorted(counts.items()):
        lines.append(f"- `{method}`: {count:,}")
    cross_family = [
        row
        for row in unresolved
        if row["resolution_method"] == "unresolved_cross_family_fact"
    ]
    other_unresolved = [row for row in unresolved if row not in cross_family]
    lines += [
        "",
        f"## Cross-Family Blocks ({len(cross_family):,})",
        "",
        "These rows are never eligible for single-candidate fallback.",
        "",
    ]
    for row in cross_family:
        lines.append(
            f"- `{row['custom_id']}` | {row['stock_name']} | "
            f"{row['anchor_date']} | requested=`{row['event_family']}` | "
            f"source_families={row['raw_exact_candidate_families_json']} | "
            f"raw_exact={row['raw_exact_evidence_ids_json']} | "
            f"family_candidates={row['family_candidate_rcept_nos_json']}"
        )
    lines += ["", f"## Other Unresolved Rows ({len(other_unresolved):,})", ""]
    for row in other_unresolved:
        lines.append(
            f"- `{row['custom_id']}` | {row['stock_name']} | "
            f"{row['anchor_date']} | `{row['resolution_method']}` | "
            f"raw_exact={row['raw_exact_evidence_ids_json']} | "
            f"raw_families={row['raw_exact_candidate_families_json']} | "
            f"family_exact={row['exact_evidence_ids_json']} | "
            f"candidates={row['family_candidate_rcept_nos_json']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, default=DEFAULT_REQUESTS)
    parser.add_argument("--briefs", type=Path, default=DEFAULT_BRIEFS)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--no-enforce-expected",
        action="store_true",
        help="Skip dataset-specific expected-count assertions.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    source_parents = {
        args.requests.resolve().parent,
        args.briefs.resolve().parent,
        args.candidates.resolve().parent,
    }
    if output_dir in source_parents:
        raise ValueError("Output directory must differ from every source directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    requests = read_jsonl(args.requests)
    briefs = {brief["bundle_id"]: brief for brief in read_jsonl(args.briefs)}
    candidates_by_key, families_by_evidence = load_candidates(args.candidates)

    rows: list[dict[str, str]] = []
    for request in requests:
        payload = request_payload(request)
        brief_id = payload["brief_id"]
        if brief_id not in briefs:
            raise KeyError(f"Brief not found for {brief_id}")
        normalized_family = normalize_event_family(payload["event_family"])
        key = (brief_id, normalized_family)
        rows.append(
            resolve(
                request,
                briefs[brief_id],
                candidates_by_key.get(key, []),
                families_by_evidence,
            )
        )

    counts = validate(requests, rows, not args.no_enforce_expected)
    csv_path = output_dir / "split_dart_source_resolution.csv"
    report_path = output_dir / "split_dart_source_resolution_report.md"
    summary_path = output_dir / "split_dart_source_resolution_summary.json"
    write_csv(csv_path, rows)
    unresolved = [row for row in rows if row["resolution_status"] == "unresolved"]
    write_report(report_path, len(rows), counts, unresolved)

    resolved_count = len(rows) - len(unresolved)
    summary = {
        "total_requests": len(rows),
        "resolved_count": resolved_count,
        "resolved_coverage_pct": round(100 * resolved_count / len(rows), 4),
        "unresolved_count": len(unresolved),
        "resolution_method_counts": dict(sorted(counts.items())),
        "cross_family_counts_by_requested_family": dict(
            sorted(
                Counter(
                    row["event_family"]
                    for row in rows
                    if row["resolution_method"] == "unresolved_cross_family_fact"
                ).items()
            )
        ),
        "cross_family_counts_by_source_families": dict(
            sorted(
                Counter(
                    row["raw_exact_candidate_families_json"]
                    for row in rows
                    if row["resolution_method"] == "unresolved_cross_family_fact"
                ).items()
            )
        ),
        "expected_method_counts": EXPECTED_METHOD_COUNTS,
        "expected_counts_enforced": not args.no_enforce_expected,
        "validation": {
            "request_row_count_preserved": True,
            "custom_ids_unique": True,
            "exact_resolution_requires_one_group": True,
            "fallback_requires_zero_exact_and_one_candidate": True,
            "taxonomy_aliases_normalized": True,
            "cross_family_facts_not_used_as_fallback": True,
            "unresolved_rows_have_no_resolved_rcept_no": True,
        },
        "outputs": {
            "resolution_csv": str(csv_path),
            "report_md": str(report_path),
        },
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
