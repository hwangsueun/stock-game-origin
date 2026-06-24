# ============================================================
# build_weo_projection_overlay.py
# IMF WEO 전망치를 입력 JSONL에 'projection' 이벤트로 주입한다.
# WEO 발표 고정일(release_date)을 입력의 다음 거래일로 스냅해 추가한다.
# ============================================================

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

VAR_LABEL = {"real_gdp": "실질 GDP 성장률", "inflation": "소비자물가 상승률"}


def build_event(kr_date: str, edition: str, recs: List[Dict[str, str]]) -> Dict[str, Any]:
    projections: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    years = sorted({r["year"] for r in recs})
    near = years[:2]
    for r in recs:
        projections[r["country"]][r["variable"]][r["year"]] = float(r["value"])

    key_figures: Dict[str, float] = {}
    for r in recs:
        if r["year"] in near:
            iso = r["iso"].lower()
            key_figures[f"{iso}_{r['variable']}_{r['year']}_pct"] = float(r["value"])

    return {
        "event_id": f"{kr_date}_projection_imf_weo",
        "event_role": "projection",
        "source_columns": ["official_release_calendar"],
        "macro_angle": "outlook",
        "angle_label": "IMF 세계경제전망(WEO)",
        "direction": "neutral",
        "severity": "moderate",
        "evidence": {
            "institution": "국제통화기금(IMF)",
            "required_attribution": "IMF",
            "allowed_release_verbs": ["전망했다", "예상했다", "내다봤다", "제시했다"],
            "release_category": "projection",
            "weo_edition": edition,
            "projection_note": (
                f"IMF가 {edition} 세계경제전망(WEO)에서 한국·미국의 성장률·물가 전망을 발표했다."
            ),
            "variable_labels": VAR_LABEL,
            "projections": {c: dict(v) for c, v in projections.items()},
        },
        "key_figures": key_figures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--weo-csv", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    args = parser.parse_args()

    by_edition: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    release_of: Dict[str, str] = {}
    with args.weo_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            by_edition[row["weo_edition"]].append(row)
            release_of[row["weo_edition"]] = row["release_date"]

    rows = []
    with args.input_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("date", ""))
    dates = [r.get("date", "") for r in rows]
    by_date = {r["date"]: r for r in rows}

    def next_trading_day(target: str) -> str | None:
        for d in dates:
            if d >= target:
                return d
        return None

    injected = 0
    for edition, recs in by_edition.items():
        snap = next_trading_day(release_of[edition])
        if snap is None:
            continue
        event = build_event(snap, edition, recs)
        by_date[snap]["macro_events"] = list(by_date[snap].get("macro_events", [])) + [event]
        injected += 1

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as handle:
        for record in rows:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"입력 {len(rows)}일 → WEO 전망 이벤트 주입 {injected}건 → {args.output_jsonl}")


if __name__ == "__main__":
    main()
