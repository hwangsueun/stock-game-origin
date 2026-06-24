#!/usr/bin/env python3
"""Classify DART correction reasons and audit correction-article wording.

The input is the detail-facts CSV produced by
pr05f_extract_dart_disclosure_detail_facts.py. Outputs are always written to a
separate directory; this script never modifies the source CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ACTION_CLASSES = (
    "cancellation_withdrawal",
    "completion_termination",
    "substantive_change",
    "disclosure_unhold",
    "finalization",
    "audit_adjustment",
    "typo_admin",
    "generic",
)

CORRECTION_MARKERS = re.compile(
    r"정정|변경|확정|취소|철회|해제|종료|종결|완료|공개|유보|감사결과|감사 과정"
)
NEW_EVENT_VERBS = re.compile(
    r"(?:계약을\s*)?체결(?:했다|했다고|한다)|"
    r"(?:배당|투자|취득|처분|시설투자|출자)을?\s*(?:결정|실시)(?:했다|했다고|한다)|"
    r"(?:실적을\s*)?(?:발표|기록)(?:했다|했다고|한다)|"
    r"(?:주식|자산)을?\s*(?:취득|처분)(?:했다|했다고|한다)"
)
STRONG_NEW_EVENT = re.compile(r"신규|새로|처음으로")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def unwrap_reason(text: str) -> str:
    """Remove the extractor's Korean wrapper without depending on its particle."""
    value = clean(text)
    match = re.match(r"^정정 사유는 ['\"](.+?)['\"](?:이?라고|으로|로) 공시됐다\.$", value)
    return clean(match.group(1)) if match else value


def event_family(report_name: str) -> str:
    title = clean(report_name).replace("ㆍ", "").replace("·", "")
    if "매출액또는손익구조" in title:
        return "earnings"
    if "현금현물배당결정" in title:
        return "dividend"
    if "단일판매공급계약체결" in title:
        return "contract"
    if "신규시설투자등" in title:
        return "investment"
    if "타법인주식및출자증권" in title or "유형자산" in title:
        return "asset"
    return "other"


def classify_reason(reason: str) -> str:
    """Map a raw reason to one mutually exclusive article-safe action class."""
    value = clean(reason)
    compact = value.replace(" ", "")

    # This must precede cancellation: "비밀유지 약정 해제" is disclosure,
    # not cancellation of the underlying commercial contract.
    if re.search(r"공시유보|유보기한|유보사유|비밀유지|정보공개|사항공개", compact):
        return "disclosure_unhold"

    if re.search(
        r"결정취소|계약(?:의)?해제|계약해지|처분철회|취득철회|매수철회|"
        r"거래무산|투자철회|사업(?:진행)?중단|결정철회|증권신고서철회",
        compact,
    ):
        return "cancellation_withdrawal"

    # End-date changes are substantive changes, not completed contracts.
    if re.search(r"종료일변경|종료일정정|기간종료일변경|종결기한.*연장", compact):
        return "substantive_change"

    if re.search(
        r"이행[률율].*계약종료|계약(?:기간)?종료$|계약종결|거래종결|"
        r"취득완료|처분완료|투자완료|절차완료|계약종료에따른",
        compact,
    ):
        return "completion_termination"

    if re.search(
        r"외부감사|감사결과|감사과정|감사보고서|재무제표|결산조정|"
        r"회계감사|법인세세무조정|손상평가|충당금반영|계정재분류",
        compact,
    ):
        return "audit_adjustment"

    if re.search(r"오기|오표기|기재오류|단위착오|단위수정|사외이사정정", compact):
        return "typo_admin"

    if "확정" in compact or re.search(r"일자기재|종료일기재|주주총회일자기재", compact):
        return "finalization"

    if re.search(
        r"변경|증가|감소|증액|감액|연장|단축|조정|정정|수정|추가|"
        r"재작성|재측정|승계|양도|공개매수결과|조건부승인",
        compact,
    ):
        return "substantive_change"

    return "generic"


def safe_action(action_class: str, family: str) -> str:
    label = {
        "earnings": "실적",
        "dividend": "배당",
        "contract": "계약",
        "investment": "시설투자",
        "asset": "주식·자산 거래",
    }.get(family, "해당 사항")
    templates = {
        "cancellation_withdrawal": f"기존 {label}의 취소·철회 또는 해제 사실을 정정 공시했다.",
        "completion_termination": f"기존 {label}의 완료·종료 또는 거래 종결 내용을 정정 공시했다.",
        "substantive_change": f"기존 {label}의 주요 조건 변경 내용을 정정 공시했다.",
        "disclosure_unhold": f"기존 {label} 공시에서 유보했던 정보를 공개했다.",
        "finalization": f"기존 {label}의 미확정 항목을 확정해 정정 공시했다.",
        "audit_adjustment": f"감사·결산 과정에서 바뀐 {label} 수치를 정정 공시했다.",
        "typo_admin": f"기존 {label} 공시의 오기나 기재 오류를 정정했다.",
        "generic": f"기존 {label} 공시의 일부 내용을 정정했다.",
    }
    return templates[action_class]


