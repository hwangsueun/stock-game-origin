#!/usr/bin/env python3
"""Build a human review report from generated context validation samples."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def fmt(value: str) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-jsonl", type=Path, required=True)
    ap.add_argument("--outputs-jsonl", type=Path, required=True)
    ap.add_argument("--price-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    prices = {r["custom_id"]: r for r in csv.DictReader(args.price_csv.open(encoding="utf-8-sig"))}
    requests = {}
    for line in args.requests_jsonl.read_text(encoding="utf-8").splitlines():
        request = json.loads(line)
        payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
        if payload.get("market_context"):
            requests[request["custom_id"]] = payload

    outputs = {}
    for line in args.outputs_jsonl.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        body = row.get("response", {}).get("body", {})
        if not body:
            continue
        content = body["choices"][0]["message"]["content"]
        outputs[row["custom_id"]] = json.loads(content)

    groups = {"vol": [], "ret5": [], "ret1": [], "multi": []}
    for cid, payload in requests.items():
        reason = prices[cid]["material_reason"]
        key = "multi" if "|" in reason else ("vol" if reason.startswith("vol") else "ret5" if reason.startswith("ret5") else "ret1")
        groups[key].append(cid)
    selected = groups["vol"][:3] + groups["ret5"][:4] + groups["ret1"][:3] + groups["multi"][:2]

    lines = [
        "# 통합 맥락 뉴스 샘플 리뷰",
        "",
        "가격 재료성 유형별 대표 12건. `누락`은 임계값을 실제로 넘긴 지표가 생성문에 쓰이지 않은 경우다.",
        "",
    ]
    missing_count = 0
    for i, cid in enumerate(selected, 1):
        payload, price, output = requests[cid], prices[cid], outputs[cid]
        context = payload["market_context"]
        text = " ".join(output.get("news_lines", []))
        missing = []
        reason = price["material_reason"]
        if "ret1d" in reason and f"{abs(float(price['ret_1d'])):g}%" not in text:
            missing.append("임계 익일수익률")
        if "ret5d" in reason and f"{abs(float(price['ret_5d'])):g}%" not in text:
            missing.append("임계 5일수익률")
        if "vol" in reason and f"{float(price['vol_mult']):g}배" not in text:
            missing.append("임계 거래량")
        missing_count += bool(missing)
        sector = context.get("sector_context", {})
        lines += [
            f"## {i}. {payload['stock_name']} | {payload['anchor_date']} | {payload['event_family']}",
            "",
            f"- 재료성: `{reason}`",
            f"- 종목 지표: 익일 `{fmt(price['ret_1d'])}%`, 5일 `{fmt(price['ret_5d'])}%`, 거래량 `{fmt(price['vol_mult'])}배`",
            f"- 업종 지표: {sector.get('index_name', '-')} 익일 `{fmt(str(sector.get('sector_return_1d_pct', '')))}%`, 5일 `{fmt(str(sector.get('sector_return_5d_pct', '')))}%`",
            f"- 임계 지표 누락: **{', '.join(missing) if missing else '없음'}**",
            f"- 생성문: {text}",
            "",
        ]
    lines.insert(3, f"선정 12건 중 임계 지표 누락 샘플: **{missing_count}건**")
    lines.insert(4, "")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[done] samples={len(selected)} trigger_missing={missing_count} -> {args.out}")


if __name__ == "__main__":
    main()
