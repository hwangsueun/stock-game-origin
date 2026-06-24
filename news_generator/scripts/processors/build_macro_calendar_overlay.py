# ============================================================
# build_macro_calendar_overlay.py
# with_releases 입력에 '예정 발표 프리뷰(T-1)'와 '발표 회고 리뷰(T+1)' 이벤트를 주입한다.
# - 프리뷰: 공식 발표가 편입되는 거래일의 직전 거래일에 추가(결과 수치 없음, 일정만).
# - 리뷰  : 공식 발표 편입일의 다음 거래일에 추가(결과 수치 포함, 회고).
# 원천: 입력에 이미 들어있는 공식 발표 이벤트(required_attribution·release_category·
#       reference_period·key_figures)를 그대로 활용하므로 새 사실을 만들지 않는다.
# ============================================================

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

CATEGORY_LABEL = {
    "monetary_policy": "정책금리 결정",
    "growth": "실질 GDP 속보치",
    "inflation": "PCE 물가지수",
    "trade": "상품·서비스 무역수지",
    "legislation": "국회 본회의 안건",
    "court_ruling": "헌법재판소 심판",
    "election": "공직선거",
    "regulatory_action": "규제기관 조치",
}

# 사전 공지(스케줄)되는 카테고리만 프리뷰(T-1). 규제 제재는 사전 미공지라 제외(look-ahead 방지).
PREVIEW_ELIGIBLE = {
    "monetary_policy", "growth", "inflation", "trade",
    "legislation", "court_ruling", "election",
}
# 카테고리별 '예정' 동사(없으면 발표)
SCHEDULED_VERB = {"legislation": "처리", "court_ruling": "선고", "election": "실시"}
# 프리뷰(T-1)는 결과를 모르는 시점이므로 제목에서 결과(주문/처리결과)를 제거(look-ahead 방지).
OUTCOME_TOKENS = {
    "court_ruling": ("인용", "기각", "각하", "헌법불합치", "한정위헌", "위헌", "합헌"),
    "legislation": ("원안가결", "수정가결", "가결", "부결", "대안반영폐기"),
}


def strip_preview_outcome(title: str, category: str) -> str:
    cleaned = title
    for tok in OUTCOME_TOKENS.get(category, ()):
        cleaned = cleaned.replace(tok, "")
    return re.sub(r"\s+", " ", cleaned).strip(" '\"")


_JOSA_LATIN_BATCHIM = set("FLMNRZ")
_JOSA_DIGIT_BATCHIM = set("1368")


def josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """끝소리 받침 여부로 붙일 조사를 반환(영문·숫자는 한국어 발음 기준)."""
    w = re.sub(r"(?:\([^)]*\)|㈜)\s*$", "", word.strip())  # 끝의 (주)/(NEC)/㈜ 제거
    w = w.strip().rstrip("']\"”’」』.")
    if not w:
        return without_batchim
    ch = w[-1]
    if "가" <= ch <= "힣":
        batchim = (ord(ch) - 0xAC00) % 28 != 0
    elif ch.isascii() and ch.isalpha():
        batchim = ch.upper() in _JOSA_LATIN_BATCHIM
    elif ch.isdigit():
        batchim = ch in _JOSA_DIGIT_BATCHIM
    else:
        batchim = False
    return with_batchim if batchim else without_batchim


def is_official_result(event: Dict[str, Any]) -> bool:
    cols = set(event.get("source_columns") or [])
    role = str(event.get("event_role") or "")
    return "official_release_calendar" in cols and role not in ("preview", "review")


def preview_allowed(event: Dict[str, Any]) -> bool:
    category = str((event.get("evidence") or {}).get("release_category") or "")
    return category in PREVIEW_ELIGIBLE


# 규제 제재(공정위/금감원)·입법은 회고(리뷰)가 generic·부자연(must-cover 누락·생성실패 유발)이라
# 리뷰 제외. 입법은 헤드라인+프리뷰로 충분. 리뷰는 가치 있는 US 지표·선거·헌재에만 적용.
REVIEW_EXCLUDE = {"regulatory_action", "legislation"}


def review_allowed(event: Dict[str, Any]) -> bool:
    category = str((event.get("evidence") or {}).get("release_category") or "")
    return category not in REVIEW_EXCLUDE


