#!/usr/bin/env python3
"""Audit generated macro-news against their cited source events.

유연 병합(기사 1건이 여러 이벤트 인용) + 프리뷰/리뷰 이벤트를 지원한다.
- 커버리지: 입력의 모든 event_id가 어느 기사에든 최소 1회 인용돼야 한다.
- 숫자 근거: 기사의 숫자는 그 기사가 인용한 이벤트들의 evidence/key_figures 합집합에서 나와야 한다.
- 금지어: 추측·심리·전망 등 단정 표현만 차단(영향·반영·배경 등 사실 연결어는 허용).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


NUMBER = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
FORBIDDEN_INFERENCE = (
    "투자자", "외국인", "기대", "전망", "우려", "심리", "가능성", "견인",
    "것으로 보인다", "예상된다", "기대된다", "수 있다", "해석된다", "해석될",
)


def numbers(value: object) -> list[float]:
    found = []
    for token in NUMBER.findall(json.dumps(value, ensure_ascii=False)):
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            pass
    return found


def matches_source(value: float, allowed: list[float]) -> bool:
    tolerance = 0.51 if abs(value) >= 100 else 0.011
    return any(
        min(abs(value - source), abs(abs(value) - abs(source)))
        <= max(tolerance, abs(source) * 0.0001)
        for source in allowed
    )


def matches_approximate_percent_band(value: float, text: str, allowed: list[float]) -> bool:
    token = f"{value:g}%대"
    return token in text and any(value <= abs(source) < value + 1 for source in allowed)


def attribution_satisfied(detail: str, evidence: dict) -> bool:
    institution = str(evidence.get("institution") or "")
    candidates = [str(evidence.get("required_attribution") or ""), institution]
    acronym = re.search(r"\(([^)]+)\)", institution)
    if acronym:
        candidates.append(acronym.group(1))
    return any(c and c in detail for c in candidates)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--generated-csv", type=Path, required=True)
    args = parser.parse_args()

    def is_must_cover(event: dict) -> bool:
        cols = set(event.get("source_columns") or [])
        role = str(event.get("event_role") or "")
        eid = str(event.get("event_id") or "")
        return (
            "official_release_calendar" in cols
            or role in ("preview", "review", "headline", "projection")
            or "major_stock" in eid
        )

    events = {}
    must_cover_by_date = {}
    with args.input_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            must = []
            for event in record.get("macro_events") or []:
                event_id = event["event_id"]
                events[event_id] = event
                if is_must_cover(event):
                    must.append(event_id)
            must_cover_by_date[record["date"]] = must

    rows_by_date = defaultdict(list)
    failures = Counter()
    examples = defaultdict(list)
    with args.generated_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows_by_date[row["date"]].append(row)

            try:
                source_ids = json.loads(row["source_event_ids"])
            except (json.JSONDecodeError, KeyError):
                source_ids = []
            if not isinstance(source_ids, list) or not source_ids:
                failures["empty_source_event_ids"] += 1
                continue
            unknown = [s for s in source_ids if s not in events]
            if unknown:
                failures["invalid_source_event_ids"] += 1
                continue

            cited = [events[s] for s in source_ids]
            allowed_numbers = numbers([
                {"evidence": e.get("evidence"), "key_figures": e.get("key_figures")}
                for e in cited
            ])
            # 신문 날짜 리드("3일", "1월" 등)의 숫자는 기사 날짜에서 온 것이므로 허용한다.
            allowed_numbers += [float(part) for part in row.get("date", "").split("-") if part.isdigit()]
            output_numbers = numbers({
                "headline": row["headline"],
                "detail_news": row["detail_news"],
                "used_evidence": row["used_evidence"],
            })
            output_text = f"{row['headline']} {row['detail_news']}"
            unsupported = [
                value for value in output_numbers
                if not matches_source(value, allowed_numbers)
                and not matches_approximate_percent_band(value, output_text, allowed_numbers)
            ]
            if unsupported:
                failures["unsupported_numeric_claim"] += 1
                examples["unsupported_numeric_claim"].append({
                    "news_id": row["news_id"], "values": unsupported,
                })

            allowed_verbs = {
                v for e in cited
                for v in ((e.get("evidence") or {}).get("allowed_release_verbs") or [])
            }
            found_words = [
                word for word in FORBIDDEN_INFERENCE
                if word in row["detail_news"] and not any(word in v for v in allowed_verbs)
            ]
            if found_words:
                failures["inference_language"] += 1
                examples["inference_language"].append({
                    "news_id": row["news_id"], "words": found_words,
                })

            for event in cited:
                is_attributed_event = (
                    "official_release_calendar" in set(event.get("source_columns") or [])
                    or str(event.get("event_role") or "") == "projection"
                )
                if not is_attributed_event:
                    continue
                evidence = event.get("evidence") or {}
                allowed_release_verbs = list(evidence.get("allowed_release_verbs") or [])
                if not attribution_satisfied(row["detail_news"], evidence):
                    failures["official_release_missing_attribution"] += 1
                if allowed_release_verbs and not any(
                    verb in row["detail_news"] for verb in allowed_release_verbs
                ):
                    failures["official_release_missing_verb"] += 1

            if not 20 <= len(row["detail_news"]) <= 280:
                failures["length_out_of_range"] += 1

            explanation = row.get("beginner_explanation", "").strip()
            if not 25 <= len(explanation) <= 120:
                failures["beginner_explanation_length"] += 1
            if any(char.isdigit() for char in explanation):
                failures["beginner_explanation_has_number"] += 1
            if re.search(r"-\s*\d[\d,.]*\s*%p\s*(?:앞섰|뒤처)", row["detail_news"]):
                failures["double_signed_relative_gap"] += 1
            if len(source_ids) == 1:
                event_id = source_ids[0]
                rate_unit = "bp" in explanation or "베이시스포인트" in explanation
                if ("rate_spread" in event_id or "global_us_rates" in event_id) and not (
                    rate_unit and "단위" in explanation
                ):
                    failures["missing_bp_explanation"] += 1

    for date, must_ids in must_cover_by_date.items():
        rows = rows_by_date.get(date, [])
        if not rows:
            failures["no_articles_for_day"] += 1
            examples["no_articles_for_day"].append({"date": date})
            continue
        if len(rows) != 5:
            failures["wrong_article_count"] += 1
            examples["wrong_article_count"].append({"date": date, "count": len(rows)})
        used = []
        for row in rows:
            try:
                used.extend(json.loads(row["source_event_ids"]))
            except (json.JSONDecodeError, KeyError):
                pass
        missing = set(must_ids) - set(used)
        if missing:
            failures["must_cover_missing"] += 1
            examples["must_cover_missing"].append({
                "date": date, "missing": sorted(missing),
            })
        explanations = [row.get("beginner_explanation", "").strip() for row in rows]
        if len(explanations) != len(set(explanations)):
            failures["duplicate_beginner_explanation"] += 1

    result = {
        "status": "PASS" if not failures else "FAIL",
        "days": len(must_cover_by_date),
        "generated_rows": sum(map(len, rows_by_date.values())),
        "failures": dict(failures),
        "examples": {key: value[:5] for key, value in examples.items()},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
