#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""연간 실적 기사 빌더 (DART 재무제표 API 결과 → 게임용 연간 실적 뉴스).

입력: fetch_annual_financials.py 산출 annual_financials_api.csv
  (stock, business_year, rcept_date, fs_div, sales/operating_profit/net_income; 단위 원)

날짜 정합성: publish_date = 사업보고서 공시일(rcept_date). 회계연도말이 아님.
  추정일(date_basis=estimated, 인덱스에 사업보고서 없던 경우)이 주말이면 다음 영업일로 보정.

사용법:
  python scripts/processors/build_annual_earnings_news.py \
    --in data/interim/pr05f_dart_annual_financial_facts_v2_all/annual_financials_api.csv \
    --out data/interim/annual_news/annual_earnings_news.jsonl \
    --md-out data/interim/annual_news/annual_earnings_news.md
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_split_article_prototype import has_jongseong  # noqa: E402


def topic(name: str) -> str:
    return f"{name}{'은' if has_jongseong(name) else '는'}"


def fmt_won(won: int) -> str:
    """원 단위 정수 → '○조○,○○○억원' (억 단위 절사)."""
    eok_total = abs(won) // 10**8
    jo, eok = divmod(eok_total, 10000)
    if jo and eok:
        return f"약 {jo}조{eok:,}억원"
    if jo:
        return f"약 {jo}조원"
    return f"약 {eok:,}억원"


def metric_phrase(pos_label: str, neg_label: str, raw: str):
    if raw in ("", None):
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    if abs(v) < 10**8:  # 1억 미만은 '약 0억원'으로 무의미 → 항목 생략
        return None
    label = neg_label if v < 0 else pos_label
    return f"{label} {fmt_won(v)}"


def next_business_day(iso: str) -> str:
    d = datetime.date.fromisoformat(iso)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d.isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--md-out", type=Path)
    args = ap.parse_args()

    rows_out = []
    skipped = 0
    for r in csv.DictReader(args.inp.open(encoding="utf-8-sig")):
        name = r["stock_name"]
        if not name:
            skipped += 1
            continue
        # 매출이 1억 미만으로 반올림되면(별도 재무의 사실상 무매출 등) 무의미 → 스킵
        try:
            sales_won = int(r["sales"])
        except (ValueError, TypeError):
            sales_won = None
        if sales_won is None or abs(sales_won) < 10**8:
            skipped += 1
            continue
        sales = metric_phrase("매출액", "매출액", r["sales"])  # 매출은 음수 라벨 없음
        op = metric_phrase("영업이익", "영업손실", r["operating_profit"])
        ni = metric_phrase("당기순이익", "당기순손실", r["net_income"])
        parts = [sales] + [p for p in (op, ni) if p]
        scope = f"{r['fs_div']} 기준 " if r.get("fs_div") else ""
        line = f"{topic(name)} {r['business_year']}년 {scope}{', '.join(parts)}을 기록했다."

        # 날짜 정합성: 사업보고서 공시일은 회계연도+1(통상) 또는 +2(정정). 그 밖이면
        # 인덱스 오매칭(예: 펄어비스 2017 사업보고서가 2017-09로 매칭) → 추정일로 대체.
        r_year = int(r["business_year"])
        pub = r["rcept_date"]
        basis = r.get("date_basis", "filing")
        pub_year = int(pub[:4]) if pub[:4].isdigit() else 0
        if pub_year not in (r_year + 1, r_year + 2):
            pub = f"{r_year + 1}-03-31"
            basis = "estimated"
        if basis == "estimated":
            pub = next_business_day(pub)
        rows_out.append({
            "news_id": f"annual__{r['stock_code']}__{r['business_year']}",
            "category": "annual_earnings",
            "stock_code": r["stock_code"], "stock_name": name,
            "business_year": int(r["business_year"]),
            "publish_date": pub, "date_basis": basis,
            "fs_div": r.get("fs_div", ""),
            "news_lines": [line],
        })

    rows_out.sort(key=lambda x: (x["publish_date"], x["stock_code"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.md_out:
        from collections import Counter
        by_year = Counter(r["business_year"] for r in rows_out)
        est = sum(1 for r in rows_out if r["date_basis"] == "estimated")
        lines = ["# 연간 실적 뉴스 (샘플 30)", "",
                 f"- 총 {len(rows_out)}건 / 종목 {len({r['stock_code'] for r in rows_out})}개 / 추정일 {est}건",
                 f"- 연도분포: {dict(sorted(by_year.items()))}", "", ""]
        for row in rows_out[:30]:
            lines.append(f"- `{row['publish_date']}` {row['news_lines'][0]}")
        args.md_out.write_text("\n".join(lines), encoding="utf-8")

    print(f"[done] annual_news={len(rows_out)} skipped={skipped}")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
