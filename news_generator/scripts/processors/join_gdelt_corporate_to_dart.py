#!/usr/bin/env python3
"""Ground GKG corporate events in DART disclosure facts, and measure net-new vs
redundant coverage.

DART disclosures (public regulatory filings, not copyrighted) are the richest
copyright-safe fact source for the ~46% corporate slice of the promoted GKG
events. This joins each corporate GKG spike to DART detail facts for the matching
listed companies in our 117-stock universe, within a date window, and classifies:

  * not_in_universe      -- entity has no listed stock we cover (e.g. 포스코,
                            아시아나, 현대오일뱅크). Net-new but no DART facts.
  * dart_grounded        -- a DART disclosure sits near the spike date. The
                            existing stock-news pipeline already processes every
                            such disclosure, so these are largely REDUNDANT;
                            GKG only re-confirms salience. We still attach the
                            disclosure facts here for a richer brief.
  * in_universe_no_dart  -- covered stock but no nearby disclosure: the spike was
                            non-operational (governance / legal / M&A drama).
                            Net-new, but DART cannot ground it.

No article text is touched. Output is a join dataset + a measurement report.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


# GKG group root (matched on entity_ko prefix) -> Korean stock-name prefix used
# to gather every affiliated listed company in our universe.
_GROUP_ROOTS = {
    "삼성": "삼성", "롯데": "롯데", "SK": "SK", "LG": "LG", "현대": "현대",
    "한화": "한화", "기아": "기아", "대한항공": "대한항공", "한진": "한진",
    "포스코": "포스코", "아시아나": "아시아나", "두산": "두산", "금호": "금호",
    "CJ": "CJ", "KT": "KT", "GS": "GS",
}


def _rcept_date(row: dict) -> str:
    rn = row.get("﻿rcept_no") or row.get("rcept_no") or ""
    return rn[:8]


def _ko_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _group_root(entity_ko: str) -> str | None:
    for root in _GROUP_ROOTS:
        if entity_ko.startswith(root):
            return root
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--events-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/promoted_events_cleaned.jsonl"))
    parser.add_argument(
        "--dart-detail-csv", type=Path,
        default=Path("data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv"))
    parser.add_argument("--stocklist", type=Path, default=Path("../stocklist.txt"))
    parser.add_argument(
        "--out-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/corporate_dart_join.jsonl"))
    parser.add_argument(
        "--report", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/CORPORATE_DART_JOIN_REPORT.md"))
    parser.add_argument("--window-days", type=int, default=3)
    args = parser.parse_args()

    # --- universe: stock_name -> code, and name set ---
    dart_rows = list(csv.DictReader(args.dart_detail_csv.open(encoding="utf-8")))
    code_of_name = {}
    disclosures_by_code: dict[str, list] = defaultdict(list)
    for r in dart_rows:
        name = r["stock_name"]
        code = r["stock_code"]
        code_of_name[name] = code
        d = _rcept_date(r)
        if d:
            disclosures_by_code[code].append({
                "date": _ko_date(d),
                "report_name": r["report_name"],
                "rcept_no": (r.get("﻿rcept_no") or r.get("rcept_no")),
                "fact_count": r.get("fact_count"),
            })
    universe_names = list(code_of_name)

    # group root -> [(name, code)] in universe
    root_to_stocks: dict[str, list] = defaultdict(list)
    for root, prefix in _GROUP_ROOTS.items():
        for name in universe_names:
            if name.startswith(prefix):
                root_to_stocks[root].append((name, code_of_name[name]))

    # --- corporate GKG events ---
    events = [json.loads(line) for line in args.events_jsonl.open(encoding="utf-8") if line.strip()]
    events = [e for e in events if e.get("label_generation_ready")]
    corporate = [e for e in events if _group_root(e["entity_ko"]) is not None]

    win = timedelta(days=args.window_days)
    out = []
    cls_counts = Counter()
    not_universe_ents = Counter()
    netnew_ents = Counter()
    grounded_reports = Counter()
    for e in corporate:
        root = _group_root(e["entity_ko"])
        stocks = root_to_stocks.get(root, [])
        ref = datetime.strptime(e["ref_date"], "%Y-%m-%d")
        if not stocks:
            klass = "not_in_universe"
            not_universe_ents[e["entity_ko"]] += 1
            matched = []
        else:
            matched = []
            for name, code in stocks:
                for disc in disclosures_by_code.get(code, []):
                    dd = datetime.strptime(disc["date"], "%Y-%m-%d")
                    if abs((dd - ref).days) <= args.window_days:
                        matched.append({"stock_name": name, "stock_code": code, **disc})
            if matched:
                klass = "dart_grounded"
                for m in matched:
                    grounded_reports[m["report_name"]] += 1
            else:
                klass = "in_universe_no_dart"
                netnew_ents[e["entity_ko"]] += 1
        cls_counts[klass] += 1
        out.append({
            "event_id": e["event_id"],
            "ref_date": e["ref_date"],
            "entity_ko": e["entity_ko"],
            "entity_canonical": e.get("entity_canonical"),
            "group_root": root,
            "n_distinct_domains": e["n_distinct_domains"],
            "join_class": klass,
            "candidate_stocks": [n for n, _ in stocks],
            "dart_matches": matched[:8],
            "fact_grounding": ("dart_disclosure" if klass == "dart_grounded"
                               else "none_available"),
        })

    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.out_jsonl.open("w", encoding="utf-8") as h:
        for r in out:
            h.write(json.dumps(r, ensure_ascii=False) + "\n")

    tot = len(corporate)
    report = [
        "# GKG corporate events -> DART grounding (net-new vs redundant)", "",
        f"- corporate GKG promoted events: {tot:,} "
        f"(of {len(events):,} generation-ready)",
        f"- date window for a nearby disclosure: +/-{args.window_days} days", "",
        "## Join classification", "",
        f"- dart_grounded (disclosure nearby; existing pipeline already covers it, "
        f"GKG re-confirms salience): {cls_counts['dart_grounded']:,} "
        f"({cls_counts['dart_grounded']/tot*100:.0f}%)",
        f"- in_universe_no_dart (covered stock, no disclosure -> governance/legal "
        f"spike, NET-NEW but no DART facts): {cls_counts['in_universe_no_dart']:,} "
        f"({cls_counts['in_universe_no_dart']/tot*100:.0f}%)",
        f"- not_in_universe (no listed stock we cover -> NET-NEW, no DART facts): "
        f"{cls_counts['not_in_universe']:,} ({cls_counts['not_in_universe']/tot*100:.0f}%)",
        "", "## not_in_universe entities (need a non-DART source)", "",
    ]
    report.extend(f"- {e}: {c:,}" for e, c in not_universe_ents.most_common(12))
    report += ["", "## in_universe_no_dart entities (governance/legal net-new)", ""]
    report.extend(f"- {e}: {c:,}" for e, c in netnew_ents.most_common(12))
    report += ["", "## DART report types that grounded GKG spikes", ""]
    report.extend(f"- {r}: {c:,}" for r, c in grounded_reports.most_common(10))
    report += [
        "", "## Caveat: date-window matches are NOT safe grounding", "",
        "GKG entities are group-level (삼성그룹) while DART is affiliate-level "
        "(삼성SDI, 삼성생명). A +/-window match therefore attaches whatever "
        "affiliate filing happens to fall nearby, even when it is a different "
        "story: the 2018-04 삼성그룹 spike (Samsung Securities ghost-share "
        "incident) gets matched to a 삼성SDI equity-disposal filing; the 2017-02 "
        "삼성그룹 spike (Lee Jae-yong bribery trial) to a 삼성생명 earnings-"
        "structure filing. Using these as facts would FABRICATE a false "
        "association. Conclusion: DART must not be auto-joined by date window to "
        "ground GKG corporate spikes. The dart_grounded slice is both small (20% "
        "of corporate, mostly already covered by the stock pipeline) and "
        "unreliable. The theme-typed event brief stays the safe representation; "
        "real enrichment needs precise event-to-source matching, not a date join.",
        "",
    ]
    args.report.write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"corporate={tot:,}")
    for k in ("dart_grounded", "in_universe_no_dart", "not_in_universe"):
        print(f"  {k}: {cls_counts[k]:,} ({cls_counts[k]/tot*100:.0f}%)")
    print(f"-> {args.out_jsonl}")


if __name__ == "__main__":
    main()
