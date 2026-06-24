#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""뉴스에 직접 쓸 수 있는 맥락 레이어(가격 반응 + 섹터)를 pr06a 요청에 주입.

GDELT theme/tone은 독자용 문장으로 자연스럽지 않으므로 별도 sidecar에만
보존하고 생성 요청에는 주입하지 않는다.

재료성 있는 이벤트에만 market_context 블록과 비인과 주가-반응 작성 지침을 추가한다.
재료성 없으면 원 요청(no_market_claim 단신)을 그대로 유지한다.

사용법:
  python scripts/processors/inject_context_layers.py \
    --requests-jsonl /tmp/req_v7/stock_news_sample_requests.jsonl \
    --price-csv data/interim/context_layers/price_reaction.csv \
    --sector-csv data/interim/context_layers/sector_reaction.csv \
    --out data/interim/context_layers/requests_context_v8.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

# 시스템 프롬프트에 덧붙일 시장-반응 작성 지침 (재료성 이벤트 한정)
MARKET_REACTION_GUIDANCE = """

Market-reaction guidance (ONLY when brief_payload.market_context is present):
- You MAY add exactly ONE extra sentence about the post-disclosure stock reaction, placed as the LAST news line.
- Use ONLY the numbers in market_context. State them as adjacent facts, NEVER as caused by the disclosure.
- Use the directional verb from market_context (next_day_direction_ko / five_day_direction_ko): 상승→'올랐다', 하락→'내렸다'. Do NOT use the vague '변동했다' when a direction is given.
- Allowed phrasing: '공시 다음 거래일 주가는 약 X% 올랐다(내렸다).', '이후 5거래일간 약 Y% 올랐다(내렸다).', '거래량은 직전 20거래일 평균의 약 Z배 수준이었다.'
- When market_context.sector_context is present, you MAY add one same-period sector comparison as a clause in that same final line. Use only its sector_return values and index_name; do not infer causality.
- Allowed sector phrasing: '같은 기간 전기전자 업종지수는 약 X% 올랐다(내렸다).'
- FORBIDDEN: causal links (때문에/덕분에/영향으로/때문/로 인해), sentiment/interpretation (호재/악재/주목/기대/우려/긍정적/부정적/투자심리/관심), forecasts (전망/예상/것으로 보인다), '반응했다'(implies causation).
- Do NOT add a reaction sentence if market_context is absent. Keep the disclosure-only news.
- Keep the reaction sentence factual and neutral; it is co-occurrence, not cause."""


def load_reactions(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in csv.DictReader(path.open(encoding="utf-8-sig")):
        if str(r.get("material")) != "True":
            continue
        out[r["custom_id"]] = r
    return out


def load_sectors(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    out: dict[str, dict] = {}
    for r in csv.DictReader(path.open(encoding="utf-8-sig")):
        if r.get("status") == "ok":
            out[r["custom_id"]] = r
    return out


def reaction_context(r: dict, sector: dict | None = None) -> dict:
    """price_reaction 행 -> market_context payload(사실 + 방향 한국어)."""
    ctx: dict = {"trade_date": r.get("trade_date")}

    def fmt(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    r1, r5, vm = fmt(r.get("ret_1d")), fmt(r.get("ret_5d")), fmt(r.get("vol_mult"))
    if r1 is not None:
        ctx["next_day_change_pct"] = r1
        ctx["next_day_direction_ko"] = "상승" if r1 > 0 else ("하락" if r1 < 0 else "보합")
    if r5 is not None:
        ctx["five_day_change_pct"] = r5
        ctx["five_day_direction_ko"] = "상승" if r5 > 0 else ("하락" if r5 < 0 else "보합")
    if vm is not None:
        ctx["volume_vs_20d_avg_mult"] = vm
    if sector:
        s1, s5 = fmt(sector.get("sector_return_1d")), fmt(sector.get("sector_return_5d"))
        sector_ctx = {
            "market": sector.get("market"),
            "index_name": sector.get("index_name"),
            "index_code": sector.get("index_code"),
        }
        if s1 is not None:
            sector_ctx["sector_return_1d_pct"] = s1
            sector_ctx["sector_1d_direction_ko"] = "상승" if s1 > 0 else ("하락" if s1 < 0 else "보합")
        if s5 is not None:
            sector_ctx["sector_return_5d_pct"] = s5
            sector_ctx["sector_5d_direction_ko"] = "상승" if s5 > 0 else ("하락" if s5 < 0 else "보합")
        ctx["sector_context"] = sector_ctx
    ctx["note"] = "공시 직후 시장 반응 지표. 인과 단정 금지, 인접 사실로만 사용."
    return ctx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-jsonl", type=Path, required=True)
    ap.add_argument("--price-csv", type=Path, required=True)
    ap.add_argument("--sector-csv", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    reacts = load_reactions(args.price_csv)
    sectors = load_sectors(args.sector_csv)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    n = n_ctx = n_sector = 0
    with args.out.open("w", encoding="utf-8") as f:
        for line in args.requests_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            n += 1
            cid = d["custom_id"]
            if cid in reacts:
                n_ctx += 1
                if cid in sectors:
                    n_sector += 1
                msgs = d["body"]["messages"]
                msgs[0]["content"] = msgs[0]["content"] + MARKET_REACTION_GUIDANCE
                payload = json.loads(msgs[1]["content"])
                payload["brief_payload"]["market_context"] = reaction_context(reacts[cid], sectors.get(cid))
                # claim level 표기 갱신
                payload["brief_payload"]["claim_level"] = "market_reaction_adjacency"
                msgs[1]["content"] = json.dumps(payload, ensure_ascii=False)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"[done] requests={n} with_market_context={n_ctx} with_sector_context={n_sector} -> {args.out}")


if __name__ == "__main__":
    main()
