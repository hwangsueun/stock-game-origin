#!/usr/bin/env python3
"""Replace context-changed model outputs in the existing complete output set."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-outputs-jsonl", type=Path, required=True)
    ap.add_argument("--context-outputs-jsonl", type=Path, required=True)
    ap.add_argument("--context-requests-jsonl", type=Path, required=True)
    ap.add_argument(
        "--semantic-audit-csv",
        type=Path,
        required=True,
        help="Semantic gate output; every requested custom_id must pass before merge.",
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    base = read_rows(args.base_outputs_jsonl)
    replacement_rows = read_rows(args.context_outputs_jsonl)
    expected = {row["custom_id"] for row in read_rows(args.context_requests_jsonl)}
    replacements = {row["custom_id"]: row for row in replacement_rows}
    if len(replacements) != len(replacement_rows):
        raise ValueError("duplicate custom_id in context outputs")
    missing = sorted(expected - replacements.keys())
    extra = sorted(replacements.keys() - expected)
    if missing or extra:
        raise ValueError(f"context output ID mismatch: missing={missing[:10]} extra={extra[:10]}")

    with args.semantic_audit_csv.open(newline="", encoding="utf-8-sig") as f:
        audit_rows = list(csv.DictReader(f))
    audit_by_id = {row.get("custom_id", ""): row for row in audit_rows}
    if len(audit_by_id) != len(audit_rows):
        raise ValueError("duplicate custom_id in semantic audit")
    audit_missing = sorted(expected - audit_by_id.keys())
    audit_extra = sorted(audit_by_id.keys() - expected)
    failed = sorted(
        custom_id
        for custom_id in expected
        if audit_by_id.get(custom_id, {}).get("pass", "").strip().lower() != "true"
    )
    if audit_missing or audit_extra or failed:
        raise ValueError(
            "semantic audit blocks merge: "
            f"missing={audit_missing[:10]} extra={audit_extra[:10]} "
            f"failed={failed[:10]} failed_count={len(failed)}"
        )

    base_ids = {row["custom_id"] for row in base}
    if not expected <= base_ids:
        raise ValueError(f"context IDs absent from base outputs: {sorted(expected - base_ids)[:10]}")
    merged = [replacements.get(row["custom_id"], row) for row in base]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in merged:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[done] base={len(base)} replaced={len(replacements)} merged={len(merged)} -> {args.out}")


if __name__ == "__main__":
    main()
