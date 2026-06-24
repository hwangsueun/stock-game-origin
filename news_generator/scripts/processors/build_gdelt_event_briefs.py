#!/usr/bin/env python3
"""Turn promoted GKG events into copyright-safe, fabrication-free event briefs.

We never stored or re-use article prose (copyright). Instead each brief is built
only from facts we legitimately hold: the Korean entity name, the publication
date, GDELT's own open-data theme classification, GDELT's computed tone, the
count of independent source domains, and the evidence URLs. The GKG theme codes
are mapped to a controlled Korean event-type vocabulary -- a factual
classification, not invented content.

The resulting `brief_ko` states only what is provably true: that on date D an
entity was the focus of an event of type T, covered by N independent outlets,
with a measured tone. No specific claims about *what* happened are fabricated.
Richer, specific facts (e.g. DART disclosures for corporate events, National
Assembly bill records for legislative ones) can later be joined in to deepen the
brief -- those are also copyright-free public primary sources.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


# GKG theme code -> Korean event type. Ordered MOST SPECIFIC FIRST; first match
# on the event's theme_signature wins. Generic political/leadership themes sit at
# the bottom so a specific signal (impeachment, IPO, investigation...) takes over.
_THEME_TYPE: list[tuple[str, str]] = [
    ("IMPEACHMENT", "탄핵"),
    ("ECON_IPO", "기업공개(IPO)·상장"),
    ("ECON_INTEREST_RATES", "금리·통화정책"),
    ("WB_2025_INVESTIGATION", "수사·조사"),
    ("WB_2024_ANTI_CORRUPTION_AUTHORITIES", "비리·부패 의혹"),
    ("WB_832_ANTI_CORRUPTION", "비리·부패 의혹"),
    ("TRIAL", "재판·판결"),
    ("WB_1014_CRIMINAL_JUSTICE", "사법·형사"),
    ("WB_840_JUSTICE", "사법·법무"),
    ("MANMADE_DISASTER_IMPLIED", "사고·재난"),
    ("NATURAL_DISASTER", "자연재해"),
    ("STRIKE", "파업·노사"),
    ("EPU_CATS_REGULATION", "규제·정책"),
    ("ECON_DEBT", "재정·부채"),
    ("WB_450_DEBT", "재정·부채"),
    ("ENV_OIL", "에너지·유가"),
    ("WB_507_ENERGY_AND_EXTRACTIVES", "에너지·자원"),
    ("WB_470_EDUCATION", "교육"),
    ("EDUCATION", "교육"),
    ("EPU_POLICY_BLUE_HOUSE", "청와대·정부 정책"),
    ("ECON_STOCKMARKET", "증시·주가"),
    ("ECON_", "경제·산업"),
    ("WB_831_GOVERNANCE", "지배구조·거버넌스"),
    ("WB_696_PUBLIC_SECTOR_MANAGEMENT", "공공정책·행정"),
    ("TAX_FNCACT_CANDIDATE", "정치·선거"),
    ("USPEC_POLITICS", "정치"),
    ("GENERAL_GOVERNMENT", "정부·행정"),
    ("LEADER", "경영진·리더십"),
]


def event_type_ko(themes: list[str]) -> str:
    joined = ";".join(themes)
    for code, label in _THEME_TYPE:
        for t in themes:
            if t == code or t.startswith(code):
                return label
    return "주요"


def tone_phrase(avg_tone) -> str:
    if avg_tone is None:
        return "중립적"
    if avg_tone <= -2.0:
        return "부정적"
    if avg_tone >= 2.0:
        return "긍정적"
    return "중립적"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--in-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/promoted_events_cleaned.jsonl"))
    parser.add_argument(
        "--out-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/event_briefs.jsonl"))
    parser.add_argument(
        "--report", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/EVENT_BRIEFS_REPORT.md"))
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.in_jsonl.open(encoding="utf-8") if line.strip()]
    rows = [r for r in rows if r.get("label_generation_ready")]

    briefs = []
    type_counts = Counter()
    for r in rows:
        etype = event_type_ko(r.get("theme_signature") or [])
        type_counts[etype] += 1
        tone = tone_phrase(r.get("avg_tone"))
        entity = r["entity_ko"]
        nd = r["n_distinct_domains"]
        # Only provable facts: focus entity, date, GDELT-classified type, outlet
        # corroboration count, measured tone. No specific event claims invented.
        brief = (f"{r['ref_date']}, {entity} 관련 {etype} 현안이 "
                 f"국내 {nd}개 매체에서 비중 있게 보도됐다. "
                 f"보도 논조는 대체로 {tone}이었다.")
        briefs.append({
            "event_id": r["event_id"],
            "ref_date": r["ref_date"],
            "available_date_conservative": r["available_date_conservative"],
            "entity_ko": entity,
            "entity_canonical": r.get("entity_canonical"),
            "event_type_ko": etype,
            "tone_label": tone,
            "avg_tone": r.get("avg_tone"),
            "n_distinct_domains": nd,
            "theme_signature": r.get("theme_signature"),
            "source_domains": r.get("source_domains"),
            "evidence": r.get("evidence"),
            "brief_ko": brief,
            "content_basis": "gdelt_open_metadata_only_no_article_text",
            "fact_grounding": "salience_and_type_only",  # upgrade via DART / gov data
            "generation_eligible": True,
        })
    briefs.sort(key=lambda b: (b["ref_date"], -b["n_distinct_domains"]))

    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.out_jsonl.open("w", encoding="utf-8") as h:
        for b in briefs:
            h.write(json.dumps(b, ensure_ascii=False) + "\n")

    report = [
        "# GKG copyright-safe event briefs", "",
        "Each brief is built only from facts we hold (entity, date, GDELT theme "
        "classification, tone, independent-domain count, URLs). No article text "
        "is reused and no specific event details are fabricated.", "",
        f"- briefs: {len(briefs):,}", "",
        "## Event-type distribution (from GDELT themes)", "",
    ]
    report.extend(f"- {t}: {c:,}" for t, c in type_counts.most_common())
    report += ["", "## Sample briefs (highest coverage)", ""]
    for b in sorted(briefs, key=lambda b: -b["n_distinct_domains"])[:15]:
        report.append(f"- {b['brief_ko']}")
    args.report.write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"briefs={len(briefs):,}")
    for t, c in type_counts.most_common(12):
        print(f"  {t}: {c:,}")
    print(f"-> {args.out_jsonl}")


if __name__ == "__main__":
    main()
