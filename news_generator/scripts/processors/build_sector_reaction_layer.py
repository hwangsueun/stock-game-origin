#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build post-disclosure KRX sector-index context for stock news events.

The stock-to-sector mapping is intentionally explicit and validated against the
sector index file.  Returns use the same t0 convention as the price layer:
t0 is the last trading date on or before anchor_date, then t+1 and t+5 are
measured from the sector index close.  Market returns come from the matching
rows in sector_context_daily.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECTOR_CONTEXT = BASE_DIR.parent / "market_indicator/data/processed/sector_context_daily.csv"

# KRX market and coarse industry index assignment for the fixed 117-stock universe.
# KOSPI does not expose transport/pharma sub-indices in this source, so those
# issuers intentionally use the broad Manufacturing index.
MAPPING_ROWS = """
000660|SK하이닉스|KOSPI|전기전자
058470|리노공업|KOSDAQ|전기전자
000990|DB하이텍|KOSPI|전기전자
042700|한미반도체|KOSPI|기계·장비
003550|LG|KOSPI|금융
000240|한국앤컴퍼니|KOSPI|화학
000270|기아|KOSPI|제조
161390|한국타이어앤테크놀로지|KOSPI|화학
004000|롯데정밀화학|KOSPI|화학
010130|고려아연|KOSPI|금속
014680|한솔케미칼|KOSPI|화학
000210|DL|KOSPI|금융
285130|SK케미칼|KOSPI|화학
011780|금호석유화학|KOSPI|화학
010060|OCI홀딩스|KOSPI|화학
012750|에스원|KOSPI|일반서비스
030190|NICE평가정보|KOSDAQ|금융
064350|현대로템|KOSPI|제조
012450|한화에어로스페이스|KOSPI|기계·장비
039490|키움증권|KOSPI|증권
071050|한국금융지주|KOSPI|금융
006800|미래에셋증권|KOSPI|증권
138040|메리츠금융지주|KOSPI|금융
017670|SK텔레콤|KOSPI|통신
021240|코웨이|KOSPI|일반서비스
007700|F&F 홀딩스|KOSPI|금융
383220|F&F|KOSPI|섬유·의류
204620|글로벌텍스프리|KOSDAQ|유통
041190|우리기술투자|KOSDAQ|금융
029780|삼성카드|KOSPI|금융
032830|삼성생명|KOSPI|보험
033780|케이티앤지|KOSPI|음식료·담배
034730|SK|KOSPI|금융
052690|한전기술|KOSPI|일반서비스
006120|SK디스커버리|KOSPI|금융
078930|GS|KOSPI|금융
034830|한국토지신탁|KOSPI|금융
210980|SK디앤디|KOSPI|일반서비스
035250|강원랜드|KOSPI|일반서비스
215000|골프존|KOSDAQ|오락·문화
363280|티와이홀딩스|KOSPI|금융
036570|NC|KOSPI|일반서비스
263750|펄어비스|KOSDAQ|출판·매체복제
035420|NAVER|KOSPI|일반서비스
259960|크래프톤|KOSPI|일반서비스
213420|덕산네오룩스|KOSDAQ|화학
049950|미래컴퍼니|KOSDAQ|기계·장비
272290|이녹스첨단소재|KOSDAQ|화학
121800|비덴트|KOSDAQ|전기전자
253450|스튜디오드래곤|KOSDAQ|오락·문화
035900|JYP Ent.|KOSDAQ|오락·문화
055550|신한지주|KOSPI|금융
323410|카카오뱅크|KOSPI|금융
207940|삼성바이오로직스|KOSPI|제조
068270|셀트리온|KOSPI|제조
302440|SK바이오사이언스|KOSPI|제조
139480|이마트|KOSPI|유통
008770|호텔신라|KOSPI|유통
282330|BGF리테일|KOSPI|유통
051600|한전KPS|KOSPI|일반서비스
015760|한국전력공사|KOSPI|전기·가스
036460|한국가스공사|KOSPI|전기·가스
086280|현대글로비스|KOSPI|유통
011200|HMM|KOSPI|일반서비스
180640|한진칼|KOSPI|일반서비스
098460|고영|KOSDAQ|기계·장비
032500|케이엠더블유|KOSDAQ|전기전자
036830|솔브레인홀딩스|KOSDAQ|화학
278280|천보|KOSDAQ|화학
009150|삼성전기|KOSPI|전기전자
393890|더블유씨피|KOSDAQ|전기전자
028300|HLB|KOSDAQ|제약
214150|클래시스|KOSDAQ|의료·정밀기기
096530|씨젠|KOSDAQ|제약
137310|에스디바이오센서|KOSPI|제조
051900|LG생활건강|KOSPI|화학
214370|케어젠|KOSDAQ|제약
002380|케이씨씨|KOSPI|화학
028260|삼성물산|KOSPI|유통
004800|효성|KOSPI|금융
005380|현대자동차|KOSPI|제조
060980|HL홀딩스|KOSPI|금융
011170|롯데케미칼|KOSPI|화학
027410|BGF|KOSPI|금융
047810|한국항공우주|KOSPI|제조
016360|삼성증권|KOSPI|증권
192400|쿠쿠홀딩스|KOSPI|금융
016600|큐캐피탈|KOSDAQ|금융
010950|S-Oil|KOSPI|화학
035720|카카오|KOSPI|일반서비스
078340|컴투스|KOSDAQ|출판·매체복제
046890|서울반도체|KOSDAQ|전기전자
149950|아바텍|KOSDAQ|전기전자
051360|토비스|KOSDAQ|전기전자
108230|톱텍|KOSDAQ|기계·장비
141000|비아트론|KOSDAQ|기계·장비
078150|HB테크놀러지|KOSDAQ|기계·장비
053210|케이티스카이라이프|KOSPI|일반서비스
214320|이노션|KOSPI|일반서비스
030000|제일기획|KOSPI|일반서비스
089600|KT나스미디어|KOSDAQ|출판·매체복제
069960|현대백화점|KOSPI|유통
071320|지역난방공사|KOSPI|전기·가스
089590|제주항공|KOSPI|일반서비스
028670|팬오션|KOSPI|일반서비스
003490|대한항공|KOSPI|일반서비스
091700|파트론|KOSDAQ|전기전자
094840|슈프리마에이치큐|KOSDAQ|전기전자
192440|슈피겐코리아|KOSDAQ|유통
006400|삼성SDI|KOSPI|전기전자
099190|아이센스|KOSDAQ|의료·정밀기기
041830|인바디|KOSDAQ|의료·정밀기기
039840|디오|KOSDAQ|의료·정밀기기
100120|뷰웍스|KOSDAQ|의료·정밀기기
214450|파마리서치|KOSDAQ|제약
126560|현대퓨처넷|KOSPI|일반서비스
090430|아모레퍼시픽|KOSPI|화학
""".strip()


