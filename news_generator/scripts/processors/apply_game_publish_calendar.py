#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""게임 발행일 거래일 정렬 가드 (파이프라인 출력 단계).

게임 달력 규칙(사용자 확정):
- 게임은 평일(월~금) 모두 플레이/뉴스 열람 가능. 즉 **주말 외 휴장일(연말 폐장·임시공휴일)에도 게임이 열린다.**
- 따라서 발행일 정렬은 **주말만** 보정한다:
    토요일 → +2일(월요일), 일요일 → +1일(월요일), 월~금(휴장일 포함) → 그대로 유지.
- 휴장일을 유지하는 이유: 게임이 그날 열리므로 다음 거래일로 미루면 안 됨.

이 스크립트가 하는 일:
1) 4개 뉴스 카테고리 출력에 `game_publish_date`를 부여(원본 `publish_date`는 보존).
2) 개별 주식 뉴스는 출력 파일에 날짜가 없으므로 candidate_pool의 `anchor_date`를 조인해 날짜를 붙이고,
   LLM 응답에서 news_lines를 파싱해 게임용 레코드로 만든다(status=rejected 제외).
3) 보정 리포트(.md)와 요약(.json)을 남기고, **모든 출력 game_publish_date가 주말이 아님**을 자체검증한다.

결정적·무비용(LLM/네트워크 없음). 재생성 시에도 동일 결과를 보장하는 가드 역할.

사용법:
  cd "/Users/hgs/Desktop/IISE-CD/data-pipeline"
  python news_generator/scripts/processors/apply_game_publish_calendar.py
  # 경로는 스크립트 위치 기준 자동 해석 — 인자 없이 동작.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
from collections import Counter
from pathlib import Path

# news_generator 루트 = .../news_generator/scripts/processors/<this> 의 parents[2]
NG_ROOT = Path(__file__).resolve().parents[2]
PIPE_ROOT = NG_ROOT.parent  # data-pipeline

DEF_STOCK_OUTPUTS = NG_ROOT / "data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl"
DEF_STOCK_POOL = NG_ROOT / "data/interim/pr06a_full_requests_v4_2_all_stocks/stock_news_sample_candidate_pool.csv"
DEF_MARKET = NG_ROOT / "data/interim/market_news/market_news.jsonl"
DEF_ANNUAL = NG_ROOT / "data/interim/annual_news/annual_earnings_news.jsonl"
DEF_SPLIT = NG_ROOT / "data/interim/context_layers/split_articles_all/split_articles_all.jsonl"
DEF_OUT_DIR = NG_ROOT / "data/interim/game_publish_calendar"

# 거래일 달력(휴장일 식별용 — 리포트 정보 제공에만 사용, 보정 로직엔 불필요)
DEF_KOSPI = PIPE_ROOT / "market_indicator/data/raw/kospi_20130101_20231231.csv"
DEF_KOSDAQ = PIPE_ROOT / "market_indicator/data/raw/kosdaq_20130101_20231231.csv"

WD_KO = ["월", "화", "수", "목", "금", "토", "일"]


def parse_date(s: str) -> datetime.date | None:
    s = (s or "").strip()[:10]
    if len(s) != 10:
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def shift_weekend_to_monday(d: datetime.date) -> datetime.date:
    """토 → +2(월), 일 → +1(월), 그 외(월~금, 휴장일 포함) → 그대로."""
    wd = d.weekday()
    if wd == 5:  # 토
        return d + datetime.timedelta(days=2)
    if wd == 6:  # 일
        return d + datetime.timedelta(days=1)
    return d


def load_trading_days(*paths: Path) -> set[str]:
    days: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                d = (row.get("date") or "").strip()[:10]
                if d:
                    days.add(d)
    return days