def is_truncated(reason: str) -> bool:
    # The extractor marks capped strings with an ellipsis. Korean nouns such as
    # "승인" legitimately end in these syllables, so suffix guessing is unsafe.
    return "…" in reason or "..." in reason


def audit_correction_sentence(text: str) -> list[str]:
    """Return blocking reasons for correction copy written as a new event."""
    value = clean(text)
    failures: list[str] = []
    if not value:
        return ["empty_article_text"]
    has_new_event = bool(NEW_EVENT_VERBS.search(value))
    has_correction_marker = bool(CORRECTION_MARKERS.search(value))
    if has_new_event and not has_correction_marker:
        failures.append("new_event_wording_without_correction_marker")
    if has_new_event and STRONG_NEW_EVENT.search(value):
        failures.append("explicit_new_event_wording")
    return failures


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def correction_records(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        try:
            facts = json.loads(row.get("facts_json") or "[]")
        except json.JSONDecodeError:
            continue
        fact_types = {clean(f.get("fact_type")) for f in facts}
        if "is_correction" not in fact_types:
            continue
        reasons = [
            unwrap_reason(f.get("source_text_ko") or f.get("text_ko", ""))
            for f in facts
            if f.get("fact_type") == "correction_reason"
        ]
        reason = reasons[0] if reasons else ""
        family = event_family(row.get("report_name", ""))
        action_class = classify_reason(reason) if reason else "generic"
        issues: list[str] = []
        if not reason:
            issues.append("missing_correction_reason")
        if is_truncated(reason):
            issues.append("truncated_correction_reason")
        if action_class == "generic":
            issues.append("generic_correction_reason")
        action_text = safe_action(action_class, family)
        action_issues = audit_correction_sentence(action_text)
        issues.extend(f"safe_action:{item}" for item in action_issues)
        output.append(
            {
                "rcept_no": clean(row.get("rcept_no")),
                "stock_code": clean(row.get("stock_code")).zfill(6),
                "stock_name": clean(row.get("stock_name")),
                "report_name": clean(row.get("report_name")),
                "event_family": family,
                "correction_reason": reason,
                "action_class": action_class,
                "safe_action_ko": action_text,
                "safe_action_new_event_check": "FAIL" if action_issues else "PASS",
                "audit_status": "FAIL" if any(x.startswith(("missing", "truncated", "safe_action:")) for x in issues) else ("WARN" if issues else "PASS"),
                "audit_issues": "|".join(issues),
            }
        )
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["rcept_no"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, source: Path, rows: list[dict[str, Any]]) -> None:
    classes = Counter(row["action_class"] for row in rows)
    statuses = Counter(row["audit_status"] for row in rows)
    wording_failures = sum(row["safe_action_new_event_check"] == "FAIL" for row in rows)
    families = Counter(row["event_family"] for row in rows)
    lines = [
        "# Correction Article Safety Audit",
        "",
        f"- Source: `{source}`",
        f"- Correction rows: {len(rows):,}",
        f"- PASS: {statuses['PASS']:,}",
        f"- WARN: {statuses['WARN']:,}",
        f"- FAIL: {statuses['FAIL']:,}",
        f"- Safe-action new-event wording failures: {wording_failures:,}",
        "",
        "## Action Classes",
        "",
    ]
    lines.extend(f"- {name}: {classes[name]:,}" for name in ACTION_CLASSES)
    lines.extend(["", "## Event Families", ""])
    lines.extend(f"- {name}: {count:,}" for name, count in families.most_common())
    lines.extend(["", "## Blocking Rule", "", "Correction articles fail when a new-event verb is used without a correction/change/finalization marker. Explicit `신규` or `새로` wording with a new-event verb also fails.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def self_test() -> None:
    cases = {
        "비밀유지 약정 해제에 따른 계약금액 공개": "disclosure_unhold",
        "주식매매계약 해제에 따른 취득 결정 취소": "cancellation_withdrawal",
        "이행률 84.75%로 계약 종료": "completion_termination",
        "계약 종료일 변경": "substantive_change",
        "외부감사 과정 중 재무제표 변경": "audit_adjustment",
        "취득예정일자 확정": "finalization",
        "단순 오기 정정": "typo_admin",
        "기재사항 반영": "generic",
    }
    for reason, expected in cases.items():
        actual = classify_reason(reason)
        assert actual == expected, (reason, expected, actual)
    assert audit_correction_sentence("A사는 B사와 공급계약을 체결했다.")
    assert not audit_correction_sentence("A사는 기존 공급계약의 금액 변경을 정정 공시했다.")
    assert "explicit_new_event_wording" in audit_correction_sentence("A사는 신규 공급계약을 체결했다고 정정 공시했다.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="DART detail-facts CSV")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
    records = correction_records(load_rows(args.input))
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty output directory: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "correction_action_classes.csv", records)
    write_csv(args.output_dir / "audit_failed_or_warned.csv", [r for r in records if r["audit_status"] != "PASS"])
    write_report(args.output_dir / "REPORT.md", args.input, records)
    print(json.dumps({"correction_rows": len(records), "classes": Counter(r["action_class"] for r in records), "statuses": Counter(r["audit_status"] for r in records)}, ensure_ascii=False, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