def build_preview(scheduled_date: str, prev_date: str, official: Dict[str, Any]) -> Dict[str, Any]:
    evi = official.get("evidence") or {}
    category = str(evi.get("release_category") or "")
    label = CATEGORY_LABEL.get(category, "공식 경제지표")
    attr = str(evi.get("required_attribution") or evi.get("institution") or "")
    ref = str(evi.get("reference_period") or "")
    verb = SCHEDULED_VERB.get(category, "발표")
    # 정책·법·선거는 구체 제목(예: '제19대 대통령선거 실시')을 프리뷰에 살리되,
    # 프리뷰 시점엔 결과를 모르므로 제목에서 주문/처리결과(인용·가결 등)를 제거한다.
    subject = str(official.get("angle_label") or "").strip() or f"{ref} {label}".strip()
    subject = strip_preview_outcome(subject, category)
    if category in SCHEDULED_VERB:
        note = f"오는 {scheduled_date}에 '{subject}'{josa(subject, '이', '가')} {verb}될 예정이다."
    else:
        note = f"오는 {scheduled_date}에 {attr}의 {ref} {label} 발표가 예정돼 있다."
    return {
        "event_id": f"{prev_date}_preview_{category or 'release'}",
        "event_role": "preview",
        "source_columns": ["official_release_calendar"],
        "macro_angle": "scheduled_release",
        "angle_label": f"{label} {verb} 예정",
        "direction": "neutral",
        "severity": "info",
        "evidence": {
            "institution": evi.get("institution"),
            "required_attribution": attr,
            "allowed_release_verbs": ["예정돼", "예정이다", "예정"],
            "release_category": category,
            "reference_period": ref,
            "scheduled_available_date_kr": scheduled_date,
            "preview_note": note,
            "source_url": evi.get("source_url"),
        },
    }


def build_review(reviewed_date: str, next_date: str, official: Dict[str, Any]) -> Dict[str, Any]:
    evi = dict(official.get("evidence") or {})
    category = str(evi.get("release_category") or "")
    label = CATEGORY_LABEL.get(category, "공식 경제지표")
    attr = str(evi.get("required_attribution") or evi.get("institution") or "")
    ref = str(evi.get("reference_period") or "")
    verbs = list(evi.get("allowed_release_verbs") or ["발표했다"])
    evi["review_of_date"] = reviewed_date
    evi["allowed_release_verbs"] = verbs
    evi["review_note"] = (
        f"지난 {reviewed_date} {attr}가 발표한 {ref} {label} 결과를 정리한다."
    )
    review = {
        "event_id": f"{next_date}_review_{category or 'release'}",
        "event_role": "review",
        "source_columns": ["official_release_calendar"],
        "macro_angle": "release_review",
        "angle_label": f"{label} 발표 회고",
        "direction": official.get("direction", "neutral"),
        "severity": official.get("severity", "moderate"),
        "evidence": evi,
    }
    if official.get("key_figures"):
        review["key_figures"] = official["key_figures"]
    return review


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--max-preview-per-day", type=int, default=1)
    parser.add_argument("--max-review-per-day", type=int, default=1)
    args = parser.parse_args()

    rows: List[Dict[str, Any]] = []
    with args.input_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    rows.sort(key=lambda r: r.get("date", ""))
    dates = [r.get("date", "") for r in rows]
    pos_by_date = {d: i for i, d in enumerate(dates)}
    extra_preview: Dict[str, List[Dict[str, Any]]] = {}
    extra_review: Dict[str, List[Dict[str, Any]]] = {}

    n_preview = n_review = 0
    for row in rows:
        incorp_date = row.get("date", "")
        pos = pos_by_date[incorp_date]
        for event in row.get("macro_events", []):
            if not is_official_result(event):
                continue
            if pos - 1 >= 0 and preview_allowed(event):  # 규제 제재는 프리뷰 제외(look-ahead)
                prev_date = dates[pos - 1]
                extra_preview.setdefault(prev_date, []).append(
                    build_preview(incorp_date, prev_date, event)
                )
            if pos + 1 < len(dates) and review_allowed(event):  # 규제 제재는 리뷰도 제외
                next_date = dates[pos + 1]
                extra_review.setdefault(next_date, []).append(
                    build_review(incorp_date, next_date, event)
                )

    for row in rows:
        d = row.get("date", "")
        previews = extra_preview.get(d, [])[: args.max_preview_per_day]
        reviews = extra_review.get(d, [])[: args.max_review_per_day]
        if previews or reviews:
            row["macro_events"] = list(row.get("macro_events", [])) + previews + reviews
            n_preview += len(previews)
            n_review += len(reviews)

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"입력 {len(rows)}일 → 출력 {args.output_jsonl}")
    print(f"추가된 프리뷰 이벤트: {n_preview}건, 리뷰 이벤트: {n_review}건")


if __name__ == "__main__":
    main()
