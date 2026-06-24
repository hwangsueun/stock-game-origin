#!/usr/bin/env python3
"""DART '매출액또는손익구조변동' 공시 상세 팩트의 ×1000 단위오류를 사후 보정한다.

배경
----
`pr05f_extract_dart_disclosure_detail_facts.py`의 단위 감지는 두 가지 한계가 있다.
  1) 단위 표기가 변동내용 헤더에서 멀리(>60자) 떨어진 공시는 천원 기본값으로 떨어졌다.
     (코드 수정으로 변동내용~대규모법인여부 섹션 전체를 보도록 확대함.)
  2) 일부 공시는 헤더가 '(단위:천원)'으로 **오기재**돼 있으나 값은 원 단위다
     (예: 한솔케미칼 20160205800457 → 매출 368,369,943,614원을 천원으로 읽어 368조).
헤더만으로는 (2)를 잡을 수 없으므로, 헤더와 무관하게 **종목 내 일관성**으로 보정한다.

판별 (오탐 방지)
----------------
순수 상대크기(중앙값 대비 ×N)는 일회성 급등(씨젠 COVID, BGF 분할, 우리기술투자 평가이익)을
단위오류로 오인한다. 진짜 ×1000 오류는 ÷1000하면 같은 종목 다른 연도 값(중앙값) 근처로
돌아오지만, 일회성 급등은 ÷1000하면 중앙값보다 한참 아래로 떨어진다. 따라서 다음을 모두
만족할 때만 ÷1000 보정한다.
  - 원본 값 ≥ ABS_RATIO × (종목·팩트유형별 중앙값)
  - ÷1000 값이 중앙값의 [LOW_BAND, HIGH_BAND] 안
  - 해당 (종목, 팩트유형) 그룹의 공시가 2건 이상 (중앙값이 의미 있어야 함)

사용
----
  python scripts/processors/correct_disclosure_unit_outliers.py \
    --input  data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv \
    --output data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts_unitfix2.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

SALES_CHANGE_KEYWORD = "매출액또는손익구조"
FIN_FACT_TYPES = {"sales", "operating_profit", "net_income"}
ABS_RATIO = 50.0   # 원본이 중앙값의 50배 이상일 때만 후보
LOW_BAND = 0.2     # ÷1000 값이 중앙값의 0.2~5배 안에 들어와야 진짜 단위오류
HIGH_BAND = 5.0

_AMOUNT_RE = re.compile(r"약\s*[\d,]+(?:조[\d,]*)?(?:억)?[\d,]*원")


def parse_eok(text: str) -> float | None:
    """'약 682조8,763억원' / '약 2,545억원' → 억 단위 float."""
    jo = re.search(r"(\d[\d,]*)\s*조", text)
    eok = re.search(r"(\d[\d,]*)\s*억", text)
    if not (jo or eok):
        return None
    value = 0.0
    if jo:
        value += int(jo.group(1).replace(",", "")) * 10000
    if eok:
        value += int(eok.group(1).replace(",", ""))
    return value or None


def format_eok(eok: float) -> str:
    """억 단위 수치를 '약 N억원' / '약 X조Y억원' 형태로(추출기 _format_krw_amount와 동일 스타일)."""
    eok_int = int(round(eok))
    if eok_int <= 0:
        return "약 0억원"
    jo, rem = divmod(eok_int, 10000)
    if jo > 0 and rem > 0:
        return f"약 {jo}조{rem:,}억원"
    if jo > 0:
        return f"약 {jo}조원"
    return f"약 {rem:,}억원"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true", help="보정 내역만 출력, 파일 미생성")
    args = ap.parse_args()

    with args.input.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    # 1패스: (종목, 팩트유형)별 값 수집 → 중앙값
    values: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if SALES_CHANGE_KEYWORD not in row["report_name"] or not row["facts_json"]:
            continue
        for fact in json.loads(row["facts_json"]):
            if not isinstance(fact, dict) or fact.get("fact_type") not in FIN_FACT_TYPES:
                continue
            eok = parse_eok(fact.get("text_ko", ""))
            if eok and eok > 0:
                values[(row["stock_code"], fact["fact_type"])].append(eok)
    medians = {k: statistics.median(v) for k, v in values.items() if len(v) >= 2}

    # 2패스: 이중조건 충족 시 ÷1000 보정
    corrections = []
    for row in rows:
        if SALES_CHANGE_KEYWORD not in row["report_name"] or not row["facts_json"]:
            continue
        facts = json.loads(row["facts_json"])
        changed = False
        for fact in facts:
            if not isinstance(fact, dict) or fact.get("fact_type") not in FIN_FACT_TYPES:
                continue
            eok = parse_eok(fact.get("text_ko", ""))
            if not eok:
                continue
            med = medians.get((row["stock_code"], fact["fact_type"]))
            if not med:
                continue
            corrected = eok / 1000.0
            if eok >= ABS_RATIO * med and LOW_BAND * med <= corrected <= HIGH_BAND * med:
                old_text = fact["text_ko"]
                new_amount = format_eok(corrected)
                new_text = _AMOUNT_RE.sub(new_amount, old_text, count=1)
                if new_text == old_text:  # 안전장치: 금액 패턴 못 찾으면 건너뜀
                    continue
                fact["text_ko"] = new_text
                if fact.get("source_text_ko"):
                    fact["source_text_ko"] = _AMOUNT_RE.sub(new_amount, fact["source_text_ko"], count=1)
                changed = True
                corrections.append((row["stock_code"], row["stock_name"], row["rcept_no"],
                                    fact["fact_type"], old_text, new_text))
        if changed:
            row["facts_json"] = json.dumps(facts, ensure_ascii=False)

    print(f"보정된 팩트: {len(corrections)}건, 공시: {len({c[2] for c in corrections})}건, "
          f"종목: {len({c[0] for c in corrections})}개")
    for code, name, rcept, ft, old, new in corrections:
        print(f"  {code} {name} {ft} {rcept}")
        print(f"    - {old}")
        print(f"    + {new}")

    if args.dry_run:
        print("\n[dry-run] 파일 미생성")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n저장: {args.output}")


if __name__ == "__main__":
    main()