def stock_mapping() -> dict[str, dict[str, str]]:
    rows = [line.split("|") for line in MAPPING_ROWS.splitlines()]
    mapping = {code: {"stock_name": name, "market": market, "index_name": sector}
               for code, name, market, sector in rows}
    if len(mapping) != len(rows):
        raise ValueError("duplicate stock code in sector mapping")
    return mapping


def iter_events(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if "body" in d:
            bp = json.loads(d["body"]["messages"][1]["content"])["brief_payload"]
            yield d["custom_id"], bp.get("stock_code"), bp.get("stock_name"), bp.get("anchor_date")
        else:
            yield d.get("bundle_id"), d.get("stock_code"), d.get("stock_name"), d.get("anchor_date")


def _pct(a: float, b: float) -> float:
    return round((b / a - 1.0) * 100.0, 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events-jsonl", type=Path, required=True)
    ap.add_argument("--sector-context-csv", type=Path, default=DEFAULT_SECTOR_CONTEXT)
    ap.add_argument("--out", type=Path, default=BASE_DIR / "data/interim/context_layers/sector_reaction.csv")
    ap.add_argument("--mapping-out", type=Path, default=BASE_DIR / "data/interim/context_layers/stock_sector_mapping.csv")
    args = ap.parse_args()

    mapping = stock_mapping()
    events = list(iter_events(args.events_jsonl))
    event_codes = {str(code).zfill(6) for _, code, _, _ in events}
    missing = sorted(event_codes - mapping.keys())
    extra = sorted(mapping.keys() - event_codes)
    if missing or extra:
        raise ValueError(f"mapping universe mismatch: missing={missing}, extra={extra}")

    context = pd.read_csv(args.sector_context_csv, dtype={"index_code": str})
    context["date"] = pd.to_datetime(context["date"])
    valid_pairs = set(zip(context["market"], context["index_name"]))
    invalid = sorted((code, m["market"], m["index_name"]) for code, m in mapping.items()
                     if (m["market"], m["index_name"]) not in valid_pairs)
    if invalid:
        raise ValueError(f"mapping refers to unavailable sector indices: {invalid}")

    series = {}
    for pair, group in context.groupby(["market", "index_name"], sort=False):
        series[pair] = group.sort_values("date").drop_duplicates("date", keep="last").set_index("date")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.mapping_out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["stock_code", "stock_name", "market", "index_name", "mapping_method"])
        for code, m in sorted(mapping.items()):
            w.writerow([code, m["stock_name"], m["market"], m["index_name"], "explicit_krx_coarse_sector"])

    fields = ["custom_id", "stock_code", "stock_name", "anchor_date", "trade_date", "market",
              "index_name", "index_code", "sector_return_1d", "sector_return_5d",
              "market_return_1d", "market_return_5d", "relative_return_1d", "relative_return_5d",
              "status"]
    counts = {"ok": 0, "no_sector_data": 0}
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for cid, raw_code, name, anchor in events:
            code = str(raw_code).zfill(6)
            m = mapping[code]
            df = series[(m["market"], m["index_name"])]
            ad = pd.Timestamp(anchor)
            pos = df.index.searchsorted(ad, side="right") - 1
            row = dict(custom_id=cid, stock_code=code, stock_name=name, anchor_date=anchor,
                       market=m["market"], index_name=m["index_name"], status="no_sector_data")
            if pos >= 0 and pos + 5 < len(df):
                t0, t1, t5 = df.iloc[pos], df.iloc[pos + 1], df.iloc[pos + 5]
                s1, s5 = _pct(t0["close"], t1["close"]), _pct(t0["close"], t5["close"])
                mr1 = round(float(t1["market_return_1d"]), 2)
                mr5 = round(float(t5["market_return_5d"]), 2)
                index_code = str(t0["index_code"]).removesuffix(".0")
                row.update(trade_date=df.index[pos].date().isoformat(), index_code=index_code,
                           sector_return_1d=s1, sector_return_5d=s5,
                           market_return_1d=mr1, market_return_5d=mr5,
                           relative_return_1d=round(s1 - mr1, 2), relative_return_5d=round(s5 - mr5, 2),
                           status="ok")
            counts[row["status"]] += 1
            w.writerow(row)
    print(f"[done] events={len(events)} stocks={len(mapping)} status={counts} -> {args.out}")
    print(f"[done] mapping -> {args.mapping_out}")


if __name__ == "__main__":
    main()
