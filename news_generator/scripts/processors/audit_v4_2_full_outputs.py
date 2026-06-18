#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit merged v4.2 stock-news batch outputs with the v4.1 strict gate."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from run_v4_1_full_chunk_download_and_audit import _audit_one, _load_requests, _read_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit merged pr06a v4.2 stock-news outputs.")
    parser.add_argument(
        "--requests-jsonl",
        type=Path,
        default=Path("data/interim/pr06a_full_requests_v4_2_all_stocks/stock_news_sample_requests.jsonl"),
    )
    parser.add_argument(
        "--outputs-jsonl",
        type=Path,
        default=Path("data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/interim/pr06a_full_outputs_v4_2_all_stocks/audit_all"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    requests = _load_requests(args.requests_jsonl)
    outputs = _read_jsonl(args.outputs_jsonl)
    rows = [_audit_one(obj, requests) for obj in outputs]
    output_ids = {row.custom_id for row in rows}
    for custom_id in requests:
        if custom_id not in output_ids:
            rows.append(_missing_row(custom_id))

    csv_path = args.output_dir / "audit_all.csv"
    failed_path = args.output_dir / "audit_failed.csv"
    fields = [
        "pass_all",
        "custom_id",
        "status",
        "news_lines",
        "line_count",
        "fail_reasons",
        "bad_term_hits",
        "market_term_hits",
        "source_label_hits",
        "numeric_leak_hits",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f_all, failed_path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as f_failed:
        all_writer = csv.writer(f_all)
        fail_writer = csv.writer(f_failed)
        all_writer.writerow(fields)
        fail_writer.writerow(fields)
        for row in rows:
            values = [
                row.pass_all,
                row.custom_id,
                row.status,
                " / ".join(row.news_lines),
                row.line_count,
                "|".join(row.fail_reasons),
                "|".join(row.bad_term_hits),
                "|".join(row.market_term_hits),
                "|".join(row.source_label_hits),
                "|".join(row.numeric_leak_hits),
            ]
            all_writer.writerow(values)
            if not row.pass_all:
                fail_writer.writerow(values)

    total = len(rows)
    passed = sum(1 for row in rows if row.pass_all)
    accepted = sum(1 for row in rows if row.status == "accepted")
    rejected = sum(1 for row in rows if row.status == "rejected")
    missing = sum(1 for row in rows if "missing_output_for_request" in row.fail_reasons)
    report_path = args.output_dir / "audit_report.md"
    report_path.write_text(
        "\n".join(
            [
                "# pr06a v4.2 full output audit",
                "",
                f"- requests: {len(requests)}",
                f"- outputs: {len(outputs)}",
                f"- audited_rows: {total}",
                f"- pass: {passed}",
                f"- fail: {total - passed}",
                f"- pass_rate: {passed / total:.1%}" if total else "- pass_rate: n/a",
                f"- accepted: {accepted}",
                f"- rejected: {rejected}",
                f"- missing_outputs: {missing}",
                f"- audit_csv: {csv_path}",
                f"- failed_csv: {failed_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(report_path.read_text(encoding="utf-8"))


def _missing_row(custom_id: str):
    from run_v4_1_full_chunk_download_and_audit import AuditRow

    return AuditRow(custom_id=custom_id, fail_reasons=["missing_output_for_request"])


if __name__ == "__main__":
    main()
