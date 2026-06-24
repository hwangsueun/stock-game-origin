#!/usr/bin/env python3
"""공시 기사 + 후속 반응 기사 분리 — 전량(material 사건) 확장.

build_split_article_prototype.py(8쌍 프로토타입)의 결정적 템플릿 로직을 그대로
재사용하되, 하드코딩 SAMPLE_KEYS를 제거하고 가격 반응이 재료성(material)인 전
사건에 대해 쌍을 생성한다. LLM 없음(무료·결정적).

- 공시 기사(day T): 공시 detail 팩트에서 사건문 생성. rcept_no 단위로 묶어 서로
  다른 공시의 상대방·금액이 섞이지 않게 함.
- 반응 기사(day T+1/T+5): 가격/섹터 반응 CSV의 material_reason horizon으로 생성.
  인과·감정·전망어 없이 비인과 주가/거래량/업종지수 사실만.
- dedup: 같은 (stock_code, reaction_publish_date, horizon)로 겹치는 후속 기사는
  1건만 유지(나머지 사건은 split 세트에서 제외 — outputs_all 단일 기사로 남음).
- 결함 텍스트(빈 금액/상대방 등 슬롯 누락)는 스킵하고 집계.

사용법:
  cd "/Users/hgs/Desktop/IISE-CD/data-pipeline/news_generator"
  python scripts/processors/build_split_articles_all.py \
    --requests-jsonl data/interim/pr06a_full_requests_v4_2_all_stocks_fixed/stock_news_sample_requests.jsonl \
    --price-csv data/interim/context_layers/price_reaction.csv \
    --sector-csv data/interim/context_layers/sector_reaction.csv \
    --dart-detail-csv data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv \
    --out data/interim/context_layers/split_articles_all/split_articles_all.md \
    --jsonl-out data/interim/context_layers/split_articles_all/split_articles_all.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd

# 프로토타입의 결정적 카피 빌더를 그대로 재사용한다.
from build_split_article_prototype import (
    DISPLAY_NAME,
    canonical_family,
    event_copy,
    reaction_copy,
    report_family,
)

# 슬롯 누락으로 깨진 문장을 걸러내는 마커.
DEGENERATE_MARKERS = [
    "  ",            # 빈 값으로 생긴 이중 공백
    "약 원",         # 금액 누락
    "규모  ",        # 금액 누락
    " 규모 을",
    " 규모 를",
    "지분을 에 취득",
    "지분을  에",
    "주당 배당금을 으로",
    "총액은 이다",
    "총액은 으로 집계",
]


def is_degenerate(text: str) -> bool:
    if not text or not text.strip():
        return True
    return any(marker in text for marker in DEGENERATE_MARKERS)


def is_material(reason: str) -> bool:
    return bool(reason) and reason not in ("none", "not_material", "no_price_data")


def horizon_of(reason: str) -> str:
    if "ret5d" in reason:
        return "5d"
    if "ret1d" in reason:
        return "1d"
    return "vol"


def _parse_sales_eok(text: str):
    """기사 본문에서 매출액을 억원 단위 정수로 파싱."""
    m = re.search(r"매출액 약 ([\d,]+조)?\s*([\d,]+억)?", text)
    if not m or not (m.group(1) or m.group(2)):
        return None
    jo = int(m.group(1).replace(",", "").replace("조", "")) * 10000 if m.group(1) else 0
    eok = int(m.group(2).replace(",", "").replace("억", "")) if m.group(2) else 0
    return jo + eok


def dedup_same_year_earnings(rows: list, stats) -> None:
    """같은 (종목·회계연도) 실적의 잠정→확정 재공시 중복 제거.

    매출이 최신 공시의 ±5% 이내면 동일 실적의 재공시로 보고 최신 공시만 남긴다.
    값이 크게 다르면(자회사/단독/연결 등 별개 주체) 모두 보존한다.
    제거 시 해당 사건의 공시+반응 쌍을 함께 뺀다.
    """
    from collections import defaultdict
    groups = defaultdict(list)  # (code, fiscal_year) -> [(publish_date, sales, cid)]
    for r in rows:
        if r.get("article_type") != "disclosure" or r.get("event_family") != "earnings":
            continue
        text = " ".join(r["news_lines"])
        my = re.search(r"(\d{4})년 매출액", text)
        sales = _parse_sales_eok(text)
        if not my or sales is None:
            continue
        groups[(r["stock_code"], my.group(1))].append((r["publish_date"], sales, r["source_custom_id"]))
    drop = set()
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort()  # publish_date 오름차순 → 마지막이 최신(확정)
        _, latest_sales, _ = items[-1]
        for _, sales, cid in items[:-1]:
            if latest_sales and abs(sales - latest_sales) <= 0.05 * latest_sales:
                drop.add(cid)
    if drop:
        rows[:] = [r for r in rows if r.get("source_custom_id") not in drop]
        stats["dedup_same_year_earnings"] = len(drop)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-jsonl", type=Path, required=True)
    ap.add_argument("--price-csv", type=Path, required=True)
    ap.add_argument("--sector-csv", type=Path, required=True)
    ap.add_argument("--dart-detail-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--jsonl-out", type=Path)
    args = ap.parse_args()

    price = {r["custom_id"]: r for r in csv.DictReader(args.price_csv.open(encoding="utf-8-sig"))}
    sector = {r["custom_id"]: r for r in csv.DictReader(args.sector_csv.open(encoding="utf-8-sig"))}

    requests = []
    for line in args.requests_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        request = json.loads(line)
        payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
        requests.append((request["custom_id"], payload))

    detail = pd.read_csv(args.dart_detail_csv, dtype=str).fillna("")

    output_rows = []
    md_pairs = []
    seen_reaction = set()
    stats = Counter()
    skipped_examples = []

    for cid, payload in requests:
        if cid not in price or not is_material(price[cid].get("material_reason", "")):
            stats["skip_not_material"] += 1
            continue
        if cid not in sector:
            stats["skip_no_sector"] += 1
            continue

        code = str(payload["stock_code"]).zfill(6)
        anchor = payload["anchor_date"]
        family = canonical_family(payload["event_family"])
        day = anchor.replace("-", "")
        rows = detail[(detail["stock_code"] == code) & detail["rcept_no"].str.startswith(day)].copy()
        rows = rows[rows["report_name"].map(report_family).eq(family)]
        if rows.empty:
            stats["skip_no_detail_row"] += 1
            continue
        row = rows.iloc[0]
        try:
            facts = json.loads(row["facts_json"])
        except (json.JSONDecodeError, TypeError):
            stats["skip_bad_facts_json"] += 1
            continue

        event_text, clause = event_copy(
            code, payload["stock_name"], anchor, family, row["report_name"], facts
        )
        if is_degenerate(event_text) or is_degenerate(clause):
            stats["skip_degenerate_event"] += 1
            if len(skipped_examples) < 12:
                skipped_examples.append((cid, family, event_text))
            continue

        reaction_date, reaction_text = reaction_copy(clause, price[cid], sector[cid])
        if is_degenerate(reaction_text):
            stats["skip_degenerate_reaction"] += 1
            continue

        h = horizon_of(price[cid]["material_reason"])
        dedup_key = (code, reaction_date, h)
        if dedup_key in seen_reaction:
            stats["skip_reaction_dedup"] += 1
            continue
        seen_reaction.add(dedup_key)

        stats["pairs_built"] += 1
        name = DISPLAY_NAME.get(code, payload["stock_name"])
        common = {
            "source_custom_id": cid,
            "stock_code": code,
            "stock_name": name,
            "event_family": family,
            "source_rcept_no": row["rcept_no"],
        }
        output_rows.append({
            **common, "article_id": f"{cid}__disclosure", "article_type": "disclosure",
            "publish_date": anchor, "news_lines": [event_text],
        })
        output_rows.append({
            **common, "article_id": f"{cid}__reaction", "article_type": "market_reaction_followup",
            "publish_date": reaction_date, "news_lines": [reaction_text],
            "material_reason": price[cid]["material_reason"],
        })
        if len(md_pairs) < 30:
            md_pairs.append(
                f"## {len(md_pairs)+1}. {name} | {family}\n\n"
                f"- 공시 기사 날짜: `{anchor}`\n- 공시 기사: {event_text}\n\n"
                f"- 반응 기사 날짜: `{reaction_date}`\n- 반응 기사: {reaction_text}\n\n"
                f"- 근거 공시: `{row['rcept_no']}` {row['report_name']}\n"
            )

    # 같은 회계연도 잠정→확정 재공시 중복 제거
    dedup_same_year_earnings(output_rows, stats)
    pairs_final = len(output_rows) // 2

    # 산출물
    args.out.parent.mkdir(parents=True, exist_ok=True)
    jsonl_out = args.jsonl_out or args.out.with_suffix(".jsonl")
    with jsonl_out.open("w", encoding="utf-8") as f:
        for row in output_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    header = [
        "# 공시 기사 + 후속 반응 기사 — 전량 빌드",
        "",
        f"- 쌍(pair): **{pairs_final}** (기사 {len(output_rows)}건 = 공시 {pairs_final} + 반응 {pairs_final})",
        "",
        "## 집계",
        "```json",
        json.dumps(dict(stats), ensure_ascii=False, indent=2),
        "```",
        "",
        "## 샘플 (앞 30쌍)",
        "",
    ]
    args.out.write_text("\n".join(header) + "\n" + "\n".join(md_pairs), encoding="utf-8")

    print(f"[done] pairs={pairs_final} articles={len(output_rows)}")
    print("[stats]", dict(stats))
    if skipped_examples:
        print("[degenerate examples]")
        for cid, fam, txt in skipped_examples:
            print(f"  {cid} {fam}: {txt}")
    print(f"  -> {jsonl_out}")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
