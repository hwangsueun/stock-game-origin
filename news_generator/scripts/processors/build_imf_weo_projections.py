# ============================================================
# build_imf_weo_projections.py
# IMF 세계경제전망(WEO) vintage에서 한국·미국 성장률·물가 전망치를 수집한다.
# 구 URL(2013~2020): /external/pubs/ft/weo/{Y}/{01|02}/weodata/WEO{Apr|Oct}{Y}all.xls (탭구분)
# 각 vintage의 'Estimates Start After'보다 큰 연도가 전망(forecast)이다.
# 출처 수치는 사실이며, 보도 목적의 수치 인용은 저작권 제약 대상이 아니다.
# ============================================================

from __future__ import annotations

import argparse
import csv
import io
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

OLD_URL = "https://www.imf.org/external/pubs/ft/weo/{year}/{half}/weodata/WEO{mon}{year}all.xls"
COUNTRIES = {"KOR": "한국", "USA": "미국"}
SUBJECTS = {"NGDP_RPCH": "real_gdp", "PCPIPCH": "inflation"}
# WEO 발표는 4월·10월 중순. 보수적으로 중순 고정일 사용(거래일 스냅은 오버레이에서).
RELEASE_DATE = {"Apr": "{year}-04-20", "Oct": "{year}-10-15"}


def fetch(url: str) -> Optional[str]:
    # IMF가 비브라우저 UA(urllib)는 403으로 막으므로 curl로 받는다.
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "60", url],
            capture_output=True, timeout=75,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout.decode("latin-1", errors="ignore")
    except Exception:
        return None


def parse_vintage(text: str) -> List[Dict[str, object]]:
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    if not rows:
        return []
    hdr = rows[0]
    try:
        i_iso = hdr.index("ISO")
        i_subj = hdr.index("WEO Subject Code")
    except ValueError:
        return []
    year_cols = {c.strip(): i for i, c in enumerate(hdr) if c.strip().isdigit()}
    i_est = next((i for i, c in enumerate(hdr) if "Estimates Start After" in c), None)

    out = []
    for r in rows[1:]:
        if len(r) <= i_subj or r[i_iso] not in COUNTRIES or r[i_subj] not in SUBJECTS:
            continue
        try:
            est_after = int(r[i_est]) if i_est is not None and r[i_est].strip().isdigit() else None
        except (ValueError, IndexError):
            est_after = None
        for year, idx in year_cols.items():
            if idx >= len(r):
                continue
            raw = r[idx].replace(",", "").strip()
            if not raw or raw in ("n/a", "--"):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            is_forecast = est_after is not None and int(year) > est_after
            out.append({
                "iso": r[i_iso],
                "country": COUNTRIES[r[i_iso]],
                "variable": SUBJECTS[r[i_subj]],
                "year": year,
                "value": round(value, 1),
                "is_forecast": is_forecast,
            })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2013)
    parser.add_argument("--end-year", type=int, default=2020)
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("data/raw/imf_weo_projections.csv"),
    )
    args = parser.parse_args()

    out_rows = []
    vintages = []
    for year in range(args.start_year, args.end_year + 1):
        for half, mon in (("01", "Apr"), ("02", "Oct")):
            url = OLD_URL.format(year=year, half=half, mon=mon)
            text = fetch(url)
            if not text or "WEO Subject Code" not in text:
                continue
            recs = parse_vintage(text)
            forecasts = [r for r in recs if r["is_forecast"]]
            if not forecasts:
                continue
            release = RELEASE_DATE[mon].format(year=year)
            vintages.append(f"{mon}{year}")
            for r in forecasts:
                out_rows.append({
                    "weo_edition": f"{mon} {year}",
                    "release_date": release,
                    "institution": "국제통화기금(IMF)",
                    "required_attribution": "IMF",
                    **{k: r[k] for k in ("iso", "country", "variable", "year", "value")},
                })

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "weo_edition", "release_date", "institution", "required_attribution",
            "iso", "country", "variable", "year", "value",
        ])
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"WEO vintage {len(vintages)}개 / 전망 레코드 {len(out_rows)}건 → {args.output_csv}")
    print(f"vintages: {vintages}")


if __name__ == "__main__":
    main()
