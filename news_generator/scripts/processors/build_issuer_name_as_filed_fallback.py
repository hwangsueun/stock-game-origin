#!/usr/bin/env python3
"""Build a date-aware, non-destructive issuer-name fallback validation set.

The source detail-facts CSV is never modified. Exact issuer names win. Missing
names are filled only when the history for the same stock code makes the choice
unambiguous under the documented policy.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PIPELINE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = PIPELINE_ROOT / Path(
    "news_generator/data/interim/"
    "pr05f_dart_disclosure_detail_facts_v6_article_ready/"
    "dart_disclosure_detail_facts.csv"
)
DEFAULT_OUTPUT_DIR = PIPELINE_ROOT / Path(
    "news_generator/data/interim/"
    "pr05f_dart_disclosure_detail_facts_v6_issuer_name_fallback_validation"
)

ISSUER_NAME_PATTERN = re.compile(
    r"공시 당시 회사명은\s*[\"'‘“](.*?)[\"'’”]이다\.?\s*$"
)


@dataclass(frozen=True)
class Observation:
    rcept_no: str
    issuer_name: str


def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def extract_exact_issuer_name(facts_json: str) -> str:
    facts = json.loads(facts_json or "[]")
    names: list[str] = []
    for fact in facts:
        if fact.get("fact_type") != "issuer_name_as_filed":
            continue
        text = clean(fact.get("text_ko") or fact.get("source_text_ko"))
        match = ISSUER_NAME_PATTERN.fullmatch(text)
        if not match:
            raise ValueError(f"Malformed issuer_name_as_filed fact: {text!r}")
        names.append(clean(match.group(1)))
    if len(names) > 1:
        raise ValueError(f"Multiple issuer_name_as_filed facts: {names!r}")
    return names[0] if names else ""


def build_history(rows: list[dict[str, str]]) -> dict[str, list[Observation]]:
    history: dict[str, list[Observation]] = defaultdict(list)
    for row in rows:
        exact = extract_exact_issuer_name(row["facts_json"])
        if exact:
            history[row["stock_code"]].append(Observation(row["rcept_no"], exact))
    for observations in history.values():
        observations.sort(key=lambda item: item.rcept_no)
    return history


def resolve_name(
    row: dict[str, str], observations: list[Observation]
) -> dict[str, str]:
    exact = extract_exact_issuer_name(row["facts_json"])
    if exact:
        return {
            "issuer_name_exact": exact,
            "issuer_name_resolved": exact,
            "resolution_method": "exact",
            "previous_rcept_no": "",
            "previous_issuer_name": "",
            "next_rcept_no": "",
            "next_issuer_name": "",
        }

    if not observations:
        return {
            "issuer_name_exact": "",
            "issuer_name_resolved": "",
            "resolution_method": "no_observation_for_code",
            "previous_rcept_no": "",
            "previous_issuer_name": "",
            "next_rcept_no": "",
            "next_issuer_name": "",
        }

    unique_names = {item.issuer_name for item in observations}
    if len(unique_names) == 1:
        name = observations[0].issuer_name
        return {
            "issuer_name_exact": "",
            "issuer_name_resolved": name,
            "resolution_method": "single_name_for_code",
            "previous_rcept_no": "",
            "previous_issuer_name": "",
            "next_rcept_no": "",
            "next_issuer_name": "",
        }

    receipt_numbers = [item.rcept_no for item in observations]
    index = bisect_left(receipt_numbers, row["rcept_no"])
    previous = observations[index - 1] if index else None
    following = observations[index] if index < len(observations) else None

    context = {
        "issuer_name_exact": "",
        "previous_rcept_no": previous.rcept_no if previous else "",
        "previous_issuer_name": previous.issuer_name if previous else "",
        "next_rcept_no": following.rcept_no if following else "",
        "next_issuer_name": following.issuer_name if following else "",
    }
    if previous is None:
        return {
            **context,
            "issuer_name_resolved": following.issuer_name,
            "resolution_method": "earliest_boundary",
        }
    if following is None:
        return {
            **context,
            "issuer_name_resolved": previous.issuer_name,
            "resolution_method": "latest_boundary",
        }
    if previous.issuer_name == following.issuer_name:
        return {
            **context,
            "issuer_name_resolved": previous.issuer_name,
            "resolution_method": "interpolated_same_neighbors",
        }
    return {
        **context,
        "issuer_name_resolved": "",
        "resolution_method": "ambiguous_name_change",
    }


def build_change_rows(
    history: dict[str, list[Observation]], stock_names: dict[str, str]
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for code, observations in sorted(history.items()):
        previous_name = ""
        sequence = 0
        for observation in observations:
            if observation.issuer_name == previous_name:
                continue
            sequence += 1
            out.append(
                {
                    "stock_code": code,
                    "current_stock_name": stock_names[code],
                    "sequence": str(sequence),
                    "first_observed_rcept_no": observation.rcept_no,
                    "issuer_name_as_filed": observation.issuer_name,
                }
            )
            previous_name = observation.issuer_name
    return out


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def validate(
    source_rows: list[dict[str, str]], resolved_rows: list[dict[str, str]]
) -> None:
    if len(source_rows) != len(resolved_rows):
        raise AssertionError("Resolution output row count differs from source")
    if len({row["rcept_no"] for row in source_rows}) != len(source_rows):
        raise AssertionError("Source rcept_no values are not unique")

    for source, resolved in zip(source_rows, resolved_rows):
        exact = extract_exact_issuer_name(source["facts_json"])
        method = resolved["resolution_method"]
        name = resolved["issuer_name_resolved"]
        if exact and (method != "exact" or name != exact):
            raise AssertionError(f"Exact name was not preserved: {source['rcept_no']}")
        if method == "ambiguous_name_change" and name:
            raise AssertionError(f"Ambiguous name was auto-filled: {source['rcept_no']}")
        if not exact and method == "exact":
            raise AssertionError(f"Missing exact name marked exact: {source['rcept_no']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir == input_path.parent:
        raise ValueError("Output directory must differ from the source directory")

    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    history = build_history(source_rows)
    stock_names = {row["stock_code"]: row["stock_name"] for row in source_rows}

    resolved_rows: list[dict[str, str]] = []
    for source in source_rows:
        resolution = resolve_name(source, history.get(source["stock_code"], []))
        resolved_rows.append(
            {
                "rcept_no": source["rcept_no"],
                "stock_code": source["stock_code"],
                "current_stock_name": source["stock_name"],
                "report_name": source["report_name"],
                "source_status": source["status"],
                **resolution,
            }
        )
    validate(source_rows, resolved_rows)

    resolution_fields = list(resolved_rows[0])
    resolution_path = output_dir / "issuer_name_resolution_validation.csv"
    write_csv(resolution_path, resolved_rows, resolution_fields)

    change_rows = build_change_rows(history, stock_names)
    change_path = output_dir / "issuer_name_observed_change_points.csv"
    write_csv(change_path, change_rows, list(change_rows[0]))

    method_counts = Counter(row["resolution_method"] for row in resolved_rows)
    resolved_count = sum(bool(row["issuer_name_resolved"]) for row in resolved_rows)
    summary = {
        "source_csv": str(input_path),
        "total_rows": len(source_rows),
        "stock_code_count": len({row["stock_code"] for row in source_rows}),
        "stock_codes_with_observation": len(history),
        "exact_count": method_counts["exact"],
        "exact_coverage_pct": round(100 * method_counts["exact"] / len(source_rows), 4),
        "resolved_count": resolved_count,
        "resolved_coverage_pct": round(100 * resolved_count / len(source_rows), 4),
        "unresolved_count": len(source_rows) - resolved_count,
        "resolution_method_counts": dict(sorted(method_counts.items())),
        "observed_issuer_name_count": len(
            {item.issuer_name for observations in history.values() for item in observations}
        ),
        "multi_name_stock_code_count": sum(
            len({item.issuer_name for item in observations}) > 1
            for observations in history.values()
        ),
        "validation": {
            "source_row_count_preserved": True,
            "exact_names_preserved": True,
            "ambiguous_names_not_auto_filled": True,
        },
        "outputs": {
            "resolution_csv": str(resolution_path),
            "change_points_csv": str(change_path),
        },
    }
    summary_path = output_dir / "issuer_name_resolution_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
