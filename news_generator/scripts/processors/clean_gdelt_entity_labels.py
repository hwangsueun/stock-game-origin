#!/usr/bin/env python3
"""Clean the noisy GDELT entity labels on the promoted event ledger.

GDELT GKG entity extraction over Korean-translated text produces labels polluted
with leading month/junk tokens ("Aug Samsung Group", "Korea Yeouido The National
Assembly"), title-cased Korean acronyms ("Sk Telecom", "Lg Group"), trailing role
fragments, and occasional two-entity concatenations ("Samsung Electronics Sk
Hynix"). Dedup across days is intentionally NOT done -- one event is legitimately
reported on several days.

Strategy: match a curated canonical Korean-entity dictionary as a substring
*anywhere* in the (acronym-normalised) label. That recovers the real entity even
when junk tokens sit in front. Each record gains:

  * entity_clean      -- acronym-corrected raw label (display)
  * entity_canonical  -- canonical English name, when a dictionary entry matched
  * entity_ko         -- Korean name for downstream Korean news generation
  * label_quality     -- canonical | concatenated | foreign | clean_unmapped | garbled

The source ledger is untouched; this writes a cleaned copy plus a review map.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


# Title-cased Korean acronyms / proper nouns -> correct casing.
_ACRONYM = {
    "Sk": "SK", "Lg": "LG", "Cj": "CJ", "Kb": "KB", "Gs": "GS", "Kt": "KT",
    "Ls": "LS", "Nh": "NH", "Sc": "SC", "Db": "DB", "Bnk": "BNK", "Dgb": "DGB",
    "Jb": "JB", "Ibk": "IBK", "Kdb": "KDB", "Sds": "SDS", "Cgv": "CGV",
    "Hmm": "HMM", "Kcc": "KCC", "Skc": "SKC", "Posco": "POSCO",
    "Kospi": "KOSPI", "Kosdaq": "KOSDAQ", "Lh": "LH", "Krx": "KRX",
}

# Foreign government markers -> not a domestic event.
_FOREIGN_GOV = re.compile(
    r"\b(china|japan|united states|france|poland|saudi arabia|germany|iran|"
    r"vietnam|taiwan|russia|india|north korea)\b.*\bministry\b|"
    r"\bministry of (commerce|finance)\b.*\b(china|united states)\b", re.I)

# Canonical dictionary: ordered, MOST SPECIFIC FIRST. Each entry is
# (lowercase keyword matched as substring, canonical English, Korean name).
_CANON: list[tuple[str, str, str]] = [
    # --- chaebol sub-entities (before their group) ---
    ("samsung electronics", "Samsung Electronics", "삼성전자"),
    ("samsung heavy", "Samsung Heavy Industries", "삼성중공업"),
    ("samsung engineering", "Samsung Engineering", "삼성엔지니어링"),
    ("samsung sds", "Samsung SDS", "삼성SDS"),
    ("samsung life", "Samsung Life", "삼성생명"),
    ("samsung c&t", "Samsung C&T", "삼성물산"),
    ("samsung", "Samsung Group", "삼성그룹"),
    ("sk hynix", "SK Hynix", "SK하이닉스"),
    ("sk telecom", "SK Telecom", "SK텔레콤"),
    ("sk energy", "SK Energy", "SK에너지"),
    ("sk networks", "SK Networks", "SK네트웍스"),
    ("sk construction", "SK Construction", "SK건설"),
    ("sk innovation", "SK Innovation", "SK이노베이션"),
    ("sk", "SK Group", "SK그룹"),
    ("lg electronics", "LG Electronics", "LG전자"),
    ("lg display", "LG Display", "LG디스플레이"),
    ("lg chem", "LG Chem", "LG화학"),
    ("lg uplus", "LG Uplus", "LG유플러스"),
    ("lg economic research", "LG Economic Research Institute", "LG경제연구원"),
    ("lg", "LG Group", "LG그룹"),
    ("hyundai heavy", "Hyundai Heavy Industries", "현대중공업"),
    ("hyundai oil bank", "Hyundai Oilbank", "현대오일뱅크"),
    ("hyundai motor", "Hyundai Motor", "현대자동차"),
    ("hyundai mobis", "Hyundai Mobis", "현대모비스"),
    ("hyundai", "Hyundai Group", "현대그룹"),
    ("lotte hotel", "Lotte Hotel", "롯데호텔"),
    ("lotte holdings", "Lotte Holdings", "롯데홀딩스"),
    ("lotte shopping", "Lotte Shopping", "롯데쇼핑"),
    ("lotte chemical", "Lotte Chemical", "롯데케미칼"),
    ("lotte", "Lotte Group", "롯데그룹"),
    ("kumho asiana", "Kumho Asiana Group", "금호아시아나그룹"),
    ("kumho industrial", "Kumho Industrial", "금호산업"),
    ("asiana", "Asiana Airlines", "아시아나항공"),
    ("korean air", "Korean Air", "대한항공"),
    ("kia", "Kia", "기아"),
    ("posco", "POSCO", "포스코"),
    ("hanwha", "Hanwha Group", "한화그룹"),
    ("doosan heavy", "Doosan Heavy Industries", "두산중공업"),
    ("doosan", "Doosan Group", "두산그룹"),
    ("cj", "CJ Group", "CJ그룹"),
    ("kb", "KB Financial Group", "KB금융그룹"),
    ("shinhan", "Shinhan", "신한"),
    ("hana financial", "Hana Financial Group", "하나금융그룹"),
    ("kakao", "Kakao", "카카오"),
    ("naver", "Naver", "네이버"),
    ("celltrion", "Celltrion", "셀트리온"),
    ("hanjin", "Hanjin", "한진"),
    # --- institutions / state bodies ---
    ("financial supervisory service", "Financial Supervisory Service", "금융감독원"),
    ("fair trade commission", "Fair Trade Commission", "공정거래위원회"),
    ("korea stock exchange", "Korea Exchange", "한국거래소"),
    ("korea exchange", "Korea Exchange", "한국거래소"),
    ("constitutional court", "Constitutional Court", "헌법재판소"),
    ("supreme prosecutor", "Supreme Prosecutors' Office", "대검찰청"),
    ("district prosecutor", "District Prosecutors' Office", "지방검찰청"),
    ("korea supreme court", "Supreme Court of Korea", "대법원"),
    ("national police agency", "National Police Agency", "경찰청"),
    ("bank of korea", "Bank of Korea", "한국은행"),
    ("blue house", "Blue House", "청와대"),
    ("national assembly", "National Assembly", "국회"),
    # --- ministries (GDELT short forms -> actual ROK ministry) ---
    ("ministry of strategy", "Ministry of Economy and Finance", "기획재정부"),
    ("ministry of finance", "Ministry of Economy and Finance", "기획재정부"),
    ("ministry of economy", "Ministry of Economy and Finance", "기획재정부"),
    ("ministry of industry", "Ministry of Trade, Industry and Energy", "산업통상자원부"),
    ("ministry of justice", "Ministry of Justice", "법무부"),
    ("ministry of defense", "Ministry of National Defense", "국방부"),
    ("ministry of foreign affairs", "Ministry of Foreign Affairs", "외교부"),
    ("ministry of education", "Ministry of Education", "교육부"),
    ("ministry of land", "Ministry of Land, Infrastructure and Transport", "국토교통부"),
    ("ministry of transportation", "Ministry of Land, Infrastructure and Transport", "국토교통부"),
    ("ministry of health", "Ministry of Health and Welfare", "보건복지부"),
    ("ministry of environment", "Ministry of Environment", "환경부"),
    ("ministry of labor", "Ministry of Employment and Labor", "고용노동부"),
    ("ministry of labour", "Ministry of Employment and Labor", "고용노동부"),
    ("ministry of employment", "Ministry of Employment and Labor", "고용노동부"),
    ("ministry of unification", "Ministry of Unification", "통일부"),
    ("ministry of maritime", "Ministry of Oceans and Fisheries", "해양수산부"),
    ("ministry of fisheries", "Ministry of Oceans and Fisheries", "해양수산부"),
    ("ministry of public administration", "Ministry of the Interior and Safety", "행정안전부"),
    ("ministry of culture", "Ministry of Culture, Sports and Tourism", "문화체육관광부"),
    ("ministry of science", "Ministry of Science and ICT", "과학기술정보통신부"),
    ("ministry of agriculture", "Ministry of Agriculture, Food and Rural Affairs", "농림축산식품부"),
    ("ministry of commerce", "Ministry of Trade, Industry and Energy", "산업통상자원부"),
    # --- political parties ---
    ("saenuri", "Saenuri Party", "새누리당"),
    ("democratic party", "Democratic Party", "더불어민주당"),
    ("democrats", "Democratic Party", "더불어민주당"),
    ("liberty korea", "Liberty Korea Party", "자유한국당"),
]

_JUNK_TOKENS = {"it", "term", "vice", "rep", "kim", "lee", "park", "the",
                "wavefront", "degrees", "seed", "junior", "senior"}


def fix_acronyms(label: str) -> str:
    return " ".join(_ACRONYM.get(tok, tok) for tok in label.split())


def canonicalize(label: str):
    """Return (canonical_en, ko, quality)."""
    norm = fix_acronyms(label)
    low = norm.lower()
    # Foreign governments take precedence so e.g. "China Ministry Of Foreign
    # Affairs" is never mis-mapped to Korea's MOFA.
    if _FOREIGN_GOV.search(norm):
        return None, None, "foreign"
    # Collect keyword matches with their character spans, then greedily accept
    # longest-first non-overlapping spans. A single entity whose name contains a
    # group keyword (e.g. "Hyundai Oil Bank" contains "hyundai") yields one
    # accepted span; two genuinely different entities ("Samsung Electronics SK
    # Hynix") yield two.
    matches = []  # (pos, end, canonical_en, ko)
    for kw, en, ko in _CANON:
        pos = low.find(kw)
        if pos >= 0:
            matches.append((pos, pos + len(kw), en, ko))
    accepted = []  # (pos, end, en, ko)
    for pos, end, en, ko in sorted(matches, key=lambda m: -(m[1] - m[0])):
        if any(pos < a_end and a_pos < end for a_pos, a_end, _, _ in accepted):
            continue
        accepted.append((pos, end, en, ko))
    if accepted:
        accepted.sort(key=lambda a: a[0])
        distinct = {a[2] for a in accepted}
        en, ko = accepted[0][2], accepted[0][3]
        quality = "canonical" if len(distinct) == 1 else "concatenated"
        return en, ko, quality
    # No dictionary hit: decide clean vs garbled.
    tokens = [t for t in re.split(r"\s+", low) if t]
    junk = sum(1 for t in tokens if t in _JUNK_TOKENS)
    if len(tokens) > 4 or junk >= 1 or len(tokens) <= 1:
        return None, None, "garbled"
    return None, None, "clean_unmapped"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--in-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/promoted_events.jsonl"))
    parser.add_argument(
        "--out-jsonl", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/promoted_events_cleaned.jsonl"))
    parser.add_argument(
        "--map-csv", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/entity_label_map.csv"))
    parser.add_argument(
        "--report", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg/ENTITY_CLEANUP_REPORT.md"))
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.in_jsonl.open(encoding="utf-8") if line.strip()]

    quality_counts = Counter()
    raw_map: dict[str, dict] = {}
    out_rows = []
    for r in rows:
        raw = r["salient_entity"]
        clean = fix_acronyms(raw)
        en, ko, quality = canonicalize(raw)
        quality_counts[quality] += 1
        # generation_ready: a real, single, domestic entity we can name in Korean.
        gen_ready = quality in {"canonical", "concatenated"} and ko is not None
        rec = {
            **r,
            "salient_entity_raw": raw,
            "entity_clean": clean,
            "entity_canonical": en,
            "entity_ko": ko,
            "label_quality": quality,
            "label_generation_ready": gen_ready,
        }
        out_rows.append(rec)
        m = raw_map.setdefault(raw, {
            "salient_entity_raw": raw, "entity_clean": clean,
            "entity_canonical": en or "", "entity_ko": ko or "",
            "label_quality": quality, "records": 0})
        m["records"] += 1

    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.out_jsonl.open("w", encoding="utf-8") as h:
        for r in out_rows:
            h.write(json.dumps(r, ensure_ascii=False) + "\n")

    map_rows = sorted(raw_map.values(), key=lambda m: -m["records"])
    with args.map_csv.open("w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=["salient_entity_raw", "entity_clean",
                                          "entity_canonical", "entity_ko",
                                          "label_quality", "records"])
        w.writeheader()
        w.writerows(map_rows)

    gen_ready = sum(1 for r in out_rows if r["label_generation_ready"])
    canon_records = quality_counts["canonical"] + quality_counts["concatenated"]
    distinct_canon = len({r["entity_canonical"] for r in out_rows if r["entity_canonical"]})
    top_ko = Counter(r["entity_ko"] for r in out_rows if r["entity_ko"])
    report = [
        "# GKG promoted-event entity label cleanup", "",
        f"- input promoted records: {len(rows):,} "
        f"({len(raw_map):,} distinct raw labels)",
        f"- generation-ready (mapped to a Korean entity name): {gen_ready:,} "
        f"({gen_ready / len(rows) * 100:.0f}%) across {distinct_canon} canonical entities",
        "", "## Records by label_quality", "",
    ]
    for q in ("canonical", "concatenated", "clean_unmapped", "foreign", "garbled"):
        report.append(f"- {q}: {quality_counts[q]:,}")
    report += ["", "## Top canonical entities (Korean) in promoted set", ""]
    report.extend(f"- {ko}: {cnt:,}" for ko, cnt in top_ko.most_common(25))
    report += ["", "Review the full raw->canonical mapping in "
               f"`{args.map_csv.name}`. Records with label_quality in "
               "{foreign, garbled} are flagged and not generation-ready.", ""]
    args.report.write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"records={len(rows):,} distinct_raw={len(raw_map):,} "
          f"generation_ready={gen_ready:,} canonical_records={canon_records:,} "
          f"distinct_canonical={distinct_canon}")
    for q in ("canonical", "concatenated", "clean_unmapped", "foreign", "garbled"):
        print(f"  {q}: {quality_counts[q]:,}")
    print(f"-> {args.out_jsonl}")


if __name__ == "__main__":
    main()
