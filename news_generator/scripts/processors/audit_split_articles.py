#!/usr/bin/env python3
"""Audit disclosure/follow-up article pairs for chronology and claim policy."""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path


BAN = ["공시 후", "때문에", "덕분에", "영향으로", "호재", "악재", "긍정적", "부정적", "평균 톤"]
BROAD_SECTORS = ["제조 업종지수", "금융 업종지수", "일반서비스 업종지수"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles-jsonl", type=Path, required=True)
    ap.add_argument("--price-csv", type=Path, required=True)
    args = ap.parse_args()

    rows = [json.loads(line) for line in args.articles_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    prices = {r["custom_id"]: r for r in csv.DictReader(args.price_csv.open(encoding="utf-8-sig"))}
    pairs = defaultdict(dict)
    failures = []
    for row in rows:
        pairs[row["source_custom_id"]][row["article_type"]] = row
        text = " ".join(row["news_lines"])
        hits = [term for term in BAN if term in text]
        if hits:
            failures.append((row["article_id"], f"banned_terms:{'|'.join(hits)}"))
        broad = [term for term in BROAD_SECTORS if term in text]
        if broad:
            failures.append((row["article_id"], f"broad_sector:{'|'.join(broad)}"))

    for cid, pair in pairs.items():
        disclosure = pair.get("disclosure")
        reaction = pair.get("market_reaction_followup")
        if not disclosure or not reaction:
            failures.append((cid, "missing_pair"))
            continue
        if date.fromisoformat(reaction["publish_date"]) <= date.fromisoformat(disclosure["publish_date"]):
            failures.append((cid, "non_future_followup_date"))
        text = " ".join(reaction["news_lines"])
        reason = prices[cid]["material_reason"]
        if "ret5d" in reason:
            if f"{abs(float(prices[cid]['ret_5d'])):g}%" not in text:
                failures.append((cid, "missing_trigger_ret5d"))
        elif "ret1d" in reason:
            if f"{abs(float(prices[cid]['ret_1d'])):g}%" not in text:
                failures.append((cid, "missing_trigger_ret1d"))
        elif reason.startswith("vol") and f"{float(prices[cid]['vol_mult']):g}배" not in text:
            failures.append((cid, "missing_trigger_volume"))

    print(f"articles={len(rows)} pairs={len(pairs)} failures={len(failures)}")
    for failure in failures:
        print("FAIL", failure[0], failure[1])
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
