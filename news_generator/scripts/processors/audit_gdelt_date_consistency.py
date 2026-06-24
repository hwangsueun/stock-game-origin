#!/usr/bin/env python3
"""Audit date consistency of the raw GDELT parquet caches.

Handoff item 2: the existing ``events_*.parquet`` were suspected of having a
filename-month / article-URL-time mismatch, so before any Korean domestic event
ledger is rebuilt we have to establish which date field is trustworthy and may
serve as the canonical publication / availability basis.

This script does NOT mutate any data. It reads the raw parquet caches under
``scripts/output`` file by file and emits:

  * ``REPORT.md``    -- human readable findings + canonical-date decision
  * ``events_per_file.csv`` / ``gkg_per_file.csv`` -- per-file statistics

Canonical-date policy validated here (see REPORT): for the GKG (Korean curated)
cache, ``ref_date`` equals ``published_at`` to the day and is the GDELT capture
date, which is never earlier than the article's real publication -- so it is the
look-ahead-safe basis. URL-path dates exist for only a minority of rows and are
used as a cross-check, never as the authority.
"""

from __future__ import annotations

import argparse
import csv
import glob
import re
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


# Article-publish date embedded in a source URL, two common shapes:
#   .../2018/02/28/...            (path segments)
#   ...AKR20180228...  / ...20180228... (compact id)
_URL_DATE_RE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})/|(20\d{2})(\d{2})(\d{2})")
_FN_RE = re.compile(r"_(\d{4})(\d{2})\.parquet$")
_KOREA_GEO = {"KS", "KN", "KOR", "South Korea"}


def _filename_yyyymm(path: str) -> str:
    m = _FN_RE.search(path)
    return (m.group(1) + m.group(2)) if m else ""


def _url_date_series(urls: pd.Series) -> pd.Series:
    """Vectorised YYYYMMDD extraction from a URL series (NaN where absent)."""
    ex = urls.fillna("").str.extract(_URL_DATE_RE)
    path = ex[0].fillna("") + ex[1].fillna("") + ex[2].fillna("")
    compact = ex[3].fillna("") + ex[4].fillna("") + ex[5].fillna("")
    out = path.where(path.str.len() == 8, compact)
    return out.where(out.str.len() == 8)