def iter_stock_news(outputs: Path, pool: Path):
    """개별 주식 뉴스: outputs_all.jsonl + candidate_pool(anchor_date) 조인.
    accepted만, news_lines 파싱. yield dict(record)."""
    b2meta: dict[str, dict] = {}
    with pool.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            b2meta[row["bundle_id"]] = {
                "stock_code": row.get("stock_code", ""),
                "stock_name": row.get("stock_name", ""),
                "anchor_date": row.get("anchor_date", ""),
                "event_family": row.get("event_family", ""),
                "news_type": row.get("news_type", ""),
            }
    skipped_rejected = 0
    skipped_nodate = 0
    with outputs.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cid = d.get("custom_id", "")
            if not cid.startswith("stock_news__"):
                continue
            bundle_id = cid.replace("stock_news__", "")
            try:
                content = d["response"]["body"]["choices"][0]["message"]["content"]
                obj = json.loads(content)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                continue
            if obj.get("status") != "accepted":
                skipped_rejected += 1
                continue
            meta = b2meta.get(bundle_id, {})
            pub = meta.get("anchor_date", "")
            if not parse_date(pub):
                skipped_nodate += 1
                continue
            yield {
                "news_id": obj.get("news_id", ""),
                "category": "stock_disclosure",
                "bundle_id": bundle_id,
                "stock_code": meta.get("stock_code", ""),
                "stock_name": meta.get("stock_name", ""),
                "event_family": meta.get("event_family", ""),
                "news_type": obj.get("news_type") or meta.get("news_type", ""),
                "claim_level": obj.get("claim_level", ""),
                "publish_date": pub[:10],
                "news_lines": obj.get("news_lines", []),
            }
    iter_stock_news.skipped_rejected = skipped_rejected  # type: ignore[attr-defined]
    iter_stock_news.skipped_nodate = skipped_nodate  # type: ignore[attr-defined]


def iter_jsonl(path: Path, category_default: str):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d.setdefault("category", category_default)
            yield d


