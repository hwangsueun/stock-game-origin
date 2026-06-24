#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""시장·매크로/섹터 뉴스 빌더 (이미 가공된 이벤트 후보 → 게임용 시장 뉴스).

소스: market_indicator/data/processed/{macro,sector}_event_candidates_daily.csv
  (이미 한국어 event_frame 서술이 적혀 있음 — LLM 불필요, 결정적)

날짜 정합성(중요):
- 모든 이벤트의 `date`는 그 사건이 공개된 거래일이다(금리·환율·지수·유가 종가, 섹터 1일 순위).
- 섹터 5일 추세는 '최근 5거래일'(backward)이라 `date`에 이미 공개됨 → look-ahead 없음.
- **경제지표 update(CPI·수출·생산·선행지수 등)는 제외**: 참조월 1일로 찍혀 있어 일부는
  실제 발표일보다 이르고(=look-ahead) 숫자도 원자료라 부정확.

품질:
- frame의 추측성 꼬리("…영향을 줌"/"…흐름을 반영")는 제거하고 사실 문장만 남긴다.
- 불가능한 일변동(시계열 단절, |1일 변화율|>30%)은 데이터 오류로 제외.

사용법:
  cd "/Users/hgs/Desktop/IISE-CD/data-pipeline"
  python news_generator/scripts/processors/build_market_news.py \
    --macro-csv market_indicator/data/processed/macro_event_candidates_daily.csv \
    --sector-csv market_indicator/data/processed/sector_event_candidates_daily.csv \
    --out news_generator/data/interim/market_news/market_news.jsonl \
    --md-out news_generator/data/interim/market_news/market_news.md \
    --min-strength 4
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_split_article_prototype import has_jongseong  # noqa: E402

# 라틴 지수명의 이/가 조사(음독 받침). 그 외는 has_jongseong로 판정.
_IGA_OVERRIDE = {"KOSDAQ": "이", "NASDAQ": "이", "KOSPI": "가", "나스닥": "이", "S&P500": "가"}


def iga(name: str) -> str:
    if name in _IGA_OVERRIDE:
        return _IGA_OVERRIDE[name]
    return "이" if has_jongseong(name) else "가"


def is_weekend(date_str: str) -> bool:
    """주말(토/일)이면 비거래일 → 시장 뉴스 날짜로 부적합.
    일부 글로벌 시계열(미국 스프레드·Dubai 유가)이 주말에도 값을 채워 생기는 아티팩트 차단."""
    try:
        return datetime.date.fromisoformat(date_str).weekday() >= 5
    except ValueError:
        return False

# 참조월 1일로 찍혀 실제 발표일과 어긋나는 경제지표 업데이트는 제외(look-ahead 방지).
EXCLUDE_MACRO = {
    "inflation_update", "trade_update", "trade_balance_update", "real_activity_update",
    "consumption_update", "investment_update", "leading_index_update",
}
KEEP_SECTOR = {"sector_leader", "sector_laggard"}

MAX_PCT_DAILY = 15.0   # |1일 변화율| 초과 시 데이터 단절로 간주(특히 Dubai 유가 ~30% 가짜점프 차단)
MAX_BP = 200.0         # 1일 bp 변화 상한(데이터 단절 가드)


def num(text: str, unit: str):
    """evidence/frame 문자열에서 부호 포함 수치를 unit과 함께 추출."""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*" + re.escape(unit), text or "")
    return float(m.group(1)) if m else None


def first_num(*texts_units):
    for text, unit in texts_units:
        v = num(text, unit)
        if v is not None:
            return v
    return None


def price_dir(v):  # 가격/지수/유가
    return "올랐다" if v > 0 else "내렸다" if v < 0 else "보합을 기록했다"


def level_dir(v):  # 금리/스프레드 수준
    return "상승했다" if v > 0 else "하락했다" if v < 0 else "보합했다"


