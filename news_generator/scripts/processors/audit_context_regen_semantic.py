#!/usr/bin/env python3
"""Semantic quality gate for context-regenerated news outputs.

The v8 factual audit checks allowed numbers/terms, but it does not guarantee the
article used the metric that made the event material.  This gate enforces the
chosen primary trigger (5-day return, else 1-day return, else volume) and flags
mechanical broad-sector comparisons.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


BROAD_SECTORS = ["제조 업종지수", "금융 업종지수", "일반서비스 업종지수"]


def model_payload(row: dict) -> dict:
    try:
        content = row["response"]["body"]["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs-jsonl", type=Path, required=True)
    ap.add_argument("--price-csv", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    prices = {r["custom_id"]: r for r in csv.DictReader(args.price_csv.open(encoding="utf-8-sig"))}
    rows = []
    for line in args.outputs_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        output = json.loads(line)
        cid = output.get("custom_id", "")
        payload = model_payload(output)
        text = " ".join(payload.get("news_lines") or [])
        price = prices.get(cid, {})
        reason = price.get("material_reason", "") or ""
        failures = []
        model_status = payload.get("status", "missing")
        if model_status != "accepted":
            failures.append(f"model_status:{model_status}")
        primary = ""
        if price.get("data_quality") not in {None, "", "ok"}:
            failures.append(f"price_data_quality:{price.get('data_quality')}")
        if "ret5d" in reason:
            primary = "ret5d"
            token = f"{abs(float(price['ret_5d'])):g}%"
        elif "ret1d" in reason:
            primary = "ret1d"
            token = f"{abs(float(price['ret_1d'])):g}%"
        elif "vol" in reason:
            primary = "volume"
            token = f"{float(price['vol_mult']):g}배"
        else:
            token = ""
            failures.append("missing_material_reason")
        if token and token not in text:
            failures.append(f"missing_primary_trigger:{primary}:{token}")
        broad = [term for term in BROAD_SECTORS if term in text]
        if broad:
            failures.append(f"broad_sector_comparison:{'|'.join(broad)}")
        rows.append({"custom_id": cid, "model_status": model_status,
                     "primary_trigger": primary, "required_token": token,
                     "news_text": text, "failures": ";".join(failures), "pass": not failures})

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "semantic_audit.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    passed = sum(r["pass"] for r in rows)
    primary_missing = sum("missing_primary_trigger" in r["failures"] for r in rows)
    broad = sum("broad_sector_comparison" in r["failures"] for r in rows)
    model_rejected = sum(r["model_status"] != "accepted" for r in rows)
    overlap = sum(
        "missing_primary_trigger" in r["failures"]
        and "broad_sector_comparison" in r["failures"]
        for r in rows
    )
    trigger_totals = Counter(r["primary_trigger"] or "unknown" for r in rows)
    trigger_passes = Counter(
        r["primary_trigger"] or "unknown" for r in rows if r["pass"]
    )
    trigger_lines = "\n".join(
        f"- {trigger}: {trigger_passes[trigger]}/{total} pass"
        for trigger, total in sorted(trigger_totals.items())
    )
    report = args.out_dir / "semantic_audit_report.md"
    report.write_text(
        "# Context regeneration semantic audit\n\n"
        f"- total: {len(rows)}\n- pass: {passed}\n- fail: {len(rows)-passed}\n"
        f"- missing primary material trigger: {primary_missing}\n"
        f"- broad sector comparison: {broad}\n"
        f"- model non-accepted: {model_rejected}\n"
        f"- both failures: {overlap}\n"
        f"- missing trigger only: {primary_missing - overlap}\n"
        f"- broad sector only: {broad - overlap}\n\n"
        "## Pass rate by primary trigger\n\n"
        f"{trigger_lines}\n",
        encoding="utf-8",
    )
    print(f"total={len(rows)} pass={passed} fail={len(rows)-passed} "
          f"missing_primary={primary_missing} broad_sector={broad}")
    print(f"-> {csv_path}\n-> {report}")
    if passed != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