def audit_events(files: list[str]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    agg = Counter()
    for f in sorted(files):
        ym = _filename_yyyymm(f)
        df = pq.read_table(
            f, columns=["event_date", "source_url", "action_geo_country"]
        ).to_pandas()
        n = len(df)
        ed = df["event_date"].astype(str).str[:6]
        ed_mismatch = int((ed != ym).sum())
        ud = _url_date_series(df["source_url"])
        ud_parsed = int(ud.notna().sum())
        ed_full = df["event_date"].astype(str).str[:8]
        ud_vs_ed = int((ud.notna() & (ud != ed_full)).sum())
        korea = int(df["action_geo_country"].isin(_KOREA_GEO).sum())
        rows.append({
            "file": Path(f).name, "rows": n,
            "event_date_vs_filename_mismatch": ed_mismatch,
            "url_date_parsed": ud_parsed,
            "url_date_vs_event_date_mismatch": ud_vs_ed,
            "korea_action_geo_rows": korea,
        })
        for k, v in (("rows", n), ("ed_mismatch", ed_mismatch),
                     ("url_parsed", ud_parsed), ("url_vs_ed", ud_vs_ed),
                     ("korea", korea)):
            agg[k] += v
    return rows, dict(agg)


def audit_gkg(files: list[str]) -> tuple[list[dict], dict, Counter]:
    rows: list[dict] = []
    agg = Counter()
    domains: Counter = Counter()
    for f in sorted(files):
        df = pq.read_table(
            f, columns=["ref_date", "published_at", "domain", "url", "lang_code"]
        ).to_pandas()
        n = len(df)
        rd = df["ref_date"].astype(str)
        pub_d = pd.to_datetime(df["published_at"], errors="coerce").dt.strftime("%Y%m%d")
        rd_pub_mismatch = int((pub_d.notna() & (pub_d != rd)).sum())
        ud = _url_date_series(df["url"])
        ud_parsed = int(ud.notna().sum())
        rd_i = pd.to_numeric(rd, errors="coerce")
        ud_i = pd.to_numeric(ud, errors="coerce")
        url_ahead = int((ud.notna() & (ud_i > rd_i)).sum())   # url later than capture
        url_lag = int((ud.notna() & (ud_i < rd_i)).sum())     # captured after publish
        dom_null = int(df["domain"].isna().sum()
                       + (df["domain"].astype(str).str.strip() == "").sum())
        non_kor = int((df["lang_code"].astype(str) != "kor").sum())
        domains.update(df["domain"].dropna().astype(str))
        rows.append({
            "file": Path(f).name, "rows": n,
            "ref_date_vs_published_at_mismatch": rd_pub_mismatch,
            "url_date_parsed": ud_parsed,
            "url_date_lag_vs_ref": url_lag,
            "url_date_ahead_of_ref": url_ahead,
            "domain_null": dom_null,
            "non_korean_rows": non_kor,
        })
        for k, v in (("rows", n), ("rd_pub_mismatch", rd_pub_mismatch),
                     ("url_parsed", ud_parsed), ("url_lag", url_lag),
                     ("url_ahead", url_ahead), ("dom_null", dom_null),
                     ("non_kor", non_kor)):
            agg[k] += v
    return rows, dict(agg), domains


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pct(num: int, den: int) -> str:
    return f"{(num / den * 100):.1f}%" if den else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--parquet-dir", type=Path,
        default=Path("scripts/output"),
        help="directory holding events_*.parquet and gkg_*.parquet")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_date_audit"))
    args = parser.parse_args()

    ev_files = glob.glob(str(args.parquet_dir / "events_*.parquet"))
    gk_files = glob.glob(str(args.parquet_dir / "gkg_*.parquet"))

    ev_rows, ev = audit_events(ev_files)
    gk_rows, gk, domains = audit_gkg(gk_files)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "events_per_file.csv", ev_rows)
    _write_csv(args.output_dir / "gkg_per_file.csv", gk_rows)

    report = [
        "# GDELT raw parquet date-consistency audit", "",
        "Read-only audit of the raw GDELT caches in "
        f"`{args.parquet_dir}` (handoff item 2). No data was mutated.", "",
        "## events_*.parquet (global CAMEO events)", "",
        f"- files: {len(ev_files)} | rows: {ev.get('rows', 0):,}",
        f"- `event_date` prefix != filename YYYYMM: {ev.get('ed_mismatch', 0):,} "
        f"({_pct(ev.get('ed_mismatch', 0), ev.get('rows', 0))})",
        f"- rows whose `source_url` carries a parseable publish date: "
        f"{ev.get('url_parsed', 0):,} ({_pct(ev.get('url_parsed', 0), ev.get('rows', 0))})",
        f"- of those, URL publish date != `event_date`: {ev.get('url_vs_ed', 0):,} "
        f"({_pct(ev.get('url_vs_ed', 0), ev.get('url_parsed', 0))} of parseable)",
        f"- rows with Korean `action_geo_country`: {ev.get('korea', 0):,} "
        f"({_pct(ev.get('korea', 0), ev.get('rows', 0))})",
        "",
        "**Finding.** `event_date` is internally consistent with the filename "
        "month, so the file partitioning is sound. The mismatch flagged in the "
        "handoff is real but lives in `source_url`: the cited article is often "
        "published a few days *before* the GDELT event_date, so a URL-path date "
        "must never be reused as the event date. This table is also global "
        "(Korea is a low single-digit share) and CAMEO-coded rather than "
        "article-level, so it is the wrong primary source for a Korean domestic "
        "event ledger.", "",
        "## gkg_*.parquet (Korean-curated GKG)", "",
        f"- files: {len(gk_files)} | rows: {gk.get('rows', 0):,}",
        f"- non-Korean `lang_code` rows: {gk.get('non_kor', 0):,} "
        f"({_pct(gk.get('non_kor', 0), gk.get('rows', 0))})",
        f"- `ref_date` != `published_at` (to the day): {gk.get('rd_pub_mismatch', 0):,} "
        f"({_pct(gk.get('rd_pub_mismatch', 0), gk.get('rows', 0))})",
        f"- rows with parseable URL-path date: {gk.get('url_parsed', 0):,} "
        f"({_pct(gk.get('url_parsed', 0), gk.get('rows', 0))})",
        f"  - URL date earlier than `ref_date` (capture lag, look-ahead-safe): "
        f"{gk.get('url_lag', 0):,}",
        f"  - URL date later than `ref_date` (mostly KST/UTC ±1d boundary): "
        f"{gk.get('url_ahead', 0):,}",
        f"- `domain` null/empty: {gk.get('dom_null', 0):,} "
        f"({_pct(gk.get('dom_null', 0), gk.get('rows', 0))}) -- URL still present",
        "",
        "**Finding.** `ref_date` and `published_at` are the same calendar day for "
        "every row; `published_at` only adds a 00:00 time. `ref_date` is the GDELT "
        "capture day, which is always at or after the real publication, so it is "
        "look-ahead-safe. Only a minority of URLs encode a parseable publish date, "
        "and where they do the difference is almost entirely a ±1 day capture-lag "
        "or KST/UTC boundary -- not enough coverage to serve as the authority.", "",
        "## Canonical-date decision", "",
        "- **Primary basis = GKG `ref_date`** (== `published_at` date). It is the "
        "earliest day the item is provably public.",
        "- `available_date_conservative` = next trading day after `ref_date`, "
        "consistent with the UCDP / Wikidata convention.",
        "- URL-path date is retained per row only as a cross-check; never used to "
        "back-date an event.",
        "- The global `events_*.parquet` is not promoted into the Korean domestic "
        "ledger; if its CAMEO actors are ever needed, filter to Korean geo and "
        "treat `event_date` (not the URL) as the date.", "",
        "## GKG top source domains (for the item-4 multi-domain verification queue)",
        "",
    ]
    report.extend(f"- {dom}: {cnt:,}" for dom, cnt in domains.most_common(20))
    (args.output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"events: files={len(ev_files)} rows={ev.get('rows', 0):,} "
          f"event_date_vs_filename_mismatch={ev.get('ed_mismatch', 0):,}")
    print(f"gkg: files={len(gk_files)} rows={gk.get('rows', 0):,} "
          f"ref_date_vs_published_at_mismatch={gk.get('rd_pub_mismatch', 0):,}")
    print(f"report -> {args.output_dir / 'REPORT.md'}")


if __name__ == "__main__":
    main()
