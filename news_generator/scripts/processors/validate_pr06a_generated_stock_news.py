#!/usr/bin/env python3
"""Validate pr06a stock-news outputs and split accepted/rejected records.

This is a dataset validator, not a generation-quality gate. A well-formed model
rejection is valid and is written to the rejected output without being counted
as a validation error.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from audit_pr06a_v3_4_news_lines import (
    BAD_TERMS,
    MARKET_TERMS,
    SOURCE_LABEL_TERMS,
    NUMERIC_TOKEN_RE,
    OutputReader,
    RequestPayloadReader,
)


REQUIRED_FIELDS = {
    "status",
    "news_id",
    "news_lines",
    "used_brief_id",
    "news_type",
    "claim_level",
    "used_facts",
    "reject_reason",
    "style_self_check",
}
SELF_CHECK_EXPECTED = {
    "used_only_detail_source_facts": True,
    "used_raw_write_safe_facts_directly": False,
    "used_restricted_facts": False,
    "has_market_claim_without_price_evidence": False,
    "has_ai_style_phrase": False,
    "has_generic_filler": False,
    "has_source_label_artifacts": False,
    "has_headline_body_repetition": False,
    "sounds_like_korean_financial_news": True,
}
CAUSAL_TERMS = [
    "때문에",
    "영향으로",
    "그 결과",
    "주된 원인",
    "주된 요인",
    "호재로",
    "악재로",
]
COMMUNITY_TERMS = ["커뮤니티", "게시글", "게시물", "온라인 반응", "토론량", "언급량"]
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def numeric_leaks(text: str, allowed_facts: list[str]) -> list[str]:
    source_text = " ".join(allowed_facts)
    normalized_dates = []
    for match in ISO_DATE_RE.finditer(source_text):
        year, month, day = match.groups()
        normalized_dates.append(f"{year}년 {int(month)}월 {int(day)}일")
    allowed = re.sub(r"\s+", "", source_text + " " + " ".join(normalized_dates))
    leaks = []
    for raw in NUMERIC_TOKEN_RE.findall(text):
        token = re.sub(r"\s+", "", raw)
        if token and token not in allowed:
            leaks.append(token)
    return sorted(set(leaks))


def validate_identity(obj: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    errors = []
    expected = {
        "news_id": payload.get("target_news_id"),
        "used_brief_id": payload.get("brief_id"),
        "news_type": payload.get("news_type"),
        "claim_level": payload.get("claim_level"),
    }
    for field, value in expected.items():
        if obj.get(field) != value:
            errors.append(f"{field}_mismatch")
    return errors


def validate_one(obj: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if obj.get("_parse_error"):
        return ["json_parse_failed"]
    missing = sorted(REQUIRED_FIELDS - obj.keys())
    errors.extend(f"missing_field:{field}" for field in missing)
    if not payload:
        errors.append("request_payload_not_found")
        return errors

    errors.extend(validate_identity(obj, payload))
    status = obj.get("status")
    if status not in {"accepted", "rejected"}:
        errors.append("invalid_status")
        return errors

    lines = as_str_list(obj.get("news_lines"))
    used_facts = as_str_list(obj.get("used_facts"))
    allowed_facts = as_str_list(payload.get("detail_source_facts_ko"))
    reject_reason = obj.get("reject_reason")

    if status == "rejected":
        if lines:
            errors.append("rejected_has_news_lines")
        if used_facts:
            errors.append("rejected_has_used_facts")
        if not isinstance(reject_reason, str) or not reject_reason.strip():
            errors.append("rejected_missing_reason")
        return errors

    if not lines:
        errors.append("accepted_missing_news_lines")
    if not used_facts:
        errors.append("accepted_missing_used_facts")
    if reject_reason != "":
        errors.append("accepted_has_reject_reason")
    if not set(used_facts).issubset(set(allowed_facts)):
        errors.append("used_facts_not_subset_of_detail_source_facts")

    rule = payload.get("news_line_count_rule")
    if rule == "exactly_one_line" and len(lines) != 1:
        errors.append("line_count_should_be_1")
    elif rule == "one_or_two_lines" and len(lines) not in {1, 2}:
        errors.append("line_count_should_be_1_or_2")
    elif rule not in {"exactly_one_line", "one_or_two_lines"}:
        errors.append("unknown_news_line_count_rule")

    text = "\n".join(lines)
    if len(lines) != len(set(lines)):
        errors.append("duplicate_news_lines")
    if any(not line.endswith(".") for line in lines):
        errors.append("news_line_missing_period")
    if any(term in text for term in BAD_TERMS):
        errors.append("ai_or_generic_style_phrase")
    if any(term in text for term in SOURCE_LABEL_TERMS):
        errors.append("source_label_artifact")
    if payload.get("claim_level") == "no_market_claim" and any(term in text for term in MARKET_TERMS):
        errors.append("market_claim_without_price_permission")
    if any(term in text for term in CAUSAL_TERMS):
        errors.append("unsupported_causal_language")
    if any(term in text for term in COMMUNITY_TERMS) and not any(
        term in " ".join(allowed_facts) for term in COMMUNITY_TERMS
    ):
        errors.append("community_context_not_grounded")
    if numeric_leaks(text, allowed_facts):
        errors.append("numeric_fact_not_grounded")

    checks = obj.get("style_self_check")
    if not isinstance(checks, dict):
        errors.append("invalid_style_self_check")
    else:
        for field, expected in SELF_CHECK_EXPECTED.items():
            if checks.get(field) is not expected:
                errors.append(f"style_self_check_failed:{field}")
    return errors


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and split pr06a generated stock news.")
    parser.add_argument("--requests-jsonl", type=Path, required=True)
    parser.add_argument("--outputs-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    requests = RequestPayloadReader.load_by_custom_id(args.requests_jsonl)
    outputs = OutputReader.read(args.outputs_jsonl)
    raw_rows: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    seen_ids: set[str] = set()

    for obj in outputs:
        custom_id = str(obj.pop("_outer_custom_id", "")).strip()
        parse_error = str(obj.pop("_parse_error", ""))
        if parse_error:
            obj["_parse_error"] = parse_error
        payload = requests.get(custom_id, {})
        errors = validate_one(obj, payload)
        normalized = {"custom_id": custom_id, **obj, "validation_errors": errors}
        raw_rows.append(normalized)
        seen_ids.add(custom_id)
        reason_counts.update(error.split(":", 1)[0] for error in errors)
        if obj.get("status") == "accepted" and not errors:
            accepted_rows.append(normalized)
        else:
            rejected_rows.append(normalized)

    for custom_id in sorted(set(requests) - seen_ids):
        row = {"custom_id": custom_id, "status": "invalid", "validation_errors": ["missing_output_for_request"]}
        raw_rows.append(row)
        rejected_rows.append(row)
        reason_counts["missing_output_for_request"] += 1

    write_jsonl(args.output_dir / "generated_stock_news_raw.jsonl", raw_rows)
    write_jsonl(args.output_dir / "generated_stock_news_validated.jsonl", accepted_rows)
    write_jsonl(args.output_dir / "generated_stock_news_rejected.jsonl", rejected_rows)

    model_rejected = sum(1 for row in rejected_rows if row.get("status") == "rejected" and not row["validation_errors"])
    invalid = sum(1 for row in rejected_rows if row["validation_errors"])
    report = [
        "pr06a generated stock news validation report",
        f"requests: {len(requests)}",
        f"outputs: {len(outputs)}",
        f"validated_accepted: {len(accepted_rows)}",
        f"valid_model_rejected: {model_rejected}",
        f"invalid_or_missing: {invalid}",
        "validation_errors:",
    ]
    report.extend(f"- {reason}: {count}" for reason, count in reason_counts.most_common())
    if not reason_counts:
        report.append("- none")
    report_path = args.output_dir / "generated_stock_news_validation_report.txt"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(report_path.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":
    main()
