#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2012–2014 연간 실적 뉴스 보강 (DART 구조화 API는 2015~만 제공).

소스: 디스클로저 detail CSV의 '매출액또는손익구조변동' 공시(이미 추출·단위정정).
  - 1~4월 공시만 사용(연간 확정 실적; 회계연도=공시연도-1).
  - 회계연도 2012–2014만 산출.
  - 같은 (종목, 회계연도)는 최신 공시(확정)만.
날짜: publish_date = 공시 접수일(rcept_no 앞 8자리). 회계연도말 아님.

annual_earnings_news.jsonl(2015~, API)과 동일 포맷 → 병합 가능.

사용법:
  python scripts/processors/build_annual_earnings_2012_2014.py \
    --detail-csv data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv \
    --out data/interim/annual_news/annual_earnings_2012_2014.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_split_article_prototype import amount, fact_value, has_jongseong  # noqa: E402

YEARS = {"2012", "2013", "2014"}


def topic(name: str) -> str:
    return f"{name}{'은' if has_jongseong(name) else '는'}"


def earnings_line(name: str, fiscal_year: str, facts: list) -> str | None:
    vals = []
    for ft, default in [("sales", "매출액"), ("operating_profit", "영업이익"), ("net_income", "당기순이익")]:
        src = fact_value(facts, ft)
        amt = amount(src)
        if amt:
            label = next((x for x in ["영업손실", "당기순손실", "영업이익", "당기순이익", "매출액"] if x in src), default)
            vals.append(f"{label} {amt}")
    if not vals or "매출액" not in vals[0]:
        return None
    return f"{topic(name)} {fiscal_year}년 {', '.join(vals)}을 기록했다."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    df = pd.read_csv(args.detail_csv, dtype=str).fillna("")
    earn = df[df["report_name"].str.contains("매출액|손익구조")]

    # (stock, fiscal_year) -> (rcept_no, row) ; 최신 공시 유지
    best = {}
    for _, r in earn.iterrows():
        rc = r["rcept_no"]
        if len(rc) < 8 or not rc[:8].isdigit():
            continue
        filing_year, month = rc[:4], rc[4:6]
        if month > "04":  # 1~4월 공시만(연간 확정)
            continue
        fiscal_year = str(int(filing_year) - 1)
        if fiscal_year not in YEARS:
            continue
        key = (r["stock_code"], fiscal_year)
        if key not in best or rc > best[key][0]:
            best[key] = (rc, r)

    rows_out = []
    for (sc, fy), (rc, r) in best.items():
        try:
            facts = json.loads(r["facts_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        line = earnings_line(r["stock_name"], fy, facts)
        if not line:
            continue
        pub = f"{rc[:4]}-{rc[4:6]}-{rc[6:8]}"
        rows_out.append({
            "news_id": f"annual__{sc}__{fy}",
            "category": "annual_earnings",
            "stock_code": sc, "stock_name": r["stock_name"],
            "business_year": int(fy),
            "publish_date": pub, "date_basis": "disclosure",
            "fs_div": "",
            "news_lines": [line],
        })

    rows_out.sort(key=lambda x: (x["publish_date"], x["stock_code"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    from collections import Counter
    print(f"[done] 2012-2014 annual_news={len(rows_out)}")
    print("  연도:", dict(sorted(Counter(r["business_year"] for r in rows_out).items())))
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