def process(records, date_field: str, trading_days: set[str], examples_cap: int = 10):
    """records에 game_publish_date 부여. 통계/예시 수집. 반환: (out_records, stats)."""
    out = []
    stats = {
        "total_in": 0,
        "emitted": 0,
        "no_date": 0,
        "weekend_shifted": 0,
        "weekday_holiday_kept": 0,
        "weekday_trading": 0,
        "shift_examples": [],
        "holiday_examples": [],
    }
    for r in records:
        stats["total_in"] += 1
        raw = str(r.get(date_field, ""))
        d = parse_date(raw)
        if d is None:
            stats["no_date"] += 1
            continue
        g = shift_weekend_to_monday(d)
        r["publish_date"] = d.isoformat()
        r["game_publish_date"] = g.isoformat()
        if g != d:
            stats["weekend_shifted"] += 1
            if len(stats["shift_examples"]) < examples_cap:
                stats["shift_examples"].append(
                    f"{d.isoformat()}({WD_KO[d.weekday()]}) -> {g.isoformat()}({WD_KO[g.weekday()]})"
                )
        else:
            # 평일 — 거래일인지 휴장일인지 (정보용)
            if trading_days and d.isoformat() not in trading_days:
                stats["weekday_holiday_kept"] += 1
                if len(stats["holiday_examples"]) < examples_cap:
                    stats["holiday_examples"].append(
                        f"{d.isoformat()}({WD_KO[d.weekday()]}) 휴장일 유지 | {r.get('stock_name') or r.get('asset_id') or r.get('news_id','')}"
                    )
            else:
                stats["weekday_trading"] += 1
        out.append(r)
        stats["emitted"] += 1
    return out, stats


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def assert_no_weekend(records, label: str) -> int:
    bad = 0
    for r in records:
        g = parse_date(r.get("game_publish_date", ""))
        if g is None or g.weekday() >= 5:
            bad += 1
    if bad:
        raise AssertionError(f"[{label}] game_publish_date 주말 {bad}건 — 가드 실패")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock-outputs", default=str(DEF_STOCK_OUTPUTS))
    ap.add_argument("--stock-pool", default=str(DEF_STOCK_POOL))
    ap.add_argument("--market", default=str(DEF_MARKET))
    ap.add_argument("--annual", default=str(DEF_ANNUAL))
    ap.add_argument("--split", default=str(DEF_SPLIT))
    ap.add_argument("--out-dir", default=str(DEF_OUT_DIR))
    ap.add_argument("--kospi", default=str(DEF_KOSPI))
    ap.add_argument("--kosdaq", default=str(DEF_KOSDAQ))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    trading = load_trading_days(Path(args.kospi), Path(args.kosdaq))

    summary = {"trading_days_loaded": len(trading), "categories": {}}

    jobs = [
        ("stock_disclosure", "stock_news.game.jsonl",
         list(iter_stock_news(Path(args.stock_outputs), Path(args.stock_pool))), "publish_date"),
        ("market", "market_news.game.jsonl",
         list(iter_jsonl(Path(args.market), "market")), "publish_date"),
        ("annual_earnings", "annual_earnings_news.game.jsonl",
         list(iter_jsonl(Path(args.annual), "annual_earnings")), "publish_date"),
        ("split_article", "split_articles.game.jsonl",
         list(iter_jsonl(Path(args.split), "split_article")), "publish_date"),
    ]

    md = ["# 게임 발행일 거래일 정렬 리포트",
          "",
          "규칙: 토→+2(월), 일→+1(월), 월~금(휴장일 포함)→유지. (게임은 평일 휴장일에도 열림)",
          f"거래일 달력 로드: {len(trading)}일 (KOSPI∪KOSDAQ)",
          ""]
    total_shift = 0
    for cat, fname, recs, field in jobs:
        out_recs, st = process(recs, field, trading)
        write_jsonl(out_dir / fname, out_recs)
        assert_no_weekend(out_recs, cat)
        total_shift += st["weekend_shifted"]
        summary["categories"][cat] = {k: v for k, v in st.items() if not k.endswith("examples")}
        summary["categories"][cat]["output"] = str((out_dir / fname))
        md.append(f"## {cat}  (`{fname}`)")
        md.append(f"- 입력 {st['total_in']} / 출력 {st['emitted']} / 날짜없음 {st['no_date']}")
        md.append(f"- **주말→월요일 이동 {st['weekend_shifted']}건**")
        md.append(f"- 평일 휴장일 유지 {st['weekday_holiday_kept']}건 / 평일 거래일 {st['weekday_trading']}건")
        if st["shift_examples"]:
            md.append("- 이동 예시:")
            md += [f"    - {e}" for e in st["shift_examples"]]
        if st["holiday_examples"]:
            md.append("- 휴장일 유지 예시:")
            md += [f"    - {e}" for e in st["holiday_examples"]]
        md.append("")

    # 개별뉴스 제외 카운트
    sr = getattr(iter_stock_news, "skipped_rejected", 0)
    sn = getattr(iter_stock_news, "skipped_nodate", 0)
    summary["stock_disclosure_skipped_rejected"] = sr
    summary["stock_disclosure_skipped_no_date"] = sn
    summary["total_weekend_shifted"] = total_shift
    md.append(f"## 개별뉴스 제외")
    md.append(f"- status=rejected 제외 {sr}건, anchor_date 없음 제외 {sn}건")
    md.append("")
    cal_min = min(trading) if trading else "?"
    cal_max = max(trading) if trading else "?"
    md.append(f"## 주의 (holiday_kept 해석)")
    md.append(f"- 거래일 달력 범위는 {cal_min}~{cal_max}. 이 범위 밖(예: 2024년 연간실적 공시일)은 "
              f"실제 평일이어도 `weekday_holiday_kept`로 집계됨 — 진짜 휴장일 아님(이동 대상도 아님).")
    md.append(f"- 범위 내 holiday_kept(예: 시장뉴스)는 글로벌 매크로가 한국 휴장일에 발생한 정상 케이스로, "
              f"게임이 평일 휴장일에 열리므로 유지가 맞음.")
    md.append("")
    md.append(f"## 자체검증")
    md.append(f"- 전 카테고리 출력 game_publish_date 주말 0건 ✅ (assert 통과)")
    md.append(f"- 전체 주말→월요일 이동 합계: **{total_shift}건**")

    (out_dir / "calendar_alignment_report.md").write_text("\n".join(md), encoding="utf-8")
    (out_dir / "calendar_alignment_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[done] out_dir={out_dir}")
    print(f"  trading_days={len(trading)}  total_weekend_shifted={total_shift}")
    for cat, fname, _, _ in jobs:
        c = summary["categories"][cat]
        print(f"  {cat:18s} emitted={c['emitted']:>5} shifted={c['weekend_shifted']:>3} "
              f"holiday_kept={c['weekday_holiday_kept']:>3}")
    print(f"  stock skipped: rejected={sr} no_date={sn}")
    print("  self-check: game_publish_date weekend=0 (assert passed)")


if __name__ == "__main__":
    main()
