#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DART 재무제표 API(fnlttSinglAcntAll)로 전 종목·전 연도 연간 재무 수집.

서술 파싱(사업보고서 XML)은 회사별 표현 차이로 수율 3~16%라 신뢰 불가 →
구조화 API로 매출액·영업이익·당기순이익을 정확히 받는다.

날짜 정합성: 뉴스 날짜는 회계연도말이 아니라 **사업보고서 공시일(rcept_date)**.
  rcept_date는 dart_business_report_index.csv((stock, year)별 사업보고서 접수일)에서 가져온다.
  인덱스에 없는 (stock, year)는 회계연도+1의 3/31을 보수적 추정으로 쓰되 'date_estimated' 플래그.

사용법:
  cd "/Users/hgs/Desktop/IISE-CD/data-pipeline/news_generator"
  python scripts/processors/fetch_annual_financials.py \
    --out data/interim/pr05f_dart_annual_financial_facts_v2_all/annual_financials_api.csv \
    --start-year 2012 --end-year 2023
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[2]  # news_generator/
URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
DART_JSON = BASE / "dart_collector/dart_results_2013_2024.json"
INDEX_CSV = BASE / "data/processed/dart_business_report_index.csv"


def load_env(path: Path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def corp_map() -> dict:
    d = json.loads(DART_JSON.read_text(encoding="utf-8"))
    out, names = {}, {}
    def walk(o):
        if isinstance(o, dict):
            sc, cc = o.get("stock_code"), o.get("corp_code")
            if sc and cc:
                out[str(sc).zfill(6)] = cc
                if o.get("corp_name"):
                    names[str(sc).zfill(6)] = o["corp_name"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return out, names


def rcept_date_map() -> dict:
    """(stock_code, business_year) -> rcept_date(YYYY-MM-DD), 사업보고서 중 가장 이른 접수."""
    m = {}
    if not INDEX_CSV.exists():
        return m
    for r in csv.DictReader(INDEX_CSV.open(encoding="utf-8-sig")):
        sc = (r["stock_code"] or "").zfill(6)[-6:]
        yr = r["business_year"]
        raw = re.sub(r"\D", "", r.get("rcept_date", ""))
        if len(raw) >= 8:
            iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
            key = (sc, yr)
            if key not in m or iso < m[key]:
                m[key] = iso
    return m


def names_map() -> dict:
    """stock_code -> 한글 종목명 (디스클로저 detail CSV에서)."""
    p = BASE / "data/interim/pr05f_dart_disclosure_detail_facts_v2_all/dart_disclosure_detail_facts.csv"
    out = {}
    if p.exists():
        for r in csv.DictReader(p.open(encoding="utf-8-sig")):
            out[(r["stock_code"] or "").zfill(6)[-6:]] = r["stock_name"]
    return out


def pick(accounts: list, *targets):
    """account_nm이 targets 중 하나와 정확히 일치하는 당기금액을 반환(IS/CIS 우선)."""
    for want in targets:
        for a in accounts:
            nm = (a.get("account_nm") or "").replace(" ", "")
            if nm == want and a.get("sj_div") in ("IS", "CIS"):
                amt = (a.get("thstrm_amount") or "").replace(",", "")
                if re.fullmatch(r"-?\d+", amt):
                    return int(amt)
    return None


def fetch(key, corp, year, fs_div):
    try:
        r = requests.get(URL, params={
            "crtfc_key": key, "corp_code": corp, "bsns_year": str(year),
            "reprt_code": "11011", "fs_div": fs_div,
        }, timeout=30)
        d = r.json()
    except Exception:
        return None
    if d.get("status") != "000":
        return None
    return d.get("list", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--start-year", type=int, default=2012)
    ap.add_argument("--end-year", type=int, default=2023)
    ap.add_argument("--sleep", type=float, default=0.25)
    args = ap.parse_args()

    load_env(BASE / ".env")
    load_env(BASE / "dart_collector/.env")
    key = os.getenv("DART_API_KEY", "")
    if not key:
        raise SystemExit("DART_API_KEY 없음")

    corps, _ = corp_map()
    rdate = rcept_date_map()
    names = names_map()
    print(f"[init] 종목 {len(corps)}개 × 연도 {args.start_year}-{args.end_year}")

    rows = []
    n_ok = n_call = 0
    for sc, corp in sorted(corps.items()):
        for year in range(args.start_year, args.end_year + 1):
            n_call += 1
            accounts = fetch(key, corp, year, "CFS")  # 연결 우선
            fs = "연결"
            if not accounts:
                accounts = fetch(key, corp, year, "OFS")  # 별도 폴백
                fs = "별도"
            time.sleep(args.sleep)
            if not accounts:
                continue
            sales = pick(accounts, "매출액", "수익(매출액)", "영업수익", "매출")
            op = pick(accounts, "영업이익", "영업이익(손실)")
            ni = pick(accounts, "당기순이익", "당기순이익(손실)", "당기순이익(손실)")
            if sales is None and op is None:
                continue
            key2 = (sc, str(year))
            pub = rdate.get(key2)
            est = ""
            if not pub:
                pub = f"{year + 1}-03-31"  # 보수적 추정(사업보고서 통상 3월 말)
                est = "estimated"
            rows.append({
                "stock_code": sc, "stock_name": names.get(sc, ""),
                "business_year": year, "rcept_date": pub, "date_basis": est or "filing",
                "fs_div": fs, "sales": sales if sales is not None else "",
                "operating_profit": op if op is not None else "",
                "net_income": ni if ni is not None else "",
            })
            n_ok += 1
        if n_call % 120 == 0:
            print(f"  ... {n_call} calls, {n_ok} rows")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["stock_code", "stock_name", "business_year",
                                          "rcept_date", "date_basis", "fs_div",
                                          "sales", "operating_profit", "net_income"])
        w.writeheader()
        w.writerows(rows)
    print(f"[done] rows={len(rows)} (calls={n_call}) -> {args.out}")


if __name__ == "__main__":
    main()
