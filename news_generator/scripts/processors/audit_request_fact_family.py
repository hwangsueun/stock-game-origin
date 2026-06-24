#!/usr/bin/env python3
"""Block request event-family/detail-fact semantic mismatches.

The audit normalizes investment and asset aliases into one semantic family.
With --fail-on-mismatch it exits 2 after writing outputs when any mismatch,
unknown family, mixed fact family, or empty detail fact is found.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


FAMILY_ALIASES = {
    "dividend": "dividend",
    "earnings": "earnings",
    "contract": "contract",
    "investment": "investment_asset",
    "equity_investment": "investment_asset",
    "asset_transaction": "investment_asset",
    "investment_asset": "investment_asset",
    "asset": "investment_asset",
    "facility_investment": "investment_asset",
}

PATTERNS = {
    "dividend": re.compile(r"배당|1주당\s*(?:현금)?배당금|배당기준일"),
    "earnings": re.compile(r"매출액|영업이익|영업손실|당기순이익|당기순손실|손익구조|재무제표\s*기준"),
    "contract": re.compile(r"계약금액|계약 상대방|계약 품목|계약 내용|판매[·ㆍ]?공급계약|공급계약|도급계약"),
    "investment_asset": re.compile(
        r"투자[·ㆍ]?취득|투자금액|시설투자|취득금액|처분금액|지분 거래 금액|"
        r"거래 대상 회사|유형자산|타법인|취득예정|처분예정|출자금액|"
        r"(?:투자|취득|처분|거래)\s*목적|목적을\s*['\"].+?['\"](?:으)?로 공시"
    ),
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_payload(record: dict[str, Any]) -> dict[str, Any]:
    if "body" in record:
        messages = record.get("body", {}).get("messages", [])
        if len(messages) < 2:
            return {}
        content = messages[1].get("content", "")
        parsed = json.loads(content) if isinstance(content, str) else content
        return parsed.get("brief_payload", parsed)
    return record.get("brief_payload", record)


def classify_detail_facts(facts: list[Any]) -> tuple[str, list[str]]:
    cleaned = [clean(item) for item in facts if clean(item)]
    if not cleaned:
        return "empty", []
    # Some malformed contract counterparties contain a trailing table label
    # such as "-최근 매출액(원)". It must not turn a contract into earnings.
    text = " ".join(cleaned)
    text = re.sub(r"-?\s*최근\s*매출액\s*\(원\)", " ", text)
    matched = [family for family, pattern in PATTERNS.items() if pattern.search(text)]
    if not matched:
        return "unknown", []
    if len(matched) > 1:
        return "mixed", matched
    return matched[0], matched


def audit_request(record: dict[str, Any], line_number: int) -> dict[str, Any]:
    payload = load_payload(record)
    raw_family = clean(payload.get("event_family"))
    expected = FAMILY_ALIASES.get(raw_family, "unknown")
    facts_raw = payload.get("detail_source_facts_ko", [])
    facts = facts_raw if isinstance(facts_raw, list) else []
    detected, matched = classify_detail_facts(facts)
    issues: list[str] = []
    if not raw_family or expected == "unknown":
        issues.append("unknown_event_family")
    if detected == "empty":
        issues.append("empty_detail_source_facts")
    elif detected == "unknown":
        issues.append("unknown_detail_fact_family")
    elif detected == "mixed":
        issues.append("mixed_detail_fact_families")
    elif expected != "unknown" and expected != detected:
        issues.append("event_fact_family_mismatch")
    return {
        "line_number": line_number,
        "custom_id": clean(record.get("custom_id") or payload.get("brief_id") or payload.get("bundle_id")),
        "stock_code": clean(payload.get("stock_code")).zfill(6),
        "stock_name": clean(payload.get("stock_name")),
        "event_family": raw_family,
        "expected_semantic_family": expected,
        "detected_fact_family": detected,
        "matched_fact_families": "|".join(matched),
        "detail_fact_count": len(facts),
        "detail_source_facts_ko": json.dumps(facts, ensure_ascii=False),
        "audit_status": "FAIL" if issues else "PASS",
        "audit_issues": "|".join(issues),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["line_number", "custom_id", "audit_status", "audit_issues"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fail-on-mismatch", action="store_true")
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty output directory: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    with args.input.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(audit_request(json.loads(line), line_number))
            except (json.JSONDecodeError, TypeError, KeyError) as error:
                rows.append({
                    "line_number": line_number, "custom_id": "", "stock_code": "", "stock_name": "",
                    "event_family": "", "expected_semantic_family": "unknown", "detected_fact_family": "unknown",
                    "matched_fact_families": "", "detail_fact_count": 0, "detail_source_facts_ko": "[]",
                    "audit_status": "FAIL", "audit_issues": f"malformed_request:{type(error).__name__}",
                })

    failures = [row for row in rows if row["audit_status"] == "FAIL"]
    write_csv(args.output_dir / "request_fact_family_audit.csv", rows)
    write_csv(args.output_dir / "request_fact_family_failures.csv", failures)
    expected_counts = Counter(row["expected_semantic_family"] for row in rows)
    detected_counts = Counter(row["detected_fact_family"] for row in rows)
    issue_counts = Counter(issue for row in failures for issue in row["audit_issues"].split("|") if issue)
    lines = [
        "# Request Fact Family Audit", "",
        f"- Source: `{args.input}`", f"- Requests: {len(rows):,}",
        f"- PASS: {len(rows) - len(failures):,}", f"- FAIL: {len(failures):,}",
        "", "## Expected Families", "",
    ]
    lines.extend(f"- {name}: {count:,}" for name, count in expected_counts.most_common())
    lines.extend(["", "## Detected Fact Families", ""])
    lines.extend(f"- {name}: {count:,}" for name, count in detected_counts.most_common())
    lines.extend(["", "## Failure Reasons", ""])
    lines.extend(f"- {name}: {count:,}" for name, count in issue_counts.most_common())
    if not issue_counts:
        lines.append("- none: 0")
    (args.output_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"requests": len(rows), "pass": len(rows) - len(failures), "fail": len(failures), "issues": issue_counts}, ensure_ascii=False, default=dict))
    return 2 if args.fail_on_mismatch and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
