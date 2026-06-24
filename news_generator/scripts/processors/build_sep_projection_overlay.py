# ============================================================
# build_sep_projection_overlay.py
# FOMC SEP 전망치를 입력 JSONL에 'projection' 이벤트로 주입한다.
# SEP는 FOMC 성명과 함께 나오므로, 해당 FOMC의 한국 편입일(available_date_kr)에 추가한다.
# ============================================================

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

VAR_LABEL = {
    "real_gdp": "실질 GDP 성장률",
    "pce_inflation": "PCE 물가상승률",
    "unemployment": "실업률",
    "federal_funds_rate": "기준금리(점도표 중앙값)",
}


def build_projection_event(kr_date: str, us_date: str, recs: List[Dict[str, str]]) -> Dict[str, Any]:
    projections: Dict[str, Dict[str, float]] = defaultdict(dict)
    for r in recs:
        projections[r["variable"]][r["projection_year"]] = float(r["median"])

    sep_year = us_date[:4]
    key_figures: Dict[str, float] = {}
    for var in ("real_gdp", "pce_inflation", "federal_funds_rate"):
        for year in (sep_year, str(int(sep_year) + 1)):
            if year in projections.get(var, {}):
                key_figures[f"{var}_{year}_pct"] = projections[var][year]

    return {
        "event_id": f"{kr_date}_projection_fomc_sep",
        "event_role": "projection",
        "source_columns": ["official_release_calendar"],
        "macro_angle": "outlook",
        "angle_label": "연준 경제전망(SEP)",
        "direction": "neutral",
        "severity": "moderate",
        "evidence": {
            "institution": "미국 연방준비제도(연준)",
            "required_attribution": "연준",
            "allowed_release_verbs": ["전망했다", "예상했다", "내다봤다", "제시했다"],
            "release_category": "projection",
            "sep_release_date": us_date,
            "projection_note": (
                f"연준이 {us_date} FOMC에서 경제전망(SEP)을 발표했다. "
                f"실질 GDP·PCE 물가·기준금리(점도표) 연도별 중앙값 전망."
            ),
            "variable_labels": VAR_LABEL,
            "projections": {var: projections[var] for var in projections},
        },
        "key_figures": key_figures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--sep-csv", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    args = parser.parse_args()

    by_kr_date: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    with args.sep_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            by_kr_date[row["available_date_kr"]].append(row)

    rows = []
    with args.input_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    injected = 0
    for record in rows:
        date = record.get("date", "")
        if date in by_kr_date:
            recs = by_kr_date[date]
            us_date = recs[0]["sep_us_date"]
            event = build_projection_event(date, us_date, recs)
            record["macro_events"] = list(record.get("macro_events", [])) + [event]
            injected += 1

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as handle:
        for record in rows:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"입력 {len(rows)}일 → 전망 이벤트 주입 {injected}일")
    matched = sum(1 for d in by_kr_date if any(r.get('date') == d for r in rows))
    print(f"SEP 회의 {len(by_kr_date)}개 중 입력 날짜와 매칭 {matched}개 → {args.output_jsonl}")


if __name__ == "__main__":
    main()
