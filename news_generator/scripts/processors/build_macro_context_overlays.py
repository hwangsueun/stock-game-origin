#!/usr/bin/env python3
"""Build sector-breadth and exceptional-stock overlays for daily macro news."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_split_article_prototype import has_jongseong  # noqa: E402


def number(text: str, unit: str) -> float | None:
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*" + re.escape(unit), text or "")
    return float(match.group(1)) if match else None


def as_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def disclosed_amount_eok(text: str) -> int:
    jo = re.search(r"약\s*(\d+)조", text)
    eok = re.search(r"(?:조)?([\d,]+)억원", text)
    return (int(jo.group(1)) * 10_000 if jo else 0) + (int(eok.group(1).replace(",", "")) if eok else 0)


def topic(name: str) -> str:
    return f"{name}{'은' if has_jongseong(name) else '는'}"


def sector_overlays(context_csv: Path, event_csv: Path) -> dict[str, list[dict]]:
    by_date_market: dict[tuple[str, str], list[dict]] = defaultdict(list)
    sector_return_lookup = {}
    with context_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            ret = as_float(row.get("sector_return_1d"))
            if ret is not None:
                by_date_market[(row["date"], row["market"])].append({**row, "return": ret})
                sector_return_lookup[(row["date"], row["market"], row["index_name"])] = ret

    result: dict[str, list[dict]] = defaultdict(list)
    for (date, market), rows in by_date_market.items():
        returns = [row["return"] for row in rows]
        advancers = sum(value > 0 for value in returns)
        decliners = sum(value < 0 for value in returns)
        unchanged = len(returns) - advancers - decliners
        best = max(rows, key=lambda row: row["return"])
        worst = min(rows, key=lambda row: row["return"])
        avg_return = statistics.fmean(returns)
        median_return = statistics.median(returns)
        positive_share = advancers / len(rows)
        negative_share = decliners / len(rows)
        direction = (
            "positive" if positive_share >= 0.6 and median_return > 0
            else "negative" if negative_share >= 0.6 and median_return < 0
            else "mixed"
        )
        result[date].append({
            "event_id": f"{date}_sector_breadth_{market.lower()}",
            "macro_angle": "market_breadth",
            "angle_label": f"{market} 업종 전반 {'강세' if direction == 'positive' else '약세' if direction == 'negative' else '혼조'}",
            "market_implication": (
                f"{market} 업종지수 {len(rows)}개 중 {advancers}개가 상승하고 "
                f"{decliners}개가 하락해 업종 전반의 확산도를 보여줌"
            ),
            "direction": direction,
            "severity": "strong" if abs(avg_return) >= 2 or abs(advancers - decliners) >= len(rows) * 0.6 else "moderate",
            "source_columns": ["sector_context_daily"],
            "evidence": {
                "market": market,
                "sector_count": len(rows),
                "advancers": advancers,
                "decliners": decliners,
                "unchanged": unchanged,
                "positive_share": round(positive_share, 4),
                "best_sector": best["index_name"],
                "worst_sector": worst["index_name"],
            },
            "key_figures": {
                "advancers": advancers,
                "decliners": decliners,
                "average_sector_return_pct": f"{avg_return:+.2f}%",
                "median_sector_return_pct": f"{median_return:+.2f}%",
                "best_sector": f"{best['index_name']} {best['return']:+.2f}%",
                "worst_sector": f"{worst['index_name']} {worst['return']:+.2f}%",
            },
        })

    strongest: dict[tuple[str, str], tuple[float, dict]] = {}
    with event_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["event_type"] not in {"sector_leader", "sector_laggard"}:
                continue
            if int(row.get("strength") or 0) < 4:
                continue
            excess = number(row.get("evidence_3", ""), "%p")
            if excess is None or abs(excess) < 2:
                continue
            key = (row["date"], row["market"])
            if key not in strongest or abs(excess) > strongest[key][0]:
                strongest[key] = (abs(excess), {**row, "excess": excess})

    for (date, market), (_, row) in strongest.items():
        excess = row["excess"]
        sector_return = sector_return_lookup.get((date, market, row["sector"]))
        result[date].append({
            "event_id": f"{date}_sector_dislocation_{row['asset_id']}",
            "macro_angle": "risk_sentiment",
            "angle_label": f"{market} {row['sector']} 업종, 시장 대비 {'강세' if excess > 0 else '약세'}",
            "market_implication": f"{row['sector']} 업종이 같은 시장 대비 {abs(excess):.2f}%p {'앞섬' if excess > 0 else '뒤처짐'}",
            "direction": "positive" if excess > 0 else "negative",
            "severity": "strong" if abs(excess) >= 3 else "moderate",
            "source_columns": ["sector_event_candidates_daily"],
            "evidence": {
                "market": market,
                "sector": row["sector"],
                "event_type": row["event_type"],
                "comparison_basis": "market_relative",
            },
            "key_figures": {
                "sector_return_pct": f"{sector_return:+.2f}%" if sector_return is not None else None,
                "relative_return_pct_point": f"{excess:+.2f}%p",
            },
        })
    return result


def major_stock_overlays(
    reaction_csv: Path,
    split_jsonl: Path,
    issuer_validation_csv: Path,
    stock_sector_mapping_csv: Path,
    sector_context_csv: Path,
) -> dict[str, list[dict]]:
    reactions = {}
    with reaction_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("data_quality") == "ok":
                reactions[row["custom_id"]] = row

    issuer_names = {}
    with issuer_validation_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("issuer_name_resolved"):
                issuer_names[row["rcept_no"]] = row["issuer_name_resolved"]

    stock_sectors = {}
    with stock_sector_mapping_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            stock_sectors[row["stock_code"]] = (row["market"], row["index_name"])

    sector_returns = {}
    with sector_context_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            sector_returns[(row["date"], row["market"], row["index_name"])] = (
                as_float(row.get("sector_return_1d")),
                as_float(row.get("sector_return_5d")),
            )

    candidates: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    with split_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            article = json.loads(line)
            if article.get("article_type") != "market_reaction_followup":
                continue
            reaction = reactions.get(article.get("source_custom_id"))
            if not reaction:
                continue
            ret_1d = as_float(reaction.get("ret_1d"))
            ret_5d = as_float(reaction.get("ret_5d"))
            vol_mult = as_float(reaction.get("vol_mult"))
            article_text = article["news_lines"][0]
            amount_eok = disclosed_amount_eok(article_text)
            mapping = stock_sectors.get(article["stock_code"])
            sector_1d, sector_5d = sector_returns.get((article["publish_date"], *mapping), (None, None)) if mapping else (None, None)
            aligned_sector_move = (
                ret_1d is not None and sector_1d is not None
                and abs(sector_1d) >= 2 and ret_1d * sector_1d > 0
            ) or (
                ret_5d is not None and sector_5d is not None
                and abs(sector_5d) >= 2 and ret_5d * sector_5d > 0
            )
            exceptional_move = (
                (ret_1d is not None and abs(ret_1d) >= 15)
                or (ret_5d is not None and abs(ret_5d) >= 25)
            ) and (aligned_sector_move or (vol_mult is not None and vol_mult >= 5))
            large_event_with_reaction = amount_eok >= 10_000 and (
                (ret_1d is not None and abs(ret_1d) >= 8)
                or (ret_5d is not None and abs(ret_5d) >= 15)
            )
            if not (exceptional_move or large_event_with_reaction):
                continue
            score = max(
                abs(ret_1d or 0) / 10,
                abs(ret_5d or 0) / 15,
                (vol_mult or 0) / 5,
            )
            candidates[article["publish_date"]].append((score, {"article": article, "reaction": reaction}))

    result: dict[str, list[dict]] = defaultdict(list)
    for date, rows in candidates.items():
        _, item = max(rows, key=lambda pair: pair[0])
        article, reaction = item["article"], item["reaction"]
        issuer_name = issuer_names.get(article.get("source_rcept_no"), article["stock_name"])
        article_text = article["news_lines"][0]
        if issuer_name != article["stock_name"]:
            article_text = article_text.replace(topic(article["stock_name"]), topic(issuer_name), 1)
        ret_1d = as_float(reaction.get("ret_1d"))
        ret_5d = as_float(reaction.get("ret_5d"))
        is_next_day_article = "다음 거래일" in article_text
        direction_value = (ret_1d or 0) if is_next_day_article else (ret_5d or 0)
        reaction_figure = (
            {"ret_1d_pct": f"{ret_1d:+.2f}%"} if is_next_day_article and ret_1d is not None
            else {"ret_5d_pct": f"{ret_5d:+.2f}%"} if ret_5d is not None
            else {}
        )
        result[date].append({
            "event_id": f"{date}_major_stock_{article['source_custom_id']}",
            "macro_angle": "risk_sentiment",
            "angle_label": f"주요 종목 반응: {issuer_name}",
            "market_implication": article_text,
            "direction": "positive" if direction_value > 0 else "negative" if direction_value < 0 else "mixed",
            "severity": "strong",
            "event_role": "headline",
            "source_columns": ["split_article_reaction", "price_reaction"],
            "evidence": {
                "stock_code": article["stock_code"],
                "stock_name": issuer_name,
                "event_family": article["event_family"],
                "article_text": article_text,
                "usage_rule": "개별 종목의 예외적으로 큰 반응으로만 서술하고 시장 전체 원인으로 확대하지 않음",
            },
            "key_figures": {"stock_name": issuer_name, **reaction_figure},
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector-context-csv", type=Path, required=True)
    parser.add_argument("--sector-event-csv", type=Path, required=True)
    parser.add_argument("--price-reaction-csv", type=Path, required=True)
    parser.add_argument("--split-articles-jsonl", type=Path, required=True)
    parser.add_argument("--issuer-validation-csv", type=Path, required=True)
    parser.add_argument("--stock-sector-mapping-csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    overlays = sector_overlays(args.sector_context_csv, args.sector_event_csv)
    stocks = major_stock_overlays(
        args.price_reaction_csv,
        args.split_articles_jsonl,
        args.issuer_validation_csv,
        args.stock_sector_mapping_csv,
        args.sector_context_csv,
    )
    for date, events in stocks.items():
        overlays[date].extend(events)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for date in sorted(overlays):
            handle.write(json.dumps({"date": date, "events": overlays[date]}, ensure_ascii=False) + "\n")

    counts = {
        "dates": len(overlays),
        "sector_breadth": sum(e["event_id"].find("sector_breadth") >= 0 for events in overlays.values() for e in events),
        "sector_dislocation": sum(e["event_id"].find("sector_dislocation") >= 0 for events in overlays.values() for e in events),
        "major_stock": sum(e["event_id"].find("major_stock") >= 0 for events in overlays.values() for e in events),
    }
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
