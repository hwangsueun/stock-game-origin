#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""주가 반응 맥락 레이어 빌더.

각 뉴스 이벤트(stock_code + anchor_date)에 대해 공시 직후 주가/거래량 반응을 계산하고
재료성(materiality)을 판정해 CSV로 저장한다. 맥락 인식 뉴스 생성의 price 레이어 입력.

반응 정의:
- t0 = anchor_date 이하의 마지막 거래일(공시 시점 종가)
- ret_1d = close[t0+1]/close[t0]-1, ret_5d = close[t0+5]/close[t0]-1
- vol_mult = vol[t0+1] / mean(vol[t0-20:t0])  (직전 20거래일 평균 대비)
- materiality: |ret_1d| >= 5%, |ret_5d| >= 8%, 또는 vol_mult >= 3.0

사용법:
  cd "/Users/hgs/Desktop/IISE-CD/data-pipeline/news_generator"
  python scripts/processors/build_price_reaction_layer.py \
    --events-jsonl <requests_or_briefs.jsonl> \
    --out data/interim/context_layers/price_reaction.csv
"""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parents[2]
PV_XLSX = BASE_DIR.parent / "data/raw/stock/stock_price-volume_npq.xlsx"
PV_SHEETS = ["13-17_price-volume", "18-22_price-volume", "23_price-volume"]

RET1_THRESHOLD = 5.0  # % — 익일 수익률 재료성 임계
RET5_THRESHOLD = 8.0  # % — 5거래일 수익률 재료성 임계
VOL_THRESHOLD = 3.0   # 배 — 거래량 재료성 임계


def load_price_volume(xlsx: Path) -> dict[str, pd.DataFrame]:
    """엑셀(종목별 종가/거래량 2열 구조)을 {stock_code: DataFrame[close, vol]}로 로드."""
    x = pd.ExcelFile(xlsx)
    series: dict[str, dict[str, pd.Series]] = {}
    for sh in PV_SHEETS:
        raw = x.parse(sh, header=None)
        codes = raw.iloc[8].tolist()
        items = raw.iloc[12].tolist()
        data = raw.iloc[14:].reset_index(drop=True)
        dates = pd.to_datetime(data.iloc[:, 0], errors="coerce")
        for j in range(1, len(codes)):
            code = str(codes[j])
            if not code.startswith("A"):
                continue
            sc = code[1:]
            col = "close" if "종가" in str(items[j]) else ("vol" if "거래량" in str(items[j]) else None)
            if not col:
                continue
            vals = pd.to_numeric(data.iloc[:, j], errors="coerce")
            s = pd.Series(vals.values, index=dates.values)
            series.setdefault(sc, {})
            series[sc][col] = pd.concat([series[sc].get(col, pd.Series(dtype=float)), s])
    out: dict[str, pd.DataFrame] = {}
    for sc, d in series.items():
        df = pd.DataFrame({"close": d.get("close"), "vol": d.get("vol")}).sort_index()
        df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
        out[sc] = df
    return out


def reaction(pv: dict[str, pd.DataFrame], code: str, anchor: str) -> dict | None:
    df = pv.get(code)
    if df is None or df.empty:
        return None
    ad = pd.Timestamp(anchor)
    pos = df.index.searchsorted(ad, side="right") - 1
    if pos < 1 or pos + 1 >= len(df):
        return None
    c0 = df["close"].iloc[pos]
    if not c0 or pd.isna(c0):
        return None

    def ret(k: int):
        if pos + k < len(df):
            return round((df["close"].iloc[pos + k] / c0 - 1) * 100, 2)
        return None

    ret1 = ret(1)
    ret5 = ret(5)
    v_base = df["vol"].iloc[max(0, pos - 20):pos].mean()
    v_event = df["vol"].iloc[pos + 1] if pos + 1 < len(df) else None
    vol_mult = round(v_event / v_base, 2) if (v_base and v_event and v_base > 0) else None

    # KRX daily price limit widened from 15% to 30% on 2015-06-15. Returns beyond
    # the compounded legal bound indicate an unadjusted corporate action or a
    # broken series and must not create a reaction article.
    daily_limit = 0.15 if df.index[pos].date() < pd.Timestamp("2015-06-15").date() else 0.30
    upper_1d, lower_1d = daily_limit * 100, -daily_limit * 100
    upper_5d = ((1 + daily_limit) ** 5 - 1) * 100
    lower_5d = ((1 - daily_limit) ** 5 - 1) * 100
    quality_flags = []
    if ret1 is not None and not (lower_1d - 0.5 <= ret1 <= upper_1d + 0.5):
        quality_flags.append("ret1_outside_krx_limit")
    if ret5 is not None and not (lower_5d - 1.0 <= ret5 <= upper_5d + 1.0):
        quality_flags.append("ret5_outside_compounded_krx_limit")

    material = False
    reasons = []
    if ret1 is not None and abs(ret1) >= RET1_THRESHOLD:
        material = True
        reasons.append(f"ret1d>={RET1_THRESHOLD}")
    if ret5 is not None and abs(ret5) >= RET5_THRESHOLD:
        material = True
        reasons.append(f"ret5d>={RET5_THRESHOLD}")
    if vol_mult is not None and vol_mult >= VOL_THRESHOLD:
        material = True
        reasons.append(f"vol>={VOL_THRESHOLD}")
    if quality_flags:
        material = False
        reasons = []
    return {
        "trade_date": df.index[pos].date().isoformat(),
        "date_1d": df.index[pos + 1].date().isoformat() if pos + 1 < len(df) else None,
        "date_5d": df.index[pos + 5].date().isoformat() if pos + 5 < len(df) else None,
        "ret_1d": ret1,
        "ret_5d": ret5,
        "vol_mult": vol_mult,
        "material": material,
        "material_reason": "|".join(sorted(set(reasons))),
        "data_quality": "|".join(quality_flags) if quality_flags else "ok",
    }


def iter_events(path: Path):
    """requests jsonl 또는 briefs jsonl에서 (custom_id, stock_code, stock_name, anchor_date, event_family) 추출."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if "body" in d:  # pr06a 요청 형식
            bp = json.loads(d["body"]["messages"][1]["content"]).get("brief_payload", {})
            yield d["custom_id"], bp.get("stock_code"), bp.get("stock_name"), bp.get("anchor_date"), bp.get("event_family")
        else:  # 브리프 형식
            yield d.get("bundle_id"), d.get("stock_code"), d.get("stock_name"), d.get("anchor_date"), d.get("event_family")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events-jsonl", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=BASE_DIR / "data/interim/context_layers/price_reaction.csv")
    ap.add_argument("--pv-xlsx", type=Path, default=PV_XLSX)
    args = ap.parse_args()

    print(f"[load] price/volume: {args.pv_xlsx}")
    pv = load_price_volume(args.pv_xlsx)
    print(f"[load] {len(pv)} stocks")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    import csv
    n = 0
    n_react = 0
    n_material = 0
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["custom_id", "stock_code", "stock_name", "anchor_date", "event_family",
                    "trade_date", "date_1d", "date_5d", "ret_1d", "ret_5d", "vol_mult",
                    "material", "material_reason", "data_quality"])
        for cid, sc, nm, ad, fam in iter_events(args.events_jsonl):
            n += 1
            r = reaction(pv, sc, ad) if (sc and ad) else None
            if r:
                n_react += 1
                if r["material"]:
                    n_material += 1
                w.writerow([cid, sc, nm, ad, fam, r["trade_date"], r["date_1d"], r["date_5d"],
                            r["ret_1d"], r["ret_5d"], r["vol_mult"], r["material"],
                            r["material_reason"], r["data_quality"]])
            else:
                w.writerow([cid, sc, nm, ad, fam, "", "", "", "", "", "", False,
                            "no_price_data", "no_price_data"])
    print(f"[done] events={n} with_reaction={n_react} material={n_material} -> {args.out}")


if __name__ == "__main__":
    main()