def macro_line(row):
    """매크로 이벤트 → 사실 문장. 부적합/데이터오류면 None."""
    et = row["event_type"]
    asset = row["asset_id"]
    e1, e2, e3 = row.get("evidence_1", ""), row.get("evidence_2", ""), row.get("evidence_3", "")
    frame = row["event_frame"]

    if et in ("fx_move", "oil_move", "safe_asset_move", "market_close_move"):
        chg = first_num((e1, "%"), (frame, "%"))
        if chg is None or abs(chg) > MAX_PCT_DAILY:
            return None  # 데이터 단절 가드
        basis = "종가 기준" if et == "market_close_move" else "전일 대비"
        return f"{asset}{iga(asset)} {basis} {abs(chg):g}% {price_dir(chg)}."

    if et == "rate_move":
        bp = first_num((e2, "bp"), (frame, "bp"))
        if bp is None or abs(bp) > MAX_BP:
            return None
        level = num(e1, "%")
        verb = "상승" if bp > 0 else "하락" if bp < 0 else "보합"
        if bp == 0:
            return f"{asset}{iga(asset)} 전일 대비 보합했다."
        tail = f"해 {level:g}%를 기록했다." if level is not None else "했다."
        return f"{asset}{iga(asset)} 전일 대비 {abs(bp):g}bp {verb}{tail}"

    if et == "spread_move":
        win = num(frame, "거래일")
        bp = first_num((e2, "bp"), (frame, "bp"))
        if bp is None:
            return None
        verb = "확대됐다" if bp > 0 else "축소됐다" if bp < 0 else "보합했다"
        window = f"최근 {int(win)}거래일 기준 " if win else ""
        return f"{asset}{iga(asset)} {window}{abs(bp):g}bp {verb}."
    return None


def sector_line(row):
    et = row["event_type"]
    market, sector = row["market"], row["sector"]
    excess = num(row.get("evidence_3", ""), "%p")
    if et == "sector_leader":
        body = f"{market} {sector} 업종이 시장 내 수익률 상위권을 기록했다"
        if excess is not None:
            body += f" (시장 대비 +{abs(excess):g}%p)"
        return body + "."
    if et == "sector_laggard":
        body = f"{market} {sector} 업종이 시장 내 수익률 하위권을 기록했다"
        if excess is not None:
            body += f" (시장 대비 -{abs(excess):g}%p)"
        return body + "."
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--macro-csv", type=Path, required=True)
    ap.add_argument("--sector-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--md-out", type=Path)
    ap.add_argument("--min-strength", type=int, default=4)
    args = ap.parse_args()

    stats = Counter()
    rows_out = []
    seen = set()  # (date, kind_key) dedup

    # 매크로
    for r in csv.DictReader(args.macro_csv.open(encoding="utf-8-sig")):
        if r["event_type"] in EXCLUDE_MACRO:
            stats["skip_macro_update"] += 1
            continue
        if is_weekend(r["date"]):
            stats["skip_weekend"] += 1
            continue
        try:
            strength = int(r.get("strength") or 0)
        except ValueError:
            strength = 0
        if strength < args.min_strength:
            stats["skip_low_strength"] += 1
            continue
        key = (r["date"], "macro", r["asset_id"])
        if key in seen:
            stats["skip_dup"] += 1
            continue
        line = macro_line(r)
        if not line:
            stats["skip_macro_unbuildable"] += 1
            continue
        seen.add(key)
        rows_out.append({
            "news_id": f"market__{r['date']}__macro__{len(rows_out)}",
            "category": "market_macro", "publish_date": r["date"],
            "asset_id": r["asset_id"], "event_type": r["event_type"],
            "direction": r["direction"], "strength": strength,
            "news_lines": [line],
        })
        stats["macro_built"] += 1

    # 섹터 (leader/laggard)
    for r in csv.DictReader(args.sector_csv.open(encoding="utf-8-sig")):
        if r["event_type"] not in KEEP_SECTOR:
            stats["skip_sector_type"] += 1
            continue
        if is_weekend(r["date"]):
            stats["skip_weekend"] += 1
            continue
        try:
            strength = int(r.get("strength") or 0)
        except ValueError:
            strength = 0
        if strength < args.min_strength:
            stats["skip_low_strength"] += 1
            continue
        key = (r["date"], "sector", r["market"], r["event_type"], r["sector"])
        if key in seen:
            stats["skip_dup"] += 1
            continue
        line = sector_line(r)
        if not line:
            stats["skip_sector_unbuildable"] += 1
            continue
        seen.add(key)
        rows_out.append({
            "news_id": f"market__{r['date']}__sector__{len(rows_out)}",
            "category": "market_sector", "publish_date": r["date"],
            "market": r["market"], "sector": r["sector"], "event_type": r["event_type"],
            "direction": r["direction"], "strength": strength,
            "news_lines": [line],
        })
        stats["sector_built"] += 1

    rows_out.sort(key=lambda x: (x["publish_date"], x["category"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.md_out:
        lines = ["# 시장·섹터 뉴스 (샘플 40)", "", f"- 총 {len(rows_out)}건", "",
                 "## 집계", "```json", json.dumps(dict(stats), ensure_ascii=False, indent=2), "```", ""]
        for row in rows_out[:40]:
            lines.append(f"- `{row['publish_date']}` [{row['category']}] {row['news_lines'][0]}")
        args.md_out.write_text("\n".join(lines), encoding="utf-8")

    print(f"[done] market_news={len(rows_out)} (macro={stats['macro_built']} sector={stats['sector_built']})")
    print("[stats]", dict(stats))
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
