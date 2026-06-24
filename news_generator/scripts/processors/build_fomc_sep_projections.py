# ============================================================
# build_fomc_sep_projections.py
# 미 연준 FOMC 경제전망(SEP) 표를 스크레이핑해 연도별 중앙값 전망치를 수집한다.
# 출처: https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{YYYYMMDD}.htm (퍼블릭 도메인)
# SEP는 분기 FOMC(3·6·9·12월)에서만 발표되므로 비SEP 회의 URL은 404 → 건너뛴다.
# 결과 수치는 그 자체가 사실이며 미 연방정부 산출물이라 저작권 제약이 없다.
# ============================================================

from __future__ import annotations

import argparse
import csv
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

SEP_URL = "https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{date}.htm"

VARIABLES = {
    "Change in real GDP": "real_gdp",
    "Unemployment rate": "unemployment",
    "PCE inflation": "pce_inflation",
    "Federal funds rate": "federal_funds_rate",
}


def fetch(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def parse_medians(html: str, label: str) -> Optional[List[float]]:
    txt = re.sub(r"<[^>]+>", "\n", html)
    txt = re.sub(r"&nbsp;|&#160;", " ", txt)
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if line == label or line.startswith(label + " "):
            vals: List[float] = []
            for token in lines[i + 1:i + 12]:
                if re.fullmatch(r"-?\d+\.\d+", token):
                    vals.append(float(token))
                else:
                    break
            if vals:
                return vals
    return None


def horizon_years(sep_year: int, n_medians: int) -> List[str]:
    # 마지막 값은 장기(longer run), 앞쪽은 sep_year, sep_year+1, ...
    years = [str(sep_year + i) for i in range(n_medians - 1)]
    years.append("장기")
    return years


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--official-csv",
        type=Path,
        default=Path("data/raw/official_macro_releases_2013_2023.csv"),
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("data/raw/fomc_sep_projections_2013_2023.csv"),
    )
    parser.add_argument("--sleep", type=float, default=0.4)
    args = parser.parse_args()

    fomc = []
    with args.official_csv.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["release_category"] == "monetary_policy" and "FOMC" in (
                row["institution"] + row["title"]
            ):
                fomc.append((row["source_release_date"], row["event_date"]))

    out_rows = []
    sep_dates = []
    for us_date, kr_date in fomc:
        compact = us_date.replace("-", "")
        html = fetch(SEP_URL.format(date=compact))
        time.sleep(args.sleep)
        if not html or "fomcprojtabl" not in html.lower():
            continue
        sep_year = int(us_date[:4])
        found_any = False
        for label, var in VARIABLES.items():
            medians = parse_medians(html, label)
            if not medians:
                continue
            found_any = True
            for year, value in zip(horizon_years(sep_year, len(medians)), medians):
                out_rows.append({
                    "sep_us_date": us_date,
                    "available_date_kr": kr_date,
                    "variable": var,
                    "projection_year": year,
                    "median": value,
                })
        if found_any:
            sep_dates.append(us_date)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sep_us_date", "available_date_kr", "variable", "projection_year", "median"],
        )
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"SEP 표 발견: {len(sep_dates)}개 회의 / 전망 레코드 {len(out_rows)}건")
    print(f"기간: {sep_dates[0] if sep_dates else '-'} ~ {sep_dates[-1] if sep_dates else '-'}")
    print(f"저장: {args.output_csv}")


if __name__ == "__main__":
    main()
